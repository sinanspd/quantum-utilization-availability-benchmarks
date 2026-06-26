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
