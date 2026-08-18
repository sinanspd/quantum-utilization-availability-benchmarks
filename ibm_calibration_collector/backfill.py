from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

LOG = logging.getLogger("ibm_calibration_collector.backfill")

DEFINITION_VERSION = "qubit-and-one-qubit-gate-calibration-dates-v2"
ADVISORY_LOCK_NAME = "ibm_calibration_collector.fetch_cycle_metrics_backfill.v2"

CONTROL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fetch_cycle_metrics_backfill_runs (
  run_id UUID PRIMARY KEY,
  definition_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'failed', 'completed')),
  batch_size INTEGER NOT NULL CHECK (batch_size > 0),
  source_created_at_cutoff TIMESTAMPTZ NOT NULL,
  total_cycle_count BIGINT NOT NULL DEFAULT 0,
  processed_cycle_count BIGINT NOT NULL DEFAULT 0,
  invalid_cycle_count BIGINT NOT NULL DEFAULT 0,
  batch_count BIGINT NOT NULL DEFAULT 0,
  last_sequence_no BIGINT NOT NULL DEFAULT 0,
  last_backend TEXT,
  last_poll_started_at TIMESTAMPTZ,
  last_fetch_cycle_id UUID,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  error_message TEXT
);

ALTER TABLE fetch_cycle_metrics_backfill_runs
  ADD COLUMN IF NOT EXISTS last_sequence_no BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_fetch_cycle_metrics_backfill_runs_status
  ON fetch_cycle_metrics_backfill_runs (status, started_at DESC);

CREATE TABLE IF NOT EXISTS fetch_cycle_metrics_backfill_items (
  run_id UUID NOT NULL REFERENCES fetch_cycle_metrics_backfill_runs(run_id) ON DELETE CASCADE,
  sequence_no BIGINT NOT NULL,
  fetch_cycle_id UUID NOT NULL,
  backend TEXT NOT NULL,
  poll_started_at TIMESTAMPTZ NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  prev_fetch_cycle_id UUID,
  prev_poll_timestamp_utc TIMESTAMPTZ,
  PRIMARY KEY (run_id, sequence_no),
  UNIQUE (run_id, fetch_cycle_id)
);

CREATE TABLE IF NOT EXISTS fetch_cycle_metrics_backfill_audit (
  run_id UUID NOT NULL,
  backed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetch_cycle_id UUID NOT NULL,
  reason TEXT NOT NULL,
  fetch_cycle_before JSONB NOT NULL,
  metrics_before JSONB,
  PRIMARY KEY (run_id, fetch_cycle_id)
);
"""

CALIBRATION_INDEXES = {
    "idx_qubit_props_fetch_cycle_calibration": """
        CREATE INDEX CONCURRENTLY idx_qubit_props_fetch_cycle_calibration
        ON qubit_property_snapshots (fetch_cycle_id, qubit, property_date DESC)
        WHERE property_date IS NOT NULL
    """,
    "idx_gate_props_fetch_cycle_one_qubit_calibration": """
        CREATE INDEX CONCURRENTLY idx_gate_props_fetch_cycle_one_qubit_calibration
        ON gate_property_snapshots (fetch_cycle_id, (qubits[1]), property_date DESC)
        WHERE cardinality(qubits) = 1 AND property_date IS NOT NULL
    """,
    "idx_gate_props_fetch_cycle_edge_calibration": """
        CREATE INDEX CONCURRENTLY idx_gate_props_fetch_cycle_edge_calibration
        ON gate_property_snapshots (fetch_cycle_id, edge_id, property_date DESC)
        WHERE edge_id IS NOT NULL AND property_date IS NOT NULL
    """,
}

INVALID_CYCLE_PREDICATE = """
(
  cycle.raw_properties IS NULL
  OR jsonb_typeof(cycle.raw_properties) <> 'object'
  OR NOT (cycle.raw_properties ? 'qubits')
  OR NOT (cycle.raw_properties ? 'gates')
  OR CASE jsonb_typeof(cycle.raw_properties -> 'errors')
    WHEN 'array' THEN jsonb_array_length(cycle.raw_properties -> 'errors') > 0
    WHEN 'object' THEN cycle.raw_properties -> 'errors' <> '{}'::jsonb
    WHEN 'string' THEN cycle.raw_properties ->> 'errors' <> ''
    WHEN 'boolean' THEN (cycle.raw_properties ->> 'errors')::boolean
    WHEN 'number' THEN (cycle.raw_properties ->> 'errors')::numeric <> 0
    ELSE FALSE
  END
)
"""

UPSERT_BATCH_SQL = """
CREATE TEMP TABLE backfill_relevant_cycles ON COMMIT DROP AS
SELECT fetch_cycle_id FROM backfill_batch_cycles
UNION
SELECT prev_fetch_cycle_id FROM backfill_batch_cycles WHERE prev_fetch_cycle_id IS NOT NULL;

