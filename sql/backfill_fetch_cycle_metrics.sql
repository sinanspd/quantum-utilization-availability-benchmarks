\set ON_ERROR_STOP 1

-- Safe by default:
--   psql "$DATABASE_URL" -f sql/backfill_fetch_cycle_metrics.sql
-- rolls the transaction back after printing the proposed counts.
-- To persist:
--   psql "$DATABASE_URL" -v apply=1 -f sql/backfill_fetch_cycle_metrics.sql

\if :{?apply}
\else
  \set apply 0
\endif

BEGIN;
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL lock_timeout = '10s';

CREATE TEMP TABLE backfill_context ON COMMIT DROP AS
SELECT gen_random_uuid() AS run_id, clock_timestamp() AS started_at;

-- Only invalid cycles need an audit copy. Corrected metrics remain reproducible from the
-- immutable raw property rows, so duplicating every large JSON timestamp map is unnecessary.
CREATE TABLE IF NOT EXISTS fetch_cycle_metrics_backfill_audit (
  run_id UUID NOT NULL,
  backed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetch_cycle_id UUID NOT NULL,
  reason TEXT NOT NULL,
  fetch_cycle_before JSONB NOT NULL,
  metrics_before JSONB,
  PRIMARY KEY (run_id, fetch_cycle_id)
);

CREATE TEMP TABLE invalid_fetch_cycles ON COMMIT DROP AS
SELECT
  f.id AS fetch_cycle_id,
  CASE
    WHEN f.raw_properties IS NULL THEN 'raw_properties is null'
    WHEN jsonb_typeof(f.raw_properties) <> 'object' THEN 'raw_properties is not an object'
    WHEN NOT (f.raw_properties ? 'qubits') OR NOT (f.raw_properties ? 'gates')
      THEN 'raw_properties is missing required qubits/gates fields'
    ELSE 'raw_properties contains a non-empty errors value'
  END AS reason
FROM fetch_cycles f
WHERE f.success = TRUE
  AND (
    f.raw_properties IS NULL
    OR jsonb_typeof(f.raw_properties) <> 'object'
    OR NOT (f.raw_properties ? 'qubits')
    OR NOT (f.raw_properties ? 'gates')
    OR CASE jsonb_typeof(f.raw_properties -> 'errors')
      WHEN 'array' THEN jsonb_array_length(f.raw_properties -> 'errors') > 0
      WHEN 'object' THEN f.raw_properties -> 'errors' <> '{}'::jsonb
      WHEN 'string' THEN f.raw_properties ->> 'errors' <> ''
      WHEN 'boolean' THEN (f.raw_properties ->> 'errors')::boolean
      WHEN 'number' THEN (f.raw_properties ->> 'errors')::numeric <> 0
      ELSE FALSE
    END
  );

INSERT INTO fetch_cycle_metrics_backfill_audit (
  run_id,
  fetch_cycle_id,
  reason,
  fetch_cycle_before,
  metrics_before
)
SELECT
  context.run_id,
  invalid.fetch_cycle_id,
  invalid.reason,
  to_jsonb(cycle),
  CASE WHEN metrics.fetch_cycle_id IS NULL THEN NULL ELSE to_jsonb(metrics) END
FROM invalid_fetch_cycles invalid
CROSS JOIN backfill_context context
JOIN fetch_cycles cycle ON cycle.id = invalid.fetch_cycle_id
LEFT JOIN fetch_cycle_metrics metrics ON metrics.fetch_cycle_id = invalid.fetch_cycle_id;

UPDATE fetch_cycles cycle
SET
  success = FALSE,
  error_message = concat_ws(
    ' | ',
    NULLIF(cycle.error_message, ''),
    'Historical backfill rejected properties payload: ' || invalid.reason
  )
FROM invalid_fetch_cycles invalid
WHERE cycle.id = invalid.fetch_cycle_id;

DELETE FROM fetch_cycle_metrics metrics
USING invalid_fetch_cycles invalid
WHERE metrics.fetch_cycle_id = invalid.fetch_cycle_id;

-- The previous baseline is the previous valid successful fetch, not an API-error payload.
CREATE TEMP TABLE valid_fetch_cycles ON COMMIT DROP AS
SELECT
  cycle.id AS fetch_cycle_id,
  cycle.backend,
  cycle.poll_started_at,
  COALESCE(cycle.poll_finished_at, cycle.poll_started_at) AS poll_timestamp_utc,
  lag(cycle.id) OVER (
    PARTITION BY cycle.backend ORDER BY cycle.poll_started_at, cycle.id
  ) AS prev_fetch_cycle_id,
  lag(COALESCE(cycle.poll_finished_at, cycle.poll_started_at)) OVER (
    PARTITION BY cycle.backend ORDER BY cycle.poll_started_at, cycle.id
  ) AS prev_poll_timestamp_utc
FROM fetch_cycles cycle
WHERE cycle.success = TRUE;

CREATE UNIQUE INDEX ON valid_fetch_cycles (fetch_cycle_id);
CREATE INDEX ON valid_fetch_cycles (backend, poll_started_at);

