from __future__ import annotations

import math

import pandas as pd

from ibm_calibration_collector.analysis import run_analysis
from ibm_calibration_collector.analysis_metrics import (
    build_calibration_events,
    compute_backend_snapshot_drift,
    compute_calibration_concentration,
    compute_component_frequency,
    compute_property_drift,
    compute_timing_discrepancy,
    compute_two_way_fixed_effect_regressions,
    make_evaluation_grid,
    summarize_device_load,
)


def _observations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "backend": "ibm_test",
                "component_type": "qubit",
                "component_id": "0",
                "property_key": "T1[s]",
                "property_name": "T1",
                "unit": "s",
                "calibration_timestamp_utc": "2026-01-01T00:00:00Z",
                "observed_at_utc": "2026-01-01T00:05:00Z",
                "value": 100.0,
            },
            {
                "backend": "ibm_test",
                "component_type": "qubit",
                "component_id": "0",
                "property_key": "T1[s]",
                "property_name": "T1",
                "unit": "s",
                "calibration_timestamp_utc": "2026-01-01T01:00:00Z",
                "observed_at_utc": "2026-01-01T01:05:00Z",
                "value": 110.0,
            },
            {
                "backend": "ibm_test",
                "component_type": "qubit",
                "component_id": "0",
                "property_key": "T1[s]",
                "property_name": "T1",
                "unit": "s",
                "calibration_timestamp_utc": "2026-01-01T02:00:00Z",
                "observed_at_utc": "2026-01-01T02:05:00Z",
                "value": 121.0,
            },
            {
                "backend": "ibm_test",
                "component_type": "qubit",
                "component_id": "1",
                "property_key": "T1[s]",
                "property_name": "T1",
                "unit": "s",
                "calibration_timestamp_utc": "2026-01-01T00:00:00Z",
                "observed_at_utc": "2026-01-01T00:05:00Z",
                "value": 100.0,
            },
        ]
    )


def _coverage() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "backend": "ibm_test",
                "study_start_utc": pd.Timestamp("2026-01-01T00:00:00Z"),
                "study_end_utc": pd.Timestamp("2026-01-01T03:00:00Z"),
                "successful_fetch_count": 13,
            }
        ]
    )


def test_property_drift_is_symmetric_and_dimensionless() -> None:
    drift = compute_property_drift(_observations())
    q0 = drift.loc[drift["component_id"] == "0"].sort_values(
        "calibration_timestamp_utc"
    )

    assert math.isnan(q0.iloc[0]["drift_score"])
    assert q0.iloc[1]["transform"] == "log"
    assert math.isclose(q0.iloc[1]["symmetric_relative_change"], 2 * 10 / 210)
    assert q0.iloc[1]["drift_score"] > 0
    assert math.isclose(q0.iloc[1]["drift_score"], q0.iloc[2]["drift_score"])
    assert q0.iloc[1]["quality_direction"] == "higher_is_better"
    assert bool(q0.iloc[1]["is_degradation"]) is False


def test_anchor_based_event_grouping_does_not_chain_indefinitely() -> None:
    base = compute_property_drift(_observations().iloc[[0]].copy())
    rows = pd.concat(
        [
            base.assign(
                calibration_timestamp_utc=pd.Timestamp("2026-01-01T00:00:00Z"),
                property_key="a",
            ),
            base.assign(
                calibration_timestamp_utc=pd.Timestamp("2026-01-01T00:10:00Z"),
                property_key="b",
            ),
            base.assign(
                calibration_timestamp_utc=pd.Timestamp("2026-01-01T00:20:00Z"),
                property_key="c",
            ),
        ],
        ignore_index=True,
    )
    events = build_calibration_events(rows, tolerance_minutes=15)

    assert len(events) == 2
    assert events.iloc[0]["updated_property_count"] == 2
    assert events.iloc[1]["updated_property_count"] == 1


def test_frequency_concentration_and_age_discrepancy() -> None:
    events = build_calibration_events(
        compute_property_drift(_observations()), tolerance_minutes=0
    )
    grid = make_evaluation_grid(_coverage(), grid_minutes=60)
    frequency = compute_component_frequency(
        events, grid, _coverage(), windows_hours=(1, 2)
    )

    q0_at_2h = frequency.loc[
        (frequency["component_id"] == "0")
        & (frequency["evaluation_timestamp_utc"] == pd.Timestamp("2026-01-01T02:00:00Z"))
    ].iloc[0]
    assert q0_at_2h["calibration_event_count_2h"] == 2
    assert bool(q0_at_2h["window_complete_2h"])

    concentration = compute_calibration_concentration(frequency, windows_hours=(1, 2))
    at_2h = concentration.loc[
        (concentration["evaluation_timestamp_utc"] == pd.Timestamp("2026-01-01T02:00:00Z"))
        & (concentration["window_hours"] == 2)
    ].iloc[0]
    assert at_2h["calibration_coverage_fraction"] == 0.5
    assert at_2h["normalized_hhi"] == 1.0

    discrepancy = compute_timing_discrepancy(events, grid, stale_thresholds_hours=(1, 2))
    age_at_2h = discrepancy.loc[
        discrepancy["evaluation_timestamp_utc"] == pd.Timestamp("2026-01-01T02:00:00Z")
    ].iloc[0]
    assert age_at_2h["known_last_calibration_count"] == 2
    assert age_at_2h["mean_pairwise_age_gap_hours"] == 2.0