CREATE UNIQUE INDEX ON backfill_relevant_cycles (fetch_cycle_id);

CREATE TEMP TABLE backfill_qubit_latest ON COMMIT DROP AS
SELECT dates.fetch_cycle_id, dates.qubit, max(dates.property_date) AS latest_date
FROM (
  SELECT snapshot.fetch_cycle_id, snapshot.qubit, snapshot.property_date
  FROM qubit_property_snapshots snapshot
  JOIN backfill_relevant_cycles relevant USING (fetch_cycle_id)
  WHERE snapshot.property_date IS NOT NULL

  UNION ALL

  SELECT snapshot.fetch_cycle_id, snapshot.qubits[1], snapshot.property_date
  FROM gate_property_snapshots snapshot
  JOIN backfill_relevant_cycles relevant USING (fetch_cycle_id)
  WHERE cardinality(snapshot.qubits) = 1 AND snapshot.property_date IS NOT NULL
) dates
GROUP BY dates.fetch_cycle_id, dates.qubit;

CREATE UNIQUE INDEX ON backfill_qubit_latest (fetch_cycle_id, qubit);

CREATE TEMP TABLE backfill_edge_latest ON COMMIT DROP AS
SELECT snapshot.fetch_cycle_id, snapshot.edge_id, max(snapshot.property_date) AS latest_date
FROM gate_property_snapshots snapshot
JOIN backfill_relevant_cycles relevant USING (fetch_cycle_id)
WHERE snapshot.edge_id IS NOT NULL AND snapshot.property_date IS NOT NULL
GROUP BY snapshot.fetch_cycle_id, snapshot.edge_id;

CREATE UNIQUE INDEX ON backfill_edge_latest (fetch_cycle_id, edge_id);