-- A qubit calibration date includes native qubit properties and every one-qubit gate
-- parameter (sx, x, id, or any other gate with cardinality(qubits) = 1).
CREATE TEMP TABLE backfill_qubit_latest ON COMMIT DROP AS
SELECT
  dates.fetch_cycle_id,
  dates.qubit,
  max(dates.property_date) AS latest_date
FROM (
  SELECT snapshot.fetch_cycle_id, snapshot.qubit, snapshot.property_date
  FROM qubit_property_snapshots snapshot
  JOIN valid_fetch_cycles valid ON valid.fetch_cycle_id = snapshot.fetch_cycle_id
  WHERE lower(snapshot.property_name) <> 'operational'
    AND snapshot.property_date IS NOT NULL

  UNION ALL

  SELECT snapshot.fetch_cycle_id, snapshot.qubits[1] AS qubit, snapshot.property_date
  FROM gate_property_snapshots snapshot
  JOIN valid_fetch_cycles valid ON valid.fetch_cycle_id = snapshot.fetch_cycle_id
  WHERE cardinality(snapshot.qubits) = 1
    AND lower(snapshot.parameter_name) <> 'operational'
    AND snapshot.property_date IS NOT NULL
) dates
GROUP BY dates.fetch_cycle_id, dates.qubit;

CREATE UNIQUE INDEX ON backfill_qubit_latest (fetch_cycle_id, qubit);

CREATE TEMP TABLE backfill_edge_latest ON COMMIT DROP AS
SELECT
  snapshot.fetch_cycle_id,
  snapshot.edge_id,
  max(snapshot.property_date) AS latest_date
FROM gate_property_snapshots snapshot
JOIN valid_fetch_cycles valid ON valid.fetch_cycle_id = snapshot.fetch_cycle_id
WHERE snapshot.edge_id IS NOT NULL
  AND lower(snapshot.parameter_name) <> 'operational'
  AND snapshot.property_date IS NOT NULL
GROUP BY snapshot.fetch_cycle_id, snapshot.edge_id;

CREATE UNIQUE INDEX ON backfill_edge_latest (fetch_cycle_id, edge_id);

