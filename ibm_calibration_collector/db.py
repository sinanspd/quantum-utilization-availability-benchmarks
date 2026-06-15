from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import (
    BackendStatusSnapshot,
    FetchCycleMetrics,
    GatePropertySnapshot,
    ParsedBackendProperties,
    QubitPropertySnapshot,
)


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def connect(self) -> Connection[Any]:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        if not schema_path.exists():
            # Installed package fallback: schema.sql might not be packaged in editable mode.
            schema_path = Path.cwd() / "sql" / "schema.sql"
        with self.connect() as conn:
            conn.execute(schema_path.read_text())
            conn.commit()

    def get_previous_summaries(
        self,
        backend: str,
    ) -> tuple[str | None, datetime | None, dict[int, datetime] | None, dict[str, datetime] | None]:
        with self.connect() as conn:
            prev = conn.execute(
                """
                SELECT id::text AS id, COALESCE(poll_finished_at, poll_started_at) AS poll_ts
                FROM fetch_cycles
                WHERE backend = %s AND success = TRUE
                ORDER BY poll_started_at DESC
                LIMIT 1
                """,
                (backend,),
            ).fetchone()
            if prev is None:
                return None, None, None, None

            fetch_id = prev["id"]
            poll_ts = prev["poll_ts"]

            q_rows = conn.execute(
                """
                SELECT qubit, max(property_date) AS latest_date
                FROM qubit_property_snapshots
                WHERE fetch_cycle_id = %s AND property_date IS NOT NULL
                GROUP BY qubit
                """,
                (fetch_id,),
            ).fetchall()
            e_rows = conn.execute(
                """
                SELECT edge_id, max(property_date) AS latest_date
                FROM gate_property_snapshots
                WHERE fetch_cycle_id = %s
                  AND edge_id IS NOT NULL
                  AND property_date IS NOT NULL
                GROUP BY edge_id
                """,
                (fetch_id,),
            ).fetchall()

        qubit_dates = {int(r["qubit"]): r["latest_date"] for r in q_rows if r["latest_date"]}
        edge_dates = {str(r["edge_id"]): r["latest_date"] for r in e_rows if r["latest_date"]}
        return fetch_id, poll_ts, qubit_dates, edge_dates

    def insert_successful_cycle(
        self,
        *,
        backend: str,
        poll_started_at: datetime,
        poll_finished_at: datetime,
        raw_status: dict[str, Any],
        raw_properties: dict[str, Any],
        status_snapshot: BackendStatusSnapshot,
        parsed_properties: ParsedBackendProperties,
        metrics: FetchCycleMetrics,
    ) -> str:
        fetch_cycle_id = str(uuid4())
        with self.connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO fetch_cycles (
                      id,
                      backend,
                      poll_started_at,
                      poll_finished_at,
                      success,
                      properties_last_update_date,
                      backend_version,
                      raw_status,
                      raw_properties
                    ) VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
                    """,
                    (
                        fetch_cycle_id,
                        backend,
                        poll_started_at,
                        poll_finished_at,
                        parsed_properties.properties_last_update_date,
                        parsed_properties.backend_version or status_snapshot.backend_version,
                        Jsonb(raw_status),
                        Jsonb(raw_properties),
                    ),
                )
                self._insert_status(conn, fetch_cycle_id, status_snapshot)
                self._insert_qubit_properties(
                    conn, fetch_cycle_id, parsed_properties.qubit_properties
                )
                self._insert_gate_properties(conn, fetch_cycle_id, parsed_properties.gate_properties)
                self._insert_metrics(conn, fetch_cycle_id, metrics)
        return fetch_cycle_id

    def insert_failed_cycle(
        self,
        *,
        backend: str,
        poll_started_at: datetime,
        poll_finished_at: datetime,
        error_message: str,
    ) -> str:
        fetch_cycle_id = str(uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fetch_cycles (
                  id, backend, poll_started_at, poll_finished_at, success, error_message
                ) VALUES (%s, %s, %s, %s, FALSE, %s)
                """,
                (fetch_cycle_id, backend, poll_started_at, poll_finished_at, error_message[:5000]),
            )
            conn.commit()
        return fetch_cycle_id

    @staticmethod
    def _insert_status(
        conn: Connection[Any],
        fetch_cycle_id: str,
        status: BackendStatusSnapshot,
    ) -> None:
        conn.execute(
            """
            INSERT INTO backend_status_snapshots (
              fetch_cycle_id, backend, poll_timestamp_utc, backend_version,
              pending_jobs, operational, status_msg, raw
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fetch_cycle_id,
                status.backend,
                status.poll_timestamp_utc,
                status.backend_version,
                status.pending_jobs,
                status.operational,
                status.status_msg,
                Jsonb(status.raw),
            ),
        )

    @staticmethod
    def _insert_qubit_properties(
        conn: Connection[Any],
        fetch_cycle_id: str,
        rows: list[QubitPropertySnapshot],
    ) -> None:
        if not rows:
            return
        values = [
            (
                fetch_cycle_id,
                r.backend,
                r.poll_timestamp_utc,
                r.properties_last_update_date,
                r.qubit,
                r.property_name,
                r.value,
                r.unit,
                r.property_date,
                Jsonb(r.raw_property),
            )
            for r in rows
        ]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO qubit_property_snapshots (
                  fetch_cycle_id, backend, poll_timestamp_utc, properties_last_update_date,
                  qubit, property_name, value, unit, property_date, raw_property
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                values,
            )

    @staticmethod
    def _insert_gate_properties(
        conn: Connection[Any],
        fetch_cycle_id: str,
        rows: list[GatePropertySnapshot],
    ) -> None:
        if not rows:
            return
        values = [
            (
                fetch_cycle_id,
                r.backend,
                r.poll_timestamp_utc,
                r.properties_last_update_date,
                r.gate_name,
                r.qubits,
                r.qubits_key,
                r.edge_id,
                r.parameter_name,
                r.value,
                r.unit,
                r.property_date,
                Jsonb(r.raw_parameter),
            )
            for r in rows
        ]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO gate_property_snapshots (
                  fetch_cycle_id, backend, poll_timestamp_utc, properties_last_update_date,
                  gate_name, qubits, qubits_key, edge_id, parameter_name,
                  value, unit, property_date, raw_parameter
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                values,
            )

    @staticmethod
    def _insert_metrics(
        conn: Connection[Any],
        fetch_cycle_id: str,
        m: FetchCycleMetrics,
    ) -> None:
        conn.execute(
            """
            INSERT INTO fetch_cycle_metrics (
              fetch_cycle_id,
              backend,
              poll_timestamp_utc,
              prev_fetch_cycle_id,
              prev_poll_timestamp_utc,
              num_qubits_calibrated_since_last_fetch,
              num_edges_calibrated_since_last_fetch,
              qubits_calibrated_since_last_fetch,
              edges_calibrated_since_last_fetch,
              max_qubit_calibration_age_seconds,
              max_edge_calibration_age_seconds,
              oldest_qubit,
              oldest_qubit_calibration_timestamp,
              oldest_edge_id,
              oldest_edge_calibration_timestamp,
              qubit_latest_calibration_dates,
              edge_latest_calibration_dates
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                fetch_cycle_id,
                m.backend,
                m.poll_timestamp_utc,
                m.prev_fetch_cycle_id,
                m.prev_poll_timestamp_utc,
                m.num_qubits_calibrated_since_last_fetch,
                m.num_edges_calibrated_since_last_fetch,
                m.qubits_calibrated_since_last_fetch,
                m.edges_calibrated_since_last_fetch,
                m.max_qubit_calibration_age_seconds,
                m.max_edge_calibration_age_seconds,
                m.oldest_qubit,
                m.oldest_qubit_calibration_timestamp,
                m.oldest_edge_id,
                m.oldest_edge_calibration_timestamp,
                Jsonb(m.qubit_latest_calibration_dates),
                Jsonb(m.edge_latest_calibration_dates),
            ),
        )
