from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from .analysis_metrics import (
    DEFAULT_LAGS_HOURS,
    DEFAULT_WINDOWS_HOURS,
    build_calibration_events,
    compute_backend_snapshot_drift,
    compute_calibration_concentration,
    compute_component_cadence,
    compute_component_frequency,
    compute_component_property_summary,
    compute_correlations,
    compute_grid_outcomes,
    compute_lagged_correlations,
    compute_property_staleness,
    compute_property_drift,
    compute_synchrony_episodes,
    compute_timing_discrepancy,
    compute_two_way_fixed_effect_regressions,
    make_evaluation_grid,
    summarize_device_load,
)

LOG = logging.getLogger("ibm_calibration_collector.analysis")


def load_analysis_data(
    database_url: str,
    *,
    backends: list[str] | None,
    start: datetime | None,
    end: datetime | None,
    baseline_lookback_days: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load server-side-deduplicated calibration values, status, and study coverage."""
    common_filters: list[str] = []
    params: list[Any] = []
    if backends:
        common_filters.append("backend = ANY(%s)")
        params.append(backends)
    if end:
        common_filters.append("poll_timestamp_utc <= %s")
        params.append(end)
    if start:
        common_filters.append("poll_timestamp_utc >= %s")
        params.append(start - timedelta(days=baseline_lookback_days))
    where = " AND ".join(common_filters) if common_filters else "TRUE"

    qubit_query = f"""
        SELECT DISTINCT ON (backend, qubit, property_name, COALESCE(unit, ''), property_date)
          backend,
          'qubit'::text AS component_type,
          qubit::text AS component_id,
          property_name,
          property_name || '[' || COALESCE(unit, '') || ']' AS property_key,
          property_name || '[' || COALESCE(unit, '') || ']' AS scale_property_key,
          unit,
          property_date AS calibration_timestamp_utc,
          value,
          poll_timestamp_utc AS observed_at_utc
        FROM qubit_property_snapshots
        WHERE {where}
          AND property_date IS NOT NULL
          AND value IS NOT NULL
        ORDER BY backend, qubit, property_name, COALESCE(unit, ''), property_date,
                 poll_timestamp_utc DESC
    """
    gate_query = f"""
        SELECT DISTINCT ON (
          backend, edge_id, gate_name, qubits_key, parameter_name, COALESCE(unit, ''), property_date
        )
          backend,
          'edge'::text AS component_type,
          edge_id::text AS component_id,
          gate_name || ':' || parameter_name AS property_name,
          gate_name || '@' || qubits_key || ':' || parameter_name ||
            '[' || COALESCE(unit, '') || ']' AS property_key,
          gate_name || ':' || parameter_name ||
            '[' || COALESCE(unit, '') || ']' AS scale_property_key,
          unit,
          property_date AS calibration_timestamp_utc,
          value,
          poll_timestamp_utc AS observed_at_utc
        FROM gate_property_snapshots
        WHERE {where}
          AND edge_id IS NOT NULL
          AND property_date IS NOT NULL
          AND value IS NOT NULL
        ORDER BY backend, edge_id, gate_name, qubits_key, parameter_name,
                 COALESCE(unit, ''), property_date, poll_timestamp_utc DESC
    """
    single_qubit_gate_query = f"""
        SELECT DISTINCT ON (
          backend, qubits_key, gate_name, parameter_name, COALESCE(unit, ''), property_date
        )
          backend,
          'qubit'::text AS component_type,
          qubits[1]::text AS component_id,
          gate_name || ':' || parameter_name AS property_name,
          gate_name || '@' || qubits_key || ':' || parameter_name ||
            '[' || COALESCE(unit, '') || ']' AS property_key,
          gate_name || ':' || parameter_name ||
            '[' || COALESCE(unit, '') || ']' AS scale_property_key,
          unit,
          property_date AS calibration_timestamp_utc,
          value,
          poll_timestamp_utc AS observed_at_utc
        FROM gate_property_snapshots
        WHERE {where}
          AND cardinality(qubits) = 1
          AND property_date IS NOT NULL
          AND value IS NOT NULL
        ORDER BY backend, qubits_key, gate_name, parameter_name,
                 COALESCE(unit, ''), property_date, poll_timestamp_utc DESC
    """
    status_filters: list[str] = []
    status_params: list[Any] = []
    if backends:
        status_filters.append("backend = ANY(%s)")
        status_params.append(backends)
    if start:
        status_filters.append("poll_timestamp_utc >= %s")
        status_params.append(start)
    if end:
        status_filters.append("poll_timestamp_utc <= %s")
        status_params.append(end)
    status_where = " AND ".join(status_filters) if status_filters else "TRUE"
    status_query = f"""
        SELECT backend,
               poll_timestamp_utc AS status_timestamp_utc,
               pending_jobs,
               operational,
               status_name,
               status_msg
        FROM backend_status_snapshots
        WHERE {status_where}
        ORDER BY backend, poll_timestamp_utc
    """
    coverage_filters = ["success = TRUE"]
    coverage_params: list[Any] = []
    if backends:
        coverage_filters.append("backend = ANY(%s)")
        coverage_params.append(backends)
    if start:
        coverage_filters.append("COALESCE(poll_finished_at, poll_started_at) >= %s")
        coverage_params.append(start)
    if end:
        coverage_filters.append("poll_started_at <= %s")
        coverage_params.append(end)
    coverage_query = f"""
        SELECT backend,
               min(poll_started_at) AS study_start_utc,
               max(COALESCE(poll_finished_at, poll_started_at)) AS study_end_utc,
               count(*) AS successful_fetch_count
        FROM fetch_cycles
        WHERE {' AND '.join(coverage_filters)}
        GROUP BY backend
        ORDER BY backend
    """

    LOG.info("loading deduplicated qubit, one-qubit gate, and edge observations")
    with psycopg.connect(database_url) as connection:
        qubits = _read_frame(connection, qubit_query, params)
        single_qubit_gates = _read_frame(connection, single_qubit_gate_query, params)
        edges = _read_frame(connection, gate_query, params)
        status = _read_frame(connection, status_query, status_params)
        coverage = _read_frame(connection, coverage_query, coverage_params)
    observations = pd.concat([qubits, single_qubit_gates, edges], ignore_index=True)
    for column in ("calibration_timestamp_utc", "observed_at_utc"):
        observations[column] = pd.to_datetime(observations[column], utc=True)
    status["status_timestamp_utc"] = pd.to_datetime(status["status_timestamp_utc"], utc=True)
    coverage["study_start_utc"] = pd.to_datetime(coverage["study_start_utc"], utc=True)
    coverage["study_end_utc"] = pd.to_datetime(coverage["study_end_utc"], utc=True)
    return observations, status, coverage


def _read_frame(
    connection: psycopg.Connection[Any],
    query: str,
    params: list[Any],
) -> pd.DataFrame:
    """Read a query through psycopg without requiring SQLAlchemy."""
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [column.name for column in cursor.description or ()]
    return pd.DataFrame(rows, columns=columns)


def run_analysis(
    *,
    observations: pd.DataFrame,
    status: pd.DataFrame,
    coverage: pd.DataFrame,
    output_dir: Path,
    windows_hours: tuple[int, ...] = DEFAULT_WINDOWS_HOURS,
    lags_hours: tuple[int, ...] = DEFAULT_LAGS_HOURS,
    event_tolerance_minutes: int = 15,
    grid_minutes: int = 60,
    max_load_staleness_minutes: int = 120,
    min_correlation_n: int = 8,
) -> dict[str, Any]:
    if observations.empty:
        raise ValueError("no calibration property observations matched the requested study period")
    if coverage.empty:
        raise ValueError("no successful fetch-cycle coverage matched the requested study period")
    output_dir.mkdir(parents=True, exist_ok=True)

    property_drift = compute_property_drift(observations)
    all_events = build_calibration_events(
        property_drift, tolerance_minutes=event_tolerance_minutes
    )
    all_events = _mark_events_in_study(all_events, coverage)
    study_events = all_events.loc[all_events["in_study_period"]].copy()
    grid = make_evaluation_grid(coverage, grid_minutes=grid_minutes)
    if grid.empty:
        raise ValueError("study coverage is shorter than one evaluation-grid interval")

    frequency = compute_component_frequency(
        all_events, grid, coverage, windows_hours=windows_hours
    )
    concentration = compute_calibration_concentration(
        frequency, windows_hours=windows_hours
    )
    discrepancy = compute_timing_discrepancy(
        all_events, grid, stale_thresholds_hours=windows_hours
    )
    cadence = compute_component_cadence(all_events, coverage)
    component_property_summary = compute_component_property_summary(
        property_drift,
        coverage,
    )
    property_staleness = compute_property_staleness(
        property_drift,
        grid,
        thresholds_hours=tuple(sorted(set(windows_hours) | {6})),
    )
    synchrony = compute_synchrony_episodes(
        all_events,
        coverage,
        tolerance_minutes=event_tolerance_minutes,
    )
    device_load = summarize_device_load(
        grid,
        status,
        target_timestamp_column="evaluation_timestamp_utc",
        windows_hours=windows_hours,
        max_staleness_minutes=max_load_staleness_minutes,
    )
    backend_snapshot_drift = compute_backend_snapshot_drift(
        property_drift,
        grid,
        grid_minutes=grid_minutes,
    )
    backend_snapshot_drift_with_load = backend_snapshot_drift.merge(
        device_load,
        on=["backend", "evaluation_timestamp_utc"],
        how="left",
        validate="many_to_one",
    )
    fixed_effect_predictors = ["pending_jobs_current"] + [
        f"pending_jobs_mean_previous_{window}h" for window in windows_hours
    ]
    fixed_effect_regressions = compute_two_way_fixed_effect_regressions(
        backend_snapshot_drift_with_load,
        subgroup_columns=["component_type"],
        outcome_columns=[
            "device_drift_l2",
            "device_drift_rms_all_properties",
            "changed_component_count",
            "changed_property_series_fraction",
        ],
        predictor_columns=fixed_effect_predictors,
        entity_column="backend",
        time_column="evaluation_timestamp_utc",
        min_n=20,
    )
    event_load = summarize_device_load(
        study_events,
        status,
        target_timestamp_column="event_timestamp_utc",
        windows_hours=windows_hours,
        max_staleness_minutes=max_load_staleness_minutes,
    )
    grid_outcomes = compute_grid_outcomes(
        study_events,
        grid,
        grid_minutes=grid_minutes,
    )
    grid_outcomes_with_load = grid_outcomes.merge(
        device_load,
        on=["backend", "evaluation_timestamp_utc"],
        how="left",
        validate="many_to_one",
    )

    load_predictors = ["pending_jobs_current"] + [
        f"pending_jobs_mean_previous_{window}h" for window in windows_hours
    ]
    event_correlations_backend = compute_correlations(
        event_load,
        group_columns=["backend", "component_type"],
        outcome_columns=["drift_rms", "drift_mean", "drift_max", "symmetric_relative_drift_mean"],
        predictor_columns=load_predictors,
        min_n=min_correlation_n,
    )
    event_correlations_backend.insert(2, "scope", "backend_component_type")
    event_correlations_component = compute_correlations(
        event_load,
        group_columns=["backend", "component_type", "component_id"],
        outcome_columns=["drift_rms", "symmetric_relative_drift_mean"],
        predictor_columns=load_predictors,
        min_n=min_correlation_n,
    )
    event_correlations_component.insert(3, "scope", "component")
    event_correlations = pd.concat(
        [event_correlations_backend, event_correlations_component], ignore_index=True
    )

    frequency_with_load = frequency.merge(
        device_load,
        on=["backend", "evaluation_timestamp_utc"],
        how="left",
        validate="many_to_one",
    )
    frequency_correlation_rows: list[pd.DataFrame] = []
    for window in windows_hours:
        complete = frequency_with_load.loc[
            frequency_with_load[f"window_complete_{window}h"]
        ].copy()
        if complete.empty:
            continue
        complete["calibration_event_count"] = complete[
            f"calibration_event_count_{window}h"
        ]
        complete["calibration_events_per_hour"] = complete[
            f"calibration_events_per_hour_{window}h"
        ]
        complete["matched_window_queue_mean"] = complete[
            f"pending_jobs_mean_previous_{window}h"
        ]
        correlations = compute_correlations(
            complete,
            group_columns=["backend", "component_type", "component_id"],
            outcome_columns=["calibration_event_count", "calibration_events_per_hour"],
            predictor_columns=["matched_window_queue_mean"],
            min_n=min_correlation_n,
        )
        if correlations.empty:
            continue
        correlations.insert(3, "window_hours", window)
        frequency_correlation_rows.append(correlations)
    frequency_correlations = (
        pd.concat(frequency_correlation_rows, ignore_index=True)
        if frequency_correlation_rows
        else pd.DataFrame()
    )
    lagged_correlations = compute_lagged_correlations(
        grid_outcomes_with_load,
        grid_minutes=grid_minutes,
        lags_hours=lags_hours,
        min_n=min_correlation_n,
    )
    component_property_with_frequency = component_property_summary.merge(
        cadence[
            [
                "backend",
                "component_type",
                "component_id",
                "calibration_event_count",
                "calibration_events_per_day",
            ]
        ],
        on=["backend", "component_type", "component_id"],
        how="left",
        validate="many_to_one",
    )
    frequency_property_correlations = compute_correlations(
        component_property_with_frequency,
        group_columns=["backend", "component_type", "property_name", "unit"],
        outcome_columns=[
            "value_median",
            "value_std",
            "drift_rms",
            "symmetric_relative_drift_median",
        ],
        predictor_columns=["calibration_events_per_day"],
        min_n=min_correlation_n,
    )

    outputs = {
        "property_drift": property_drift,
        "component_property_summary": component_property_summary,
        "calibration_events": all_events,
        "component_frequency": frequency,
        "calibration_concentration": concentration,
        "calibration_timing_discrepancy": discrepancy,
        "property_staleness": property_staleness,
        "component_cadence": cadence,
        "calibration_synchrony_episodes": synchrony,
        "device_queue_load": device_load,
        "event_load_alignment": event_load,
        "device_grid_outcomes": grid_outcomes_with_load,
        "backend_snapshot_drift": backend_snapshot_drift_with_load,
        "fixed_effect_regressions": fixed_effect_regressions,
        "event_load_correlations": event_correlations,
        "component_frequency_load_correlations": frequency_correlations,
        "frequency_property_correlations": frequency_property_correlations,
        "lagged_device_correlations": lagged_correlations,
    }
    for name, frame in outputs.items():
        path = output_dir / f"{name}.csv.gz"
        frame.to_csv(path, index=False, compression="gzip", date_format="%Y-%m-%dT%H:%M:%S.%fZ")
        LOG.info("wrote %s rows to %s", len(frame), path)

    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "windows_hours": list(windows_hours),
        "lags_hours_load_leads": list(lags_hours),
        "event_tolerance_minutes": event_tolerance_minutes,
        "grid_minutes": grid_minutes,
        "max_load_staleness_minutes": max_load_staleness_minutes,
        "minimum_correlation_sample_size": min_correlation_n,
        "coverage": _json_records(coverage),
        "row_counts": {name: len(frame) for name, frame in outputs.items()},
        "load_interpretation": (
            "pending_jobs is a backend queue/demand-pressure proxy; it is not physical QPU "
            "utilization and does not identify qubit- or edge-level load"
        ),
        "correlation_warning": (
            "Associations are observational. Use lag results, multiplicity-adjusted q-values, "
            "the emitted two-way fixed-effects estimates, and blocked-bootstrap sensitivity "
            "analysis; do not interpret queue coefficients as causal physical utilization effects"
        ),
    }
    (output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def _mark_events_in_study(events: pd.DataFrame, coverage: pd.DataFrame) -> pd.DataFrame:
    bounds = coverage[["backend", "study_start_utc", "study_end_utc"]]
    marked = events.merge(bounds, on="backend", how="left", validate="many_to_one")
    marked["in_study_period"] = (
        (marked["event_timestamp_utc"] >= marked["study_start_utc"])
        & (marked["event_timestamp_utc"] <= marked["study_end_utc"])
    )
    return marked


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = frame.to_dict(orient="records")
    for record in records:
        for key, value in list(record.items()):
            if isinstance(value, pd.Timestamp):
                record[key] = value.isoformat()
    return records


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_int_tuple(value: str) -> tuple[int, ...]:
    parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not parsed:
        raise argparse.ArgumentTypeError("expected a comma-separated list of integers")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compute component calibration drift/cadence metrics and their association with "
            "the stored backend queue-pressure proxy."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="PostgreSQL URL; defaults to DATABASE_URL. Never place it in source control.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/analysis"),
        help="Directory for compressed CSV outputs and run_metadata.json.",
    )
    parser.add_argument("--backends", help="Optional comma-separated backend names.")
    parser.add_argument("--start", help="Inclusive UTC study start (ISO 8601).")
    parser.add_argument("--end", help="Inclusive UTC study end (ISO 8601).")
    parser.add_argument(
        "--windows-hours",
        type=_parse_int_tuple,
        default=DEFAULT_WINDOWS_HOURS,
        help="Rolling calibration/load windows (default: 1,2,8,24,48).",
    )
    parser.add_argument(
        "--lags-hours",
        type=_parse_int_tuple,
        default=DEFAULT_LAGS_HOURS,
        help="Cross-correlation lags; positive means load leads outcome.",
    )
    parser.add_argument("--event-tolerance-minutes", type=int, default=15)
    parser.add_argument("--grid-minutes", type=int, default=60)
    parser.add_argument("--max-load-staleness-minutes", type=int, default=120)
    parser.add_argument("--baseline-lookback-days", type=int, default=7)
    parser.add_argument("--min-correlation-n", type=int, default=8)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    backends = (
        [backend.strip() for backend in args.backends.split(",") if backend.strip()]
        if args.backends
        else None
    )
    observations, status, coverage = load_analysis_data(
        args.database_url,
        backends=backends,
        start=_parse_datetime(args.start),
        end=_parse_datetime(args.end),
        baseline_lookback_days=args.baseline_lookback_days,
    )
    run_analysis(
        observations=observations,
        status=status,
        coverage=coverage,
        output_dir=args.output_dir,
        windows_hours=args.windows_hours,
        lags_hours=args.lags_hours,
        event_tolerance_minutes=args.event_tolerance_minutes,
        grid_minutes=args.grid_minutes,
        max_load_staleness_minutes=args.max_load_staleness_minutes,
        min_correlation_n=args.min_correlation_n,
    )


if __name__ == "__main__":
    main()