CREATE TEMP TABLE backfill_qubit_summary ON COMMIT DROP AS
SELECT
  valid.fetch_cycle_id,
  (array_agg(latest.qubit ORDER BY latest.latest_date, latest.qubit)
    FILTER (WHERE latest.qubit IS NOT NULL))[1] AS oldest_qubit,
  min(latest.latest_date) AS oldest_qubit_calibration_timestamp,
  CASE
    WHEN min(latest.latest_date) IS NULL THEN NULL
    ELSE greatest(
      0.0,
      extract(epoch FROM (valid.poll_timestamp_utc - min(latest.latest_date)))
    )
  END::double precision AS max_qubit_calibration_age_seconds,
  COALESCE(
    jsonb_object_agg(
      latest.qubit::text,
      to_jsonb(
        to_char(
          latest.latest_date AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00'
      )
    ) FILTER (WHERE latest.qubit IS NOT NULL),
    '{}'::jsonb
  ) AS qubit_latest_calibration_dates
FROM valid_fetch_cycles valid
LEFT JOIN backfill_qubit_latest latest ON latest.fetch_cycle_id = valid.fetch_cycle_id
GROUP BY valid.fetch_cycle_id, valid.poll_timestamp_utc;

CREATE UNIQUE INDEX ON backfill_qubit_summary (fetch_cycle_id);

CREATE TEMP TABLE backfill_edge_summary ON COMMIT DROP AS
SELECT
  valid.fetch_cycle_id,
  (array_agg(latest.edge_id ORDER BY latest.latest_date, latest.edge_id)
    FILTER (WHERE latest.edge_id IS NOT NULL))[1] AS oldest_edge_id,
  min(latest.latest_date) AS oldest_edge_calibration_timestamp,
  CASE
    WHEN min(latest.latest_date) IS NULL THEN NULL
    ELSE greatest(
      0.0,
      extract(epoch FROM (valid.poll_timestamp_utc - min(latest.latest_date)))
    )
  END::double precision AS max_edge_calibration_age_seconds,
  COALESCE(
    jsonb_object_agg(
      latest.edge_id,
      to_jsonb(
        to_char(
          latest.latest_date AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00'
      )
    ) FILTER (WHERE latest.edge_id IS NOT NULL),
    '{}'::jsonb
  ) AS edge_latest_calibration_dates
FROM valid_fetch_cycles valid
LEFT JOIN backfill_edge_latest latest ON latest.fetch_cycle_id = valid.fetch_cycle_id
GROUP BY valid.fetch_cycle_id, valid.poll_timestamp_utc;

CREATE UNIQUE INDEX ON backfill_edge_summary (fetch_cycle_id);

CREATE TEMP TABLE backfill_qubit_changes ON COMMIT DROP AS
SELECT
  valid.fetch_cycle_id,
  COALESCE(
    array_agg(current.qubit ORDER BY current.qubit)
      FILTER (WHERE previous.latest_date IS NOT NULL
              AND current.latest_date > previous.latest_date),
    ARRAY[]::integer[]
  ) AS calibrated_qubits
FROM valid_fetch_cycles valid
LEFT JOIN backfill_qubit_latest current
  ON current.fetch_cycle_id = valid.fetch_cycle_id
LEFT JOIN backfill_qubit_latest previous
  ON previous.fetch_cycle_id = valid.prev_fetch_cycle_id
 AND previous.qubit = current.qubit
GROUP BY valid.fetch_cycle_id;

CREATE UNIQUE INDEX ON backfill_qubit_changes (fetch_cycle_id);

CREATE TEMP TABLE backfill_edge_changes ON COMMIT DROP AS
SELECT
  valid.fetch_cycle_id,
  COALESCE(
    array_agg(current.edge_id ORDER BY current.edge_id)
      FILTER (WHERE previous.latest_date IS NOT NULL
              AND current.latest_date > previous.latest_date),
    ARRAY[]::text[]
  ) AS calibrated_edges
FROM valid_fetch_cycles valid
LEFT JOIN backfill_edge_latest current
  ON current.fetch_cycle_id = valid.fetch_cycle_id
LEFT JOIN backfill_edge_latest previous
  ON previous.fetch_cycle_id = valid.prev_fetch_cycle_id
 AND previous.edge_id = current.edge_id
GROUP BY valid.fetch_cycle_id;

CREATE UNIQUE INDEX ON backfill_edge_changes (fetch_cycle_id);

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
)
SELECT
  valid.fetch_cycle_id,
  valid.backend,
  valid.poll_timestamp_utc,
  valid.prev_fetch_cycle_id,
  valid.prev_poll_timestamp_utc,
  cardinality(qubit_changes.calibrated_qubits),
  cardinality(edge_changes.calibrated_edges),
  qubit_changes.calibrated_qubits,
  edge_changes.calibrated_edges,
  qubit_summary.max_qubit_calibration_age_seconds,
  edge_summary.max_edge_calibration_age_seconds,
  qubit_summary.oldest_qubit,
  qubit_summary.oldest_qubit_calibration_timestamp,
  edge_summary.oldest_edge_id,
  edge_summary.oldest_edge_calibration_timestamp,
  qubit_summary.qubit_latest_calibration_dates,
  edge_summary.edge_latest_calibration_dates
FROM valid_fetch_cycles valid
JOIN backfill_qubit_changes qubit_changes
  ON qubit_changes.fetch_cycle_id = valid.fetch_cycle_id
JOIN backfill_edge_changes edge_changes
  ON edge_changes.fetch_cycle_id = valid.fetch_cycle_id
JOIN backfill_qubit_summary qubit_summary
  ON qubit_summary.fetch_cycle_id = valid.fetch_cycle_id
JOIN backfill_edge_summary edge_summary
  ON edge_summary.fetch_cycle_id = valid.fetch_cycle_id
ON CONFLICT (fetch_cycle_id) DO UPDATE SET
  backend = EXCLUDED.backend,
  poll_timestamp_utc = EXCLUDED.poll_timestamp_utc,
  prev_fetch_cycle_id = EXCLUDED.prev_fetch_cycle_id,
  prev_poll_timestamp_utc = EXCLUDED.prev_poll_timestamp_utc,
  num_qubits_calibrated_since_last_fetch =
    EXCLUDED.num_qubits_calibrated_since_last_fetch,
  num_edges_calibrated_since_last_fetch =
    EXCLUDED.num_edges_calibrated_since_last_fetch,
  qubits_calibrated_since_last_fetch =
    EXCLUDED.qubits_calibrated_since_last_fetch,
  edges_calibrated_since_last_fetch =
    EXCLUDED.edges_calibrated_since_last_fetch,
  max_qubit_calibration_age_seconds =
    EXCLUDED.max_qubit_calibration_age_seconds,
  max_edge_calibration_age_seconds =
    EXCLUDED.max_edge_calibration_age_seconds,
  oldest_qubit = EXCLUDED.oldest_qubit,
  oldest_qubit_calibration_timestamp =
    EXCLUDED.oldest_qubit_calibration_timestamp,
  oldest_edge_id = EXCLUDED.oldest_edge_id,
  oldest_edge_calibration_timestamp =
    EXCLUDED.oldest_edge_calibration_timestamp,
  qubit_latest_calibration_dates =
    EXCLUDED.qubit_latest_calibration_dates,
  edge_latest_calibration_dates =
    EXCLUDED.edge_latest_calibration_dates;

SELECT
  context.run_id,
  context.started_at,
  (SELECT count(*) FROM invalid_fetch_cycles) AS invalid_cycles_quarantined,
  (SELECT count(*) FROM valid_fetch_cycles) AS valid_cycles_recomputed,
  (SELECT count(*) FROM backfill_qubit_latest) AS qubit_cycle_summaries,
  (SELECT count(*) FROM backfill_edge_latest) AS edge_cycle_summaries
FROM backfill_context context;

\if :apply
  COMMIT;
  \echo 'Backfill committed.'
\else
  ROLLBACK;
  \echo 'Dry run only: all changes rolled back. Re-run with -v apply=1 to commit.'
\endif
