CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS fetch_cycles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  backend TEXT NOT NULL,
  poll_started_at TIMESTAMPTZ NOT NULL,
  poll_finished_at TIMESTAMPTZ,
  success BOOLEAN NOT NULL DEFAULT FALSE,
  error_message TEXT,
  properties_last_update_date TIMESTAMPTZ,
  backend_version TEXT,
  raw_backend_summary JSONB,
  raw_status JSONB,
  raw_properties JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE fetch_cycles
  ADD COLUMN IF NOT EXISTS raw_backend_summary JSONB;

CREATE INDEX IF NOT EXISTS idx_fetch_cycles_backend_started
  ON fetch_cycles (backend, poll_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_fetch_cycles_backend_success_started
  ON fetch_cycles (backend, success, poll_started_at DESC);

CREATE TABLE IF NOT EXISTS backend_status_snapshots (
  id BIGSERIAL PRIMARY KEY,
  fetch_cycle_id UUID NOT NULL REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  backend_version TEXT,
  pending_jobs INTEGER,
  operational BOOLEAN,
  status_name TEXT,
  status_msg TEXT,
  raw JSONB NOT NULL,
  raw_backend JSONB
);

ALTER TABLE backend_status_snapshots
  ADD COLUMN IF NOT EXISTS status_name TEXT;

ALTER TABLE backend_status_snapshots
  ADD COLUMN IF NOT EXISTS raw_backend JSONB;

CREATE INDEX IF NOT EXISTS idx_backend_status_backend_poll
  ON backend_status_snapshots (backend, poll_timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS qubit_property_snapshots (
  id BIGSERIAL PRIMARY KEY,
  fetch_cycle_id UUID NOT NULL REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  properties_last_update_date TIMESTAMPTZ,
  qubit INTEGER NOT NULL,
  property_name TEXT NOT NULL,
  value DOUBLE PRECISION,
  unit TEXT,
  property_date TIMESTAMPTZ,
  raw_property JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qubit_props_backend_poll
  ON qubit_property_snapshots (backend, poll_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_qubit_props_backend_qubit_name_date
  ON qubit_property_snapshots (backend, qubit, property_name, property_date DESC);

-- These indexes make historical metrics reconstruction proportional to the selected
-- fetch-cycle batch instead of requiring a full snapshot-table scan for every batch.
CREATE INDEX IF NOT EXISTS idx_qubit_props_fetch_cycle_calibration
  ON qubit_property_snapshots (fetch_cycle_id, qubit, property_date DESC)
  WHERE property_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS gate_property_snapshots (
  id BIGSERIAL PRIMARY KEY,
  fetch_cycle_id UUID NOT NULL REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  properties_last_update_date TIMESTAMPTZ,
  gate_name TEXT NOT NULL,
  qubits INTEGER[] NOT NULL,
  qubits_key TEXT NOT NULL,
  edge_id TEXT,
  parameter_name TEXT NOT NULL,
  value DOUBLE PRECISION,
  unit TEXT,
  property_date TIMESTAMPTZ,
  raw_parameter JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_props_backend_poll
  ON gate_property_snapshots (backend, poll_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_gate_props_backend_edge_param_date
  ON gate_property_snapshots (backend, edge_id, parameter_name, property_date DESC);

CREATE INDEX IF NOT EXISTS idx_gate_props_fetch_cycle_one_qubit_calibration
  ON gate_property_snapshots (fetch_cycle_id, (qubits[1]), property_date DESC)
  WHERE cardinality(qubits) = 1 AND property_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_gate_props_fetch_cycle_edge_calibration
  ON gate_property_snapshots (fetch_cycle_id, edge_id, property_date DESC)
  WHERE edge_id IS NOT NULL AND property_date IS NOT NULL;

-- IBM exposes operational state as a property on individual qubits and gate
-- instructions. Keep the explicit API value separate from the Qiskit-compatible
-- effective value: when the property is absent, Qiskit treats the component as
-- operational, while operational_is_explicit remains false here.
CREATE TABLE IF NOT EXISTS qubit_operational_snapshots (
  id BIGSERIAL PRIMARY KEY,
  fetch_cycle_id UUID NOT NULL REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  qubit INTEGER NOT NULL,
  operational_reported BOOLEAN,
  operational_effective BOOLEAN NOT NULL,
  operational_is_explicit BOOLEAN NOT NULL,
  operational_property_date TIMESTAMPTZ,
  raw_operational_property JSONB,
  CHECK (operational_is_explicit = (operational_reported IS NOT NULL)),
  CHECK (operational_effective = COALESCE(operational_reported, TRUE))
);

CREATE INDEX IF NOT EXISTS idx_qubit_operational_backend_qubit_poll
  ON qubit_operational_snapshots (backend, qubit, poll_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_qubit_operational_fetch_cycle
  ON qubit_operational_snapshots (fetch_cycle_id, qubit);

CREATE TABLE IF NOT EXISTS gate_operational_snapshots (
  id BIGSERIAL PRIMARY KEY,
  fetch_cycle_id UUID NOT NULL REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  gate_name TEXT NOT NULL,
  qubits INTEGER[] NOT NULL,
  qubits_key TEXT NOT NULL,
  edge_id TEXT,
  operational_reported BOOLEAN,
  operational_effective BOOLEAN NOT NULL,
  operational_is_explicit BOOLEAN NOT NULL,
  operational_property_date TIMESTAMPTZ,
  raw_gate JSONB NOT NULL,
  raw_operational_parameter JSONB,
  CHECK (operational_is_explicit = (operational_reported IS NOT NULL)),
  CHECK (operational_effective = COALESCE(operational_reported, TRUE))
);

CREATE INDEX IF NOT EXISTS idx_gate_operational_backend_gate_poll
  ON gate_operational_snapshots (backend, gate_name, qubits_key, poll_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_gate_operational_backend_edge_poll
  ON gate_operational_snapshots (backend, edge_id, poll_timestamp_utc DESC)
  WHERE edge_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fetch_cycle_metrics (
  fetch_cycle_id UUID PRIMARY KEY REFERENCES fetch_cycles(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  poll_timestamp_utc TIMESTAMPTZ NOT NULL,
  prev_fetch_cycle_id UUID REFERENCES fetch_cycles(id) ON DELETE SET NULL,
  prev_poll_timestamp_utc TIMESTAMPTZ,

  -- User-requested six pieces of derived information.
  num_qubits_calibrated_since_last_fetch INTEGER,
  num_edges_calibrated_since_last_fetch INTEGER,
  qubits_calibrated_since_last_fetch INTEGER[] NOT NULL DEFAULT '{}',
  edges_calibrated_since_last_fetch TEXT[] NOT NULL DEFAULT '{}',
  max_qubit_calibration_age_seconds DOUBLE PRECISION,
  max_edge_calibration_age_seconds DOUBLE PRECISION,

  -- Extra diagnostic columns: these identify which component produced the max age.
  oldest_qubit INTEGER,
  oldest_qubit_calibration_timestamp TIMESTAMPTZ,
  oldest_edge_id TEXT,
  oldest_edge_calibration_timestamp TIMESTAMPTZ,

  -- Raw per-component latest timestamps used for reproducibility/debugging.
  qubit_latest_calibration_dates JSONB NOT NULL DEFAULT '{}'::jsonb,
  edge_latest_calibration_dates JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fetch_cycle_metrics_backend_poll
  ON fetch_cycle_metrics (backend, poll_timestamp_utc DESC);

-- Durable state for the restart-safe historical metrics backfill. A progress cursor is
-- committed in the same transaction as each metrics batch.
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

-- Handy interval-level view for downstream analysis.
CREATE OR REPLACE VIEW calibration_interval_observations AS
SELECT
  m.backend,
  m.prev_poll_timestamp_utc AS interval_start,
  m.poll_timestamp_utc AS interval_end,
  EXTRACT(EPOCH FROM (m.poll_timestamp_utc - m.prev_poll_timestamp_utc)) AS interval_seconds,
  m.num_qubits_calibrated_since_last_fetch,
  m.num_edges_calibrated_since_last_fetch,
  m.qubits_calibrated_since_last_fetch,
  m.edges_calibrated_since_last_fetch,
  m.max_qubit_calibration_age_seconds,
  m.max_edge_calibration_age_seconds,
  s.pending_jobs,
  s.operational,
  s.status_msg,
  f.properties_last_update_date,
  s.status_name
FROM fetch_cycle_metrics m
JOIN fetch_cycles f ON f.id = m.fetch_cycle_id
LEFT JOIN backend_status_snapshots s ON s.fetch_cycle_id = m.fetch_cycle_id;

-- A physical edge can have multiple directed/native two-qubit gate instructions.
-- This view preserves both optimistic (any instruction works) and strict (all work)
-- gate aggregates, and combines the optimistic aggregate with endpoint-qubit state.
CREATE OR REPLACE VIEW edge_operational_snapshots AS
WITH edge_gates AS (
  SELECT
    fetch_cycle_id,
    backend,
    poll_timestamp_utc,
    edge_id,
    bool_or(operational_effective) AS any_gate_operational_effective,
    bool_and(operational_effective) AS all_gates_operational_effective,
    bool_and(operational_is_explicit) AS all_gate_statuses_explicit,
    count(*) AS gate_count,
    count(*) FILTER (WHERE operational_is_explicit) AS explicit_gate_status_count,
    jsonb_agg(
      jsonb_build_object(
        'gate_name', gate_name,
        'qubits', qubits,
        'qubits_key', qubits_key,
        'operational_reported', operational_reported,
        'operational_effective', operational_effective,
        'operational_is_explicit', operational_is_explicit,
        'operational_property_date', operational_property_date
      ) ORDER BY gate_name, qubits_key
    ) AS gate_statuses
  FROM gate_operational_snapshots
  WHERE edge_id IS NOT NULL
  GROUP BY fetch_cycle_id, backend, poll_timestamp_utc, edge_id
),
edge_endpoints AS (
  SELECT DISTINCT
    gate.fetch_cycle_id,
    gate.backend,
    gate.poll_timestamp_utc,
    gate.edge_id,
    endpoint.qubit
  FROM gate_operational_snapshots gate
  CROSS JOIN LATERAL unnest(gate.qubits) AS endpoint(qubit)
  WHERE gate.edge_id IS NOT NULL
),
endpoint_status AS (
  SELECT
    endpoint.fetch_cycle_id,
    endpoint.backend,
    endpoint.poll_timestamp_utc,
    endpoint.edge_id,
    count(*) AS endpoint_count,
    count(*) FILTER (WHERE qubit.operational_is_explicit) AS explicit_endpoint_status_count,
    bool_and(COALESCE(qubit.operational_effective, TRUE))
      AS all_endpoint_qubits_operational_effective,
    bool_and(COALESCE(qubit.operational_is_explicit, FALSE))
      AS all_endpoint_statuses_explicit,
    jsonb_agg(
      jsonb_build_object(
        'qubit', endpoint.qubit,
        'operational_reported', qubit.operational_reported,
        'operational_effective', COALESCE(qubit.operational_effective, TRUE),
        'operational_is_explicit', COALESCE(qubit.operational_is_explicit, FALSE),
        'operational_property_date', qubit.operational_property_date
      ) ORDER BY endpoint.qubit
    ) AS endpoint_statuses
  FROM edge_endpoints endpoint
  LEFT JOIN qubit_operational_snapshots qubit
    ON qubit.fetch_cycle_id = endpoint.fetch_cycle_id
   AND qubit.qubit = endpoint.qubit
  GROUP BY
    endpoint.fetch_cycle_id,
    endpoint.backend,
    endpoint.poll_timestamp_utc,
    endpoint.edge_id
)
SELECT
  gates.fetch_cycle_id,
  gates.backend,
  gates.poll_timestamp_utc,
  gates.edge_id,
  gates.any_gate_operational_effective,
  gates.all_gates_operational_effective,
  endpoints.all_endpoint_qubits_operational_effective,
  (
    gates.any_gate_operational_effective
    AND endpoints.all_endpoint_qubits_operational_effective
  ) AS edge_operational_effective,
  (
    gates.all_gate_statuses_explicit
    AND endpoints.all_endpoint_statuses_explicit
  ) AS edge_operational_is_fully_explicit,
  gates.gate_count,
  gates.explicit_gate_status_count,
  endpoints.endpoint_count,
  endpoints.explicit_endpoint_status_count,
  gates.gate_statuses,
  endpoints.endpoint_statuses
FROM edge_gates gates
JOIN endpoint_status endpoints USING (
  fetch_cycle_id,
  backend,
  poll_timestamp_utc,
  edge_id
);