def test_load_windows_are_strictly_prior() -> None:
    targets = pd.DataFrame(
        [
            {
                "backend": "ibm_test",
                "evaluation_timestamp_utc": pd.Timestamp("2026-01-01T02:00:00Z"),
            }
        ]
    )
    status = pd.DataFrame(
        [
            {
                "backend": "ibm_test",
                "status_timestamp_utc": pd.Timestamp("2026-01-01T00:00:00Z"),
                "pending_jobs": 1,
            },
            {
                "backend": "ibm_test",
                "status_timestamp_utc": pd.Timestamp("2026-01-01T01:00:00Z"),
                "pending_jobs": 3,
            },
            {
                "backend": "ibm_test",
                "status_timestamp_utc": pd.Timestamp("2026-01-01T02:00:00Z"),
                "pending_jobs": 99,
            },
        ]
    )
    result = summarize_device_load(
        targets,
        status,
        target_timestamp_column="evaluation_timestamp_utc",
        windows_hours=(2,),
    ).iloc[0]

    assert result["pending_jobs_current"] == 99
    assert result["pending_jobs_mean_previous_2h"] == 2
    assert result["pending_jobs_samples_previous_2h"] == 2


def test_end_to_end_analysis_writes_auditable_outputs(tmp_path) -> None:
    status = pd.DataFrame(
        [
            {
                "backend": "ibm_test",
                "status_timestamp_utc": pd.Timestamp(f"2026-01-01T0{hour}:00:00Z"),
                "pending_jobs": hour,
                "operational": True,
                "status_name": "active",
                "status_msg": "Ready",
            }
            for hour in range(4)
        ]
    )
    metadata = run_analysis(
        observations=_observations(),
        status=status,
        coverage=_coverage(),
        output_dir=tmp_path,
        windows_hours=(1, 2),
        lags_hours=(-1, 0, 1),
        event_tolerance_minutes=0,
        grid_minutes=60,
        min_correlation_n=2,
    )

    assert metadata["row_counts"]["property_drift"] == 4
    assert (tmp_path / "property_drift.csv.gz").is_file()
    assert (tmp_path / "component_frequency.csv.gz").is_file()
    assert (tmp_path / "backend_snapshot_drift.csv.gz").is_file()
    assert (tmp_path / "fixed_effect_regressions.csv.gz").is_file()
    assert (tmp_path / "property_staleness.csv.gz").is_file()
    assert (tmp_path / "run_metadata.json").is_file()


def test_snapshot_drift_and_two_way_fixed_effect_model() -> None:
    drift = compute_property_drift(_observations())
    grid = make_evaluation_grid(_coverage(), grid_minutes=60)
    snapshot = compute_backend_snapshot_drift(drift, grid, grid_minutes=60)
    at_1h = snapshot.loc[
        snapshot["evaluation_timestamp_utc"] == pd.Timestamp("2026-01-01T01:00:00Z")
    ].iloc[0]
    assert at_1h["changed_property_series_count"] == 1
    assert at_1h["device_drift_l2"] > 0

    panel_rows = []
    for backend, backend_effect in (("a", 10.0), ("b", -5.0), ("c", 2.0)):
        for time_index in range(4):
            timestamp = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(
                hours=time_index
            )
            interaction = (ord(backend) - ord("a") + 1) * (time_index + 1)
            time_effect = time_index * 3.0
            panel_rows.append(
                {
                    "backend": backend,
                    "component_type": "qubit",
                    "evaluation_timestamp_utc": timestamp,
                    "load": interaction,
                    "drift": backend_effect + time_effect + 2.0 * interaction,
                }
            )
    regression = compute_two_way_fixed_effect_regressions(
        pd.DataFrame(panel_rows),
        subgroup_columns=["component_type"],
        outcome_columns=["drift"],
        predictor_columns=["load"],
        min_n=8,
    ).iloc[0]
    assert math.isclose(regression["coefficient"], 2.0, rel_tol=1e-9)