WITH qubit_summary AS (
  SELECT
    batch.fetch_cycle_id,
    (array_agg(latest.qubit ORDER BY latest.latest_date, latest.qubit)
      FILTER (WHERE latest.qubit IS NOT NULL))[1] AS oldest_qubit,
    min(latest.latest_date) AS oldest_timestamp,
    CASE WHEN min(latest.latest_date) IS NULL THEN NULL ELSE greatest(
      0.0, extract(epoch FROM (batch.poll_timestamp_utc - min(latest.latest_date)))
    ) END::double precision AS max_age_seconds,
    COALESCE(
      jsonb_object_agg(
        latest.qubit::text,
        to_jsonb(to_char(latest.latest_date AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
      ) FILTER (WHERE latest.qubit IS NOT NULL),
      '{}'::jsonb
    ) AS latest_dates
  FROM backfill_batch_cycles batch
  LEFT JOIN backfill_qubit_latest latest ON latest.fetch_cycle_id = batch.fetch_cycle_id
  GROUP BY batch.fetch_cycle_id, batch.poll_timestamp_utc
),
edge_summary AS (
  SELECT
    batch.fetch_cycle_id,
    (array_agg(latest.edge_id ORDER BY latest.latest_date, latest.edge_id)
      FILTER (WHERE latest.edge_id IS NOT NULL))[1] AS oldest_edge_id,
    min(latest.latest_date) AS oldest_timestamp,
    CASE WHEN min(latest.latest_date) IS NULL THEN NULL ELSE greatest(
      0.0, extract(epoch FROM (batch.poll_timestamp_utc - min(latest.latest_date)))
    ) END::double precision AS max_age_seconds,
    COALESCE(
      jsonb_object_agg(
        latest.edge_id,
        to_jsonb(to_char(latest.latest_date AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
      ) FILTER (WHERE latest.edge_id IS NOT NULL),
      '{}'::jsonb
    ) AS latest_dates
  FROM backfill_batch_cycles batch
  LEFT JOIN backfill_edge_latest latest ON latest.fetch_cycle_id = batch.fetch_cycle_id
  GROUP BY batch.fetch_cycle_id, batch.poll_timestamp_utc
),
qubit_changes AS (
  SELECT
    batch.fetch_cycle_id,
    COALESCE(
      array_agg(current.qubit ORDER BY current.qubit)
        FILTER (WHERE previous.latest_date IS NOT NULL
                AND current.latest_date > previous.latest_date),
      ARRAY[]::integer[]
    ) AS calibrated
  FROM backfill_batch_cycles batch
  LEFT JOIN backfill_qubit_latest current ON current.fetch_cycle_id = batch.fetch_cycle_id
  LEFT JOIN backfill_qubit_latest previous
    ON previous.fetch_cycle_id = batch.prev_fetch_cycle_id
   AND previous.qubit = current.qubit
  GROUP BY batch.fetch_cycle_id
),
edge_changes AS (
  SELECT
    batch.fetch_cycle_id,
    COALESCE(
      array_agg(current.edge_id ORDER BY current.edge_id)
        FILTER (WHERE previous.latest_date IS NOT NULL
                AND current.latest_date > previous.latest_date),
      ARRAY[]::text[]
    ) AS calibrated
  FROM backfill_batch_cycles batch
  LEFT JOIN backfill_edge_latest current ON current.fetch_cycle_id = batch.fetch_cycle_id
  LEFT JOIN backfill_edge_latest previous
    ON previous.fetch_cycle_id = batch.prev_fetch_cycle_id
   AND previous.edge_id = current.edge_id
  GROUP BY batch.fetch_cycle_id
)
INSERT INTO fetch_cycle_metrics (
  fetch_cycle_id, backend, poll_timestamp_utc,
  prev_fetch_cycle_id, prev_poll_timestamp_utc,
  num_qubits_calibrated_since_last_fetch, num_edges_calibrated_since_last_fetch,
  qubits_calibrated_since_last_fetch, edges_calibrated_since_last_fetch,
  max_qubit_calibration_age_seconds, max_edge_calibration_age_seconds,
  oldest_qubit, oldest_qubit_calibration_timestamp,
  oldest_edge_id, oldest_edge_calibration_timestamp,
  qubit_latest_calibration_dates, edge_latest_calibration_dates
)
SELECT
  batch.fetch_cycle_id, batch.backend, batch.poll_timestamp_utc,
  batch.prev_fetch_cycle_id, batch.prev_poll_timestamp_utc,
  cardinality(qubit_changes.calibrated), cardinality(edge_changes.calibrated),
  qubit_changes.calibrated, edge_changes.calibrated,
  qubit_summary.max_age_seconds, edge_summary.max_age_seconds,
  qubit_summary.oldest_qubit, qubit_summary.oldest_timestamp,
  edge_summary.oldest_edge_id, edge_summary.oldest_timestamp,
  qubit_summary.latest_dates, edge_summary.latest_dates
FROM backfill_batch_cycles batch
JOIN qubit_summary USING (fetch_cycle_id)
JOIN edge_summary USING (fetch_cycle_id)
JOIN qubit_changes USING (fetch_cycle_id)
JOIN edge_changes USING (fetch_cycle_id)
ON CONFLICT (fetch_cycle_id) DO UPDATE SET
  backend = EXCLUDED.backend,
  poll_timestamp_utc = EXCLUDED.poll_timestamp_utc,
  prev_fetch_cycle_id = EXCLUDED.prev_fetch_cycle_id,
  prev_poll_timestamp_utc = EXCLUDED.prev_poll_timestamp_utc,
  num_qubits_calibrated_since_last_fetch =
    EXCLUDED.num_qubits_calibrated_since_last_fetch,
  num_edges_calibrated_since_last_fetch =
    EXCLUDED.num_edges_calibrated_since_last_fetch,
  qubits_calibrated_since_last_fetch = EXCLUDED.qubits_calibrated_since_last_fetch,
  edges_calibrated_since_last_fetch = EXCLUDED.edges_calibrated_since_last_fetch,
  max_qubit_calibration_age_seconds = EXCLUDED.max_qubit_calibration_age_seconds,
  max_edge_calibration_age_seconds = EXCLUDED.max_edge_calibration_age_seconds,
  oldest_qubit = EXCLUDED.oldest_qubit,
  oldest_qubit_calibration_timestamp = EXCLUDED.oldest_qubit_calibration_timestamp,
  oldest_edge_id = EXCLUDED.oldest_edge_id,
  oldest_edge_calibration_timestamp = EXCLUDED.oldest_edge_calibration_timestamp,
  qubit_latest_calibration_dates = EXCLUDED.qubit_latest_calibration_dates,
  edge_latest_calibration_dates = EXCLUDED.edge_latest_calibration_dates;
"""


@dataclass(frozen=True)
class BackfillRun:
    run_id: UUID
    status: str
    batch_size: int
    source_created_at_cutoff: datetime
    total_cycle_count: int
    processed_cycle_count: int
    batch_count: int
    last_sequence_no: int
    last_backend: str | None
    last_poll_started_at: datetime | None
    last_fetch_cycle_id: UUID | None


def connect(database_url: str, *, autocommit: bool = False) -> Connection[Any]:
    return psycopg.connect(
        database_url,
        autocommit=autocommit,
        row_factory=dict_row,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def ensure_control_schema(database_url: str) -> None:
    with connect(database_url) as conn:
        conn.execute(CONTROL_SCHEMA_SQL)
        conn.commit()


def prepare_indexes(database_url: str) -> None:
    """Build selective indexes without blocking collector writes."""
    with connect(database_url, autocommit=True) as conn:
        conn.execute("SET statement_timeout = 0")
        for name, create_sql in CALIBRATION_INDEXES.items():
            row = conn.execute(
                """
                SELECT index.indisvalid
                FROM pg_class relation
                JOIN pg_index index ON index.indexrelid = relation.oid
                JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = current_schema() AND relation.relname = %s
                """,
                (name,),
            ).fetchone()
            if row is not None and row["indisvalid"]:
                LOG.info("index ready: %s", name)
                continue
            if row is not None:
                LOG.warning("dropping interrupted invalid index: %s", name)
                conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
            LOG.info("building index concurrently: %s", name)
            conn.execute(create_sql)
            LOG.info("index ready: %s", name)


def acquire_lock(conn: Connection[Any]) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
        (ADVISORY_LOCK_NAME,),
    ).fetchone()
    if row is None or not row["acquired"]:
        raise RuntimeError("another metrics backfill process is already running")


def _reason_sql(alias: str = "cycle") -> str:
    return f"""
    CASE
      WHEN {alias}.raw_properties IS NULL THEN 'raw_properties is null'
      WHEN jsonb_typeof({alias}.raw_properties) <> 'object'
        THEN 'raw_properties is not an object'
      WHEN NOT ({alias}.raw_properties ? 'qubits')
        OR NOT ({alias}.raw_properties ? 'gates')
        THEN 'raw_properties is missing required qubits/gates fields'
      ELSE 'raw_properties contains a non-empty errors value'
    END
    """


def _row_to_run(row: dict[str, Any]) -> BackfillRun:
    return BackfillRun(
        run_id=row["run_id"],
        status=row["status"],
        batch_size=row["batch_size"],
        source_created_at_cutoff=row["source_created_at_cutoff"],
        total_cycle_count=row["total_cycle_count"],
        processed_cycle_count=row["processed_cycle_count"],
        batch_count=row["batch_count"],
        last_sequence_no=row["last_sequence_no"],
        last_backend=row["last_backend"],
        last_poll_started_at=row["last_poll_started_at"],
        last_fetch_cycle_id=row["last_fetch_cycle_id"],
    )


def get_or_create_run(
    conn: Connection[Any],
    *,
    batch_size: int,
    run_id: UUID | None,
    force_new: bool,
) -> BackfillRun:
    with conn.transaction():
        selected = None
        if run_id is not None:
            selected = conn.execute(
                "SELECT * FROM fetch_cycle_metrics_backfill_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if selected is None:
                raise ValueError(f"backfill run does not exist: {run_id}")
        elif not force_new:
            selected = conn.execute(
                """
                SELECT *
                FROM fetch_cycle_metrics_backfill_runs
                WHERE definition_version = %s AND status IN ('running', 'failed')
                ORDER BY started_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (DEFINITION_VERSION,),
            ).fetchone()

        if selected is not None:
            if selected["status"] == "completed":
                return _row_to_run(selected)
            selected = conn.execute(
                """
                UPDATE fetch_cycle_metrics_backfill_runs
                SET status = 'running', last_heartbeat_at = clock_timestamp(),
                    error_message = NULL
                WHERE run_id = %s
                RETURNING *
                """,
                (selected["run_id"],),
            ).fetchone()
            assert selected is not None
            return _row_to_run(selected)

        new_run_id = uuid4()
        cutoff = conn.execute("SELECT clock_timestamp() AS cutoff").fetchone()["cutoff"]
        conn.execute(
            """
            INSERT INTO fetch_cycle_metrics_backfill_runs (
              run_id, definition_version, status, batch_size, source_created_at_cutoff
            ) VALUES (%s, %s, 'running', %s, %s)
            """,
            (new_run_id, DEFINITION_VERSION, batch_size, cutoff),
        )

        conn.execute(
            f"""
            WITH invalid AS (
              SELECT cycle.id AS fetch_cycle_id, {_reason_sql()} AS reason
              FROM fetch_cycles cycle
              WHERE cycle.success = TRUE
                AND cycle.created_at <= %s
                AND {INVALID_CYCLE_PREDICATE}
            )
            INSERT INTO fetch_cycle_metrics_backfill_audit (
              run_id, fetch_cycle_id, reason, fetch_cycle_before, metrics_before
            )
            SELECT
              %s, invalid.fetch_cycle_id, invalid.reason, to_jsonb(cycle),
              CASE WHEN metrics.fetch_cycle_id IS NULL THEN NULL ELSE to_jsonb(metrics) END
            FROM invalid
            JOIN fetch_cycles cycle ON cycle.id = invalid.fetch_cycle_id
            LEFT JOIN fetch_cycle_metrics metrics ON metrics.fetch_cycle_id = invalid.fetch_cycle_id
            ON CONFLICT (run_id, fetch_cycle_id) DO NOTHING
            """,
            (cutoff, new_run_id),
        )
        invalid_count = conn.execute(
            f"""
            WITH invalid AS (
              SELECT cycle.id, {_reason_sql()} AS reason
              FROM fetch_cycles cycle
              WHERE cycle.success = TRUE
                AND cycle.created_at <= %s
                AND {INVALID_CYCLE_PREDICATE}
            ), updated AS (
              UPDATE fetch_cycles cycle
              SET success = FALSE,
                  error_message = concat_ws(
                    ' | ', NULLIF(cycle.error_message, ''),
                    'Historical backfill rejected properties payload: ' || invalid.reason
                  )
              FROM invalid
              WHERE cycle.id = invalid.id
              RETURNING cycle.id
            )
            SELECT count(*) AS count FROM updated
            """,
            (cutoff,),
        ).fetchone()["count"]
        conn.execute(
            """
            DELETE FROM fetch_cycle_metrics metrics
            USING fetch_cycle_metrics_backfill_audit audit
            WHERE audit.run_id = %s AND metrics.fetch_cycle_id = audit.fetch_cycle_id
            """,
            (new_run_id,),
        )
        # Materialize the exact visible input set and its previous-valid-cycle relationship.
        # This makes the run stable even if a collector transaction commits after this one.
        conn.execute(
            """
            INSERT INTO fetch_cycle_metrics_backfill_items (
              run_id, sequence_no, fetch_cycle_id, backend, poll_started_at,
              poll_timestamp_utc, prev_fetch_cycle_id, prev_poll_timestamp_utc
            )
            SELECT
              %s,
              row_number() OVER (ORDER BY valid.backend, valid.poll_started_at, valid.id),
              valid.id,
              valid.backend,
              valid.poll_started_at,
              valid.poll_timestamp_utc,
              lag(valid.id) OVER (
                PARTITION BY valid.backend ORDER BY valid.poll_started_at, valid.id
              ),
              lag(valid.poll_timestamp_utc) OVER (
                PARTITION BY valid.backend ORDER BY valid.poll_started_at, valid.id
              )
            FROM (
              SELECT
                cycle.id,
                cycle.backend,
                cycle.poll_started_at,
                COALESCE(cycle.poll_finished_at, cycle.poll_started_at) AS poll_timestamp_utc
              FROM fetch_cycles cycle
              WHERE cycle.success = TRUE AND cycle.created_at <= %s
            ) valid
            """,
            (new_run_id, cutoff),
        )
        total = conn.execute(
            """
            SELECT count(*) AS count
            FROM fetch_cycle_metrics_backfill_items
            WHERE run_id = %s
            """,
            (new_run_id,),
        ).fetchone()["count"]
        created = conn.execute(
            """
            UPDATE fetch_cycle_metrics_backfill_runs
            SET total_cycle_count = %s, invalid_cycle_count = %s,
                last_heartbeat_at = clock_timestamp()
            WHERE run_id = %s
            RETURNING *
            """,
            (total, invalid_count, new_run_id),
        ).fetchone()
        assert created is not None
        return _row_to_run(created)


def process_one_batch(conn: Connection[Any], run: BackfillRun) -> tuple[BackfillRun, int]:
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout = 0")
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute("SET LOCAL TIME ZONE 'UTC'")

        conn.execute(
            """
            CREATE TEMP TABLE backfill_batch_cycles ON COMMIT DROP AS
            SELECT
              sequence_no, fetch_cycle_id, backend, poll_started_at,
              poll_timestamp_utc, prev_fetch_cycle_id, prev_poll_timestamp_utc
            FROM fetch_cycle_metrics_backfill_items
            WHERE run_id = %s AND sequence_no > %s
            ORDER BY sequence_no
            LIMIT %s
            """,
            (run.run_id, run.last_sequence_no, run.batch_size),
        )
        conn.execute("CREATE UNIQUE INDEX ON backfill_batch_cycles (fetch_cycle_id)")

        batch_count = conn.execute(
            "SELECT count(*) AS count FROM backfill_batch_cycles"
        ).fetchone()["count"]
        if batch_count == 0:
            completed = conn.execute(
                """
                UPDATE fetch_cycle_metrics_backfill_runs
                SET status = 'completed', completed_at = clock_timestamp(),
                    last_heartbeat_at = clock_timestamp(), error_message = NULL
                WHERE run_id = %s
                RETURNING *
                """,
                (run.run_id,),
            ).fetchone()
            assert completed is not None
            return _row_to_run(completed), 0

        conn.execute(UPSERT_BATCH_SQL)
        last = conn.execute(
            """
            SELECT sequence_no, backend, poll_started_at, fetch_cycle_id
            FROM backfill_batch_cycles
            ORDER BY sequence_no DESC
            LIMIT 1
            """
        ).fetchone()
        updated = conn.execute(
            """
            UPDATE fetch_cycle_metrics_backfill_runs
            SET processed_cycle_count = processed_cycle_count + %s,
                batch_count = batch_count + 1,
                last_sequence_no = %s,
                last_backend = %s,
                last_poll_started_at = %s,
                last_fetch_cycle_id = %s,
                last_heartbeat_at = clock_timestamp(),
                error_message = NULL
            WHERE run_id = %s
            RETURNING *
            """,
            (
                batch_count,
                last["sequence_no"],
                last["backend"],
                last["poll_started_at"],
                last["fetch_cycle_id"],
                run.run_id,
            ),
        ).fetchone()
        assert updated is not None
        return _row_to_run(updated), batch_count


def mark_failed(database_url: str, run_id: UUID, error: BaseException) -> None:
    try:
        with connect(database_url) as conn:
            conn.execute(
                """
                UPDATE fetch_cycle_metrics_backfill_runs
                SET status = 'failed', error_message = %s,
                    last_heartbeat_at = clock_timestamp()
                WHERE run_id = %s AND status <> 'completed'
                """,
                (repr(error)[:5000], run_id),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 - preserve the original failure.
        LOG.exception("could not record backfill failure for run_id=%s", run_id)


def execute_backfill(
    database_url: str,
    *,
    batch_size: int,
    run_id: UUID | None,
    force_new: bool,
    max_batches: int | None,
) -> BackfillRun:
    active_run: BackfillRun | None = None
    try:
        # Autocommit keeps the session-level advisory lock while allowing each explicit
        # conn.transaction() block below to become an independent durable batch.
        with connect(database_url, autocommit=True) as conn:
            acquire_lock(conn)
            active_run = get_or_create_run(
                conn,
                batch_size=batch_size,
                run_id=run_id,
                force_new=force_new,
            )
            if active_run.status == "completed":
                LOG.info("run already completed: %s", active_run.run_id)
                return active_run

            LOG.info(
                "run_id=%s progress=%s/%s batches=%s batch_size=%s",
                active_run.run_id,
                active_run.processed_cycle_count,
                active_run.total_cycle_count,
                active_run.batch_count,
                active_run.batch_size,
            )
            batches_this_process = 0
            while max_batches is None or batches_this_process < max_batches:
                started = time.monotonic()
                active_run, processed = process_one_batch(conn, active_run)
                if processed == 0:
                    LOG.info(
                        "backfill complete run_id=%s processed=%s/%s batches=%s",
                        active_run.run_id,
                        active_run.processed_cycle_count,
                        active_run.total_cycle_count,
                        active_run.batch_count,
                    )
                    return active_run
                batches_this_process += 1
                LOG.info(
                    "committed batch=%s rows=%s progress=%s/%s elapsed=%.1fs",
                    active_run.batch_count,
                    processed,
                    active_run.processed_cycle_count,
                    active_run.total_cycle_count,
                    time.monotonic() - started,
                )

            LOG.info(
                "stopped at --max-batches run_id=%s progress=%s/%s; rerun to resume",
                active_run.run_id,
                active_run.processed_cycle_count,
                active_run.total_cycle_count,
            )
            return active_run
    except BaseException as exc:
        if active_run is not None and not isinstance(exc, (KeyboardInterrupt, SystemExit)):
            mark_failed(database_url, active_run.run_id, exc)
        raise


def print_status(database_url: str) -> None:
    ensure_control_schema(database_url)
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT run_id, status, definition_version, batch_size,
                   processed_cycle_count, total_cycle_count, invalid_cycle_count,
                   batch_count, started_at, last_heartbeat_at, completed_at,
                   error_message
            FROM fetch_cycle_metrics_backfill_runs
            ORDER BY started_at DESC
            LIMIT 10
            """
        ).fetchall()
    if not rows:
        print("No backfill runs found.")
        return
    for row in rows:
        print(
            f"{row['run_id']} status={row['status']} "
            f"progress={row['processed_cycle_count']}/{row['total_cycle_count']} "
            f"batches={row['batch_count']} invalid={row['invalid_cycle_count']} "
            f"heartbeat={row['last_heartbeat_at']}"
        )
        if row["error_message"]:
            print(f"  error={row['error_message']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restart-safe batched reconstruction of historical fetch-cycle metrics."
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--run-id", type=UUID, default=None, help="Resume this specific run.")
    parser.add_argument(
        "--new-run", action="store_true", help="Start over even if an incomplete run exists."
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop cleanly after this many batches; useful for validation.",
    )
    parser.add_argument("--status", action="store_true", help="Print recent run progress and exit.")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create the supporting indexes and control tables, then exit.",
    )
    parser.add_argument(
        "--skip-index-preparation",
        action="store_true",
        help="Skip concurrent supporting-index checks.",
    )
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.max_batches is not None and args.max_batches <= 0:
        parser.error("--max-batches must be positive")
    if args.max_retries < 0:
        parser.error("--max-retries cannot be negative")
    if args.new_run and args.run_id is not None:
        parser.error("--new-run and --run-id cannot be used together")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    if args.status:
        print_status(database_url)
        return

    ensure_control_schema(database_url)
    if not args.skip_index_preparation:
        prepare_indexes(database_url)
    if args.prepare_only:
        LOG.info("backfill preparation complete")
        return

    retries = 0
    force_new = args.new_run
    while True:
        try:
            result = execute_backfill(
                database_url,
                batch_size=args.batch_size,
                run_id=args.run_id,
                force_new=force_new,
                max_batches=args.max_batches,
            )
            if result.status == "failed":
                sys.exit(1)
            return
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if retries >= args.max_retries:
                raise
            retries += 1
            force_new = False
            delay = min(60, 2**retries)
            LOG.warning(
                "database connection interrupted (%s/%s); resuming in %ss: %s",
                retries,
                args.max_retries,
                delay,
                exc,
            )
            time.sleep(delay)


if __name__ == "__main__":
    main()
