# IBM Calibration Collector

Collects IBM Quantum backend status and backend properties into PostgreSQL without running quantum jobs.

It stores:

- backend status snapshots, including `pending_jobs`
- raw backend properties JSON
- per-qubit property rows, including value/unit/timestamp
- per-gate/per-edge property rows, including value/unit/timestamp
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

Interval-level view useful for later regression work:

```sql
SELECT *
FROM calibration_interval_observations
WHERE interval_start IS NOT NULL
ORDER BY backend, interval_end;
```

## Notes

This collector does not submit quantum jobs. It only calls IBM Quantum Runtime REST endpoints for backend status and backend properties.

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

Deploy the corrected collector first (or pause the old collector) so new rows are not written
with the old definition while the backfill runs.

Run a safe dry run first. This executes the complete reconstruction and then rolls it back:

```bash
psql "$DATABASE_URL" -X -f sql/backfill_fetch_cycle_metrics.sql
```

Review the printed invalid/valid cycle counts. To commit the same backfill:

```bash
psql "$DATABASE_URL" -X -v apply=1 -f sql/backfill_fetch_cycle_metrics.sql
```

On commit, invalid fetches are marked unsuccessful, their misleading derived metrics rows
are removed, and their previous state is retained in
`fetch_cycle_metrics_backfill_audit`. Valid metrics are rebuilt from raw qubit properties,
one-qubit gate parameters, and two-qubit edge parameters. Re-running the script is safe.
