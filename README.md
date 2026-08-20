# IBM Calibration Collector

Collects IBM Quantum backend status and backend properties into PostgreSQL without running quantum jobs.

It stores:

- backend status snapshots, including `pending_jobs`
- raw backend properties JSON
- per-qubit property rows, including value/unit/timestamp
- per-gate/per-edge property rows, including value/unit/timestamp
- per-qubit and per-gate operational snapshots, including whether IBM explicitly reported the flag
- an edge-level operational view that also accounts for the two endpoint qubits
- one derived metrics row per backend per fetch cycle

The derived metrics include the six requested fields:

1. `num_qubits_calibrated_since_last_fetch`
2. `num_edges_calibrated_since_last_fetch`
3. `qubits_calibrated_since_last_fetch`
4. `edges_calibrated_since_last_fetch`
5. `max_qubit_calibration_age_seconds`
6. `max_edge_calibration_age_seconds`

## Definitions used

### Qubit calibrated since last fetch

For each qubit, the collector computes:

```text
latest_qubit_calibration_time(q) = max(
    property_date for all qubit properties on q,
    property_date for all one-qubit gate parameters on q
)
```

A qubit is counted as calibrated since the previous fetch if its current latest timestamp is greater than the latest timestamp stored for that qubit in the previous successful fetch.

On the first fetch for a backend, the collector has no baseline, so the calibrated-since-last-fetch lists are empty.

### Edge calibrated since last fetch

For each two-qubit edge, the collector computes:

```text
latest_edge_calibration_time(e) = max(property_date for all two-qubit gate parameters on e)
```

An edge is counted as calibrated since the previous fetch if its current latest timestamp is greater than the latest timestamp stored for that edge in the previous successful fetch.

By default, edge IDs are undirected and canonicalized as `min-max`, e.g. `12-13`. Set `EDGE_ID_MODE=directed` if you want `12-13` and `13-12` to be treated separately.

### Maximum calibration age

For qubits:

```text
max_qubit_calibration_age_seconds = now - min_q(latest_qubit_calibration_time(q))
```

This identifies the qubit whose latest observed property timestamp is oldest.

For edges:

```text
max_edge_calibration_age_seconds = now - min_e(latest_edge_calibration_time(e))
```

The `now` used is the fetch-cycle completion timestamp, stored as `poll_timestamp_utc` in `fetch_cycle_metrics`.

### Qubit, gate, and edge operational state

IBM reports operational state on individual qubits and gate instructions. The collector writes
one row per qubit to `qubit_operational_snapshots` and one row per gate instruction to
`gate_operational_snapshots` on every successful fetch, including gates with no numeric
calibration parameters.

Each row distinguishes three related concepts:

- `operational_reported`: IBM's explicit value, or `NULL` when the property was absent.
- `operational_is_explicit`: whether IBM actually supplied the value.
- `operational_effective`: the value used by Qiskit's backend-properties semantics, where an
  absent operational property defaults to `TRUE`.

The `edge_operational_snapshots` view aggregates all two-qubit gate instructions sharing the
same physical `edge_id`. It exposes both `any_gate_operational_effective` and
`all_gates_operational_effective`. Its convenience field `edge_operational_effective` is true
when at least one two-qubit instruction is operational and both endpoint qubits are operational.
Use `edge_operational_is_fully_explicit` to distinguish a fully reported state from one that
depends on the missing-value default.

Operational properties are retained in the raw/generic property data for auditability, but they
are excluded from calibration-frequency and numeric-drift calculations because an availability
flag is not a calibration-quality measurement.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your IBM API key, service CRN, backend list, and database URL.

Start Postgres:

```bash
docker compose up -d postgres
```

Load environment variables:

```bash
set -a
source .env
set +a
```

Run one collection cycle and initialize the schema:

```bash
python -m ibm_calibration_collector.collector --init-db --once
```

Apply the schema without collecting:

```bash
python -m ibm_calibration_collector.collector --init-db-only
```

Run continuously:

```bash
python -m ibm_calibration_collector.collector --init-db
```

Or use the console script after installing the package:

```bash
pip install -e .
ibm-calibration-collector --init-db --once
```

## Useful queries

Latest derived metrics:

```sql
SELECT *
FROM fetch_cycle_metrics
ORDER BY poll_timestamp_utc DESC
LIMIT 20;
```

Latest status snapshots:

```sql
SELECT backend, poll_timestamp_utc, pending_jobs, operational, status_msg
FROM backend_status_snapshots
ORDER BY poll_timestamp_utc DESC;
```

Find which qubits have the oldest latest calibration timestamp in a fetch:

```sql
SELECT backend, poll_timestamp_utc, oldest_qubit,
       oldest_qubit_calibration_timestamp,
       max_qubit_calibration_age_seconds
FROM fetch_cycle_metrics
ORDER BY poll_timestamp_utc DESC;
```

Latest CZ/ECR gate-error timestamps by edge:

```sql
SELECT backend, edge_id, gate_name, parameter_name, value, unit, property_date
FROM gate_property_snapshots
WHERE parameter_name = 'gate_error'
  AND gate_name IN ('cz', 'ecr', 'rzz')
ORDER BY poll_timestamp_utc DESC, edge_id;
```

Latest gate-level operational state for two-qubit instructions:

```sql
SELECT backend, poll_timestamp_utc, edge_id, gate_name, qubits,
       operational_reported, operational_effective, operational_is_explicit
FROM gate_operational_snapshots
WHERE edge_id IS NOT NULL
ORDER BY poll_timestamp_utc DESC, backend, edge_id, gate_name;
```

Latest physical-edge operational state:

```sql
SELECT DISTINCT ON (backend, edge_id)
       backend, poll_timestamp_utc, edge_id,
       edge_operational_effective, edge_operational_is_fully_explicit,
       any_gate_operational_effective, all_gates_operational_effective,
       all_endpoint_qubits_operational_effective
FROM edge_operational_snapshots
ORDER BY backend, edge_id, poll_timestamp_utc DESC;
```

Interval-level view useful for later regression work:

```sql
SELECT *
FROM calibration_interval_observations
WHERE interval_start IS NOT NULL
ORDER BY backend, interval_end;
```

## Notes

This collector does not submit quantum jobs. It only calls IBM Quantum Runtime REST endpoints for backend status and backend properties.

The operational snapshot tables begin filling only after this schema and collector version are
deployed. Existing property rows are not implicitly rewritten.

Queue length is stored as `pending_jobs` and should be interpreted as a demand-pressure proxy, not as direct physical QPU utilization.

## Offline calibration/load analysis

Install the analysis dependencies:

```bash
python -m pip install -r requirements-analysis.txt
```

Run the full analysis against PostgreSQL/RDS (credentials remain in the environment):

```bash
export DATABASE_URL='postgresql://...'
ibm-calibration-analyze \
  --output-dir output/analysis \
  --windows-hours 1,2,8,24,48
```

Useful filters and sensitivity controls include:

```bash
ibm-calibration-analyze \
  --backends ibm_fez,ibm_torino \
  --start 2026-01-01T00:00:00Z \
  --end 2026-06-30T23:59:59Z \
  --event-tolerance-minutes 15 \
  --grid-minutes 60
```

The command writes compressed CSVs for property drift, property volatility, grouped
calibration events, component frequency, concentration, component and property staleness,
long-run cadence, calibration synchrony, backend calibration-vector drift, queue pressure,
fixed-effects regressions, aligned event/load observations, and correlation families. Both
native qubit properties and one-qubit gate parameters (`sx`, `x`, `id`, and similar gates)
are included in qubit analysis. It also writes `run_metadata.json` with study coverage and
important interpretation warnings.

The formal definitions and statistical caveats are in
[`docs/metric_definitions.md`](docs/metric_definitions.md). Most importantly, the stored
`pending_jobs` field is a backend queue-pressure proxy. The database does not contain
physical-qubit/edge job placement, shots, or execution duration, so it cannot establish
qubit- or edge-level utilization.

## Backfilling historical fetch-cycle metrics

Historical raw one-qubit gate rows are already present, but older `fetch_cycle_metrics`
rows did not use them when calculating qubit freshness. The idempotent backfill also
quarantines historical IBM error payloads that were incorrectly marked successful.

Deploy the corrected collector first so new rows use the corrected definition. The recommended
backfill is restart-safe and can run while the corrected collector continues writing: it freezes
its input at a durable cutoff and processes only rows at or before that cutoff.

Prepare the supporting indexes first. They are built concurrently so collector writes can
continue, and an interrupted invalid index is repaired on the next run:

```bash
python -m ibm_calibration_collector.backfill --prepare-only
```

Start the backfill with 500 fetch cycles per transaction:

```bash
python -m ibm_calibration_collector.backfill --batch-size 500
```

Each metrics batch and its progress cursor commit atomically. If the process or connection stops,
run the same command again and it resumes the newest incomplete run. Only the active batch is
rolled back. To inspect progress:

```bash
python -m ibm_calibration_collector.backfill --status
```

For a short validation run, commit one batch and stop cleanly:

```bash
python -m ibm_calibration_collector.backfill --batch-size 100 --max-batches 1
```

The next normal invocation resumes that run using its original batch size. Use `--run-id UUID`
to select a particular incomplete run, or `--new-run` only when intentionally reconstructing the
entire frozen data set again. A database advisory lock prevents concurrent backfill processes.

Invalid historical error payloads are marked unsuccessful in the initialization transaction,
their misleading metrics rows are removed, and their previous state is retained in
`fetch_cycle_metrics_backfill_audit`. Valid metrics are rebuilt from raw qubit properties,
one-qubit gate parameters, and two-qubit edge parameters.

The older all-at-once SQL remains available at `sql/backfill_fetch_cycle_metrics.sql` for a
transactional dry run on small databases. It is not recommended for the production RDS data set.

### Running detached on Heroku

The command reads `DATABASE_URL` inside the dyno, so credentials do not need to be placed in the
command line:

```bash
heroku run:detached --app qurator-calibration-collector \
  python -m ibm_calibration_collector.backfill --batch-size 500
```

Find the detached dyno and follow its output:

```bash
heroku ps --app qurator-calibration-collector
heroku logs --tail --app qurator-calibration-collector --dyno run.N
```

If Heroku cycles the dyno, submit the same detached command again; it resumes from the last
committed batch.

The `Procfile` includes a Heroku release phase that applies `sql/schema.sql` before the updated
collector process starts. The migration is idempotent and the new operational tables are empty,
so deploying this version does not rewrite the existing multi-million-row property tables.
