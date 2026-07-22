from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_WINDOWS_HOURS = (1, 2, 8, 24, 48)
DEFAULT_LAGS_HOURS = (-48, -24, -8, -2, -1, 0, 1, 2, 8, 24, 48)


def robust_scale(values: pd.Series) -> float:
    """Return a robust scale with deterministic fallbacks for degenerate samples."""
    clean = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    clean = clean[np.isfinite(clean)]
    if clean.size == 0:
        return math.nan
    median = float(np.median(clean))
    mad_scale = 1.4826 * float(np.median(np.abs(clean - median)))
    if mad_scale > 1e-12:
        return mad_scale
    if clean.size >= 2:
        q25, q75 = np.quantile(clean, [0.25, 0.75])
        iqr_scale = float(q75 - q25) / 1.349
        if iqr_scale > 1e-12:
            return iqr_scale
        std = float(np.std(clean, ddof=1))
        if std > 1e-12:
            return std
    magnitude_floor = max(abs(median) * 1e-9, 1e-12)
    return magnitude_floor


def compute_property_drift(observations: pd.DataFrame) -> pd.DataFrame:
    """Compute scale-free drift increments for deduplicated calibration properties."""
    required = {
        "backend",
        "component_type",
        "component_id",
        "property_key",
        "calibration_timestamp_utc",
        "value",
    }
    _require_columns(observations, required)
    data = observations.copy()
    data["calibration_timestamp_utc"] = pd.to_datetime(
        data["calibration_timestamp_utc"], utc=True
    )
    data["value"] = pd.to_numeric(data["value"], errors="coerce")
    if "scale_property_key" not in data:
        data["scale_property_key"] = data["property_key"]
    if "observed_at_utc" not in data:
        data["observed_at_utc"] = data["calibration_timestamp_utc"]
    data["observed_at_utc"] = pd.to_datetime(data["observed_at_utc"], utc=True)

    identity = ["backend", "component_type", "component_id", "property_key"]
    data = data.sort_values(identity + ["calibration_timestamp_utc", "observed_at_utc"])
    data = data.drop_duplicates(identity + ["calibration_timestamp_utc"], keep="last")
    grouped = data.groupby(identity, observed=True, sort=False)
    data["previous_value"] = grouped["value"].shift(1)
    data["previous_calibration_timestamp_utc"] = grouped[
        "calibration_timestamp_utc"
    ].shift(1)
    data["elapsed_hours"] = (
        data["calibration_timestamp_utc"]
        - data["previous_calibration_timestamp_utc"]
    ).dt.total_seconds() / 3600.0
    data["signed_absolute_change"] = data["value"] - data["previous_value"]
    data["absolute_change"] = data["signed_absolute_change"].abs()

    denominator = data["value"].abs() + data["previous_value"].abs()
    data["symmetric_relative_change"] = np.where(
        denominator > 0,
        2.0 * data["absolute_change"] / denominator,
        np.where(data["absolute_change"] == 0, 0.0, np.nan),
    )
    positive_pair = (data["value"] > 0) & (data["previous_value"] > 0)
    data["transform"] = np.where(positive_pair, "log", "identity")
    with np.errstate(divide="ignore", invalid="ignore"):
        log_change = np.log(data["value"]) - np.log(data["previous_value"])
    data["signed_transformed_change"] = np.where(
        positive_pair,
        log_change,
        data["signed_absolute_change"],
    )
    invalid_interval = data["elapsed_hours"].isna() | (data["elapsed_hours"] <= 0)
    data.loc[invalid_interval, "signed_transformed_change"] = np.nan

    scale_keys = ["backend", "component_type", "scale_property_key"]
    data["robust_property_scale"] = data.groupby(
        scale_keys, observed=True, sort=False
    )["signed_transformed_change"].transform(robust_scale)
    valid_scale = data["robust_property_scale"] > 0
    data["signed_drift_score"] = np.where(
        valid_scale,
        data["signed_transformed_change"] / data["robust_property_scale"],
        np.nan,
    )
    data["drift_score"] = data["signed_drift_score"].abs()
    data["quality_direction"] = data["property_name"].map(_quality_direction)
    data["adverse_drift_score"] = np.where(
        data["quality_direction"] == "lower_is_better",
        data["signed_drift_score"],
        np.where(
            data["quality_direction"] == "higher_is_better",
            -data["signed_drift_score"],
            np.nan,
        ),
    )
    data["is_degradation"] = pd.array(
        np.where(
            data["adverse_drift_score"].notna(),
            data["adverse_drift_score"] > 0,
            None,
        ),
        dtype="boolean",
    )
    data["is_improvement"] = pd.array(
        np.where(
            data["adverse_drift_score"].notna(),
            data["adverse_drift_score"] < 0,
            None,
        ),
        dtype="boolean",
    )
    data["drift_score_per_hour"] = np.where(
        data["elapsed_hours"] > 0,
        data["drift_score"] / data["elapsed_hours"],
        np.nan,
    )
    data["symmetric_relative_drift_per_hour"] = np.where(
        data["elapsed_hours"] > 0,
        data["symmetric_relative_change"] / data["elapsed_hours"],
        np.nan,
    )
    return data.reset_index(drop=True)


def build_calibration_events(
    property_drift: pd.DataFrame,
    *,
    tolerance_minutes: int,
) -> pd.DataFrame:
    """Group near-simultaneous property timestamps into component calibration events."""
    if tolerance_minutes < 0:
        raise ValueError("tolerance_minutes must be non-negative")
    required = {
        "backend",
        "component_type",
        "component_id",
        "property_key",
        "calibration_timestamp_utc",
        "drift_score",
    }
    _require_columns(property_drift, required)
    data = property_drift.copy()
    data["calibration_timestamp_utc"] = pd.to_datetime(
        data["calibration_timestamp_utc"], utc=True
    )
    component_keys = ["backend", "component_type", "component_id"]
    data = data.sort_values(component_keys + ["calibration_timestamp_utc"])
    data["component_event_number"] = -1
    tolerance = pd.Timedelta(minutes=tolerance_minutes)
    for _, index in data.groupby(component_keys, observed=True, sort=False).groups.items():
        ordered_index = list(index)
        times = data.loc[ordered_index, "calibration_timestamp_utc"].tolist()
        event_numbers: list[int] = []
        event_number = 0
        anchor = times[0]
        for timestamp in times:
            if timestamp - anchor > tolerance:
                event_number += 1
                anchor = timestamp
            event_numbers.append(event_number)
        data.loc[ordered_index, "component_event_number"] = event_numbers

    event_keys = component_keys + ["component_event_number"]
    events = data.groupby(event_keys, observed=True, sort=False).agg(
        event_start_utc=("calibration_timestamp_utc", "min"),
        event_end_utc=("calibration_timestamp_utc", "max"),
        updated_property_count=("property_key", "nunique"),
        drift_property_count=("drift_score", "count"),
        drift_mean=("drift_score", "mean"),
        drift_median=("drift_score", "median"),
        drift_max=("drift_score", "max"),
        signed_drift_mean=("signed_drift_score", "mean"),
        symmetric_relative_drift_mean=("symmetric_relative_change", "mean"),
        symmetric_relative_drift_max=("symmetric_relative_change", "max"),
        quality_property_count=("adverse_drift_score", "count"),
        adverse_drift_mean=("adverse_drift_score", "mean"),
        degraded_property_count=("is_degradation", "sum"),
        improved_property_count=("is_improvement", "sum"),
    ).reset_index()

    rms = data.groupby(event_keys, observed=True, sort=False)["drift_score"].apply(
        _root_mean_square
    )
    events = events.merge(rms.rename("drift_rms").reset_index(), on=event_keys, how="left")
    events["event_timestamp_utc"] = events["event_end_utc"]
    events["event_span_minutes"] = (
        events["event_end_utc"] - events["event_start_utc"]
    ).dt.total_seconds() / 60.0
    events["component_degraded"] = pd.array(
        np.where(
            events["quality_property_count"] > 0,
            events["adverse_drift_mean"] > 0,
            None,
        ),
        dtype="boolean",
    )
    events["component_improved"] = pd.array(
        np.where(
            events["quality_property_count"] > 0,
            events["adverse_drift_mean"] < 0,
            None,
        ),
        dtype="boolean",
    )
    events["event_id"] = (
        events["backend"].astype(str)
        + ":"
        + events["component_type"].astype(str)
        + ":"
        + events["component_id"].astype(str)
        + ":"
        + events["component_event_number"].astype(str)
    )
    return events.sort_values(
        ["backend", "component_type", "component_id", "event_timestamp_utc"]
    ).reset_index(drop=True)


def make_evaluation_grid(coverage: pd.DataFrame, *, grid_minutes: int) -> pd.DataFrame:
    if grid_minutes <= 0:
        raise ValueError("grid_minutes must be positive")
    _require_columns(coverage, {"backend", "study_start_utc", "study_end_utc"})
    rows: list[dict[str, object]] = []
    frequency = pd.Timedelta(minutes=grid_minutes)
    for row in coverage.itertuples(index=False):
        start = pd.Timestamp(row.study_start_utc).ceil(frequency)
        end = pd.Timestamp(row.study_end_utc).floor(frequency)
        if end < start:
            continue
        for timestamp in pd.date_range(start, end, freq=frequency):
            rows.append({"backend": row.backend, "evaluation_timestamp_utc": timestamp})
    return pd.DataFrame(rows, columns=["backend", "evaluation_timestamp_utc"])


def compute_component_frequency(
    events: pd.DataFrame,
    grid: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    windows_hours: Sequence[int] = DEFAULT_WINDOWS_HOURS,
) -> pd.DataFrame:
    windows = _validate_windows(windows_hours)
    _require_columns(
        events,
        {"backend", "component_type", "component_id", "event_timestamp_utc"},
    )
    _require_columns(grid, {"backend", "evaluation_timestamp_utc"})
    coverage_by_backend = coverage.set_index("backend")
    rows: list[dict[str, object]] = []
    universe = events[["backend", "component_type", "component_id"]].drop_duplicates()
    for component in universe.itertuples(index=False):
        backend_grid = grid.loc[
            grid["backend"] == component.backend, "evaluation_timestamp_utc"
        ].sort_values()
        component_events = events.loc[
            (events["backend"] == component.backend)
            & (events["component_type"] == component.component_type)
            & (events["component_id"] == component.component_id),
            "event_timestamp_utc",
        ].sort_values()
        timestamps = _timestamp_values_ns(component_events)
        study_start = pd.Timestamp(
            coverage_by_backend.loc[component.backend, "study_start_utc"]
        )
        for evaluation_time in backend_grid:
            evaluation_ns = evaluation_time.value
            right = int(np.searchsorted(timestamps, evaluation_ns, side="right"))
            observed_hours = max(
                0.0, (evaluation_time - study_start).total_seconds() / 3600.0
            )
            row: dict[str, object] = {
                "backend": component.backend,
                "component_type": component.component_type,
                "component_id": component.component_id,
                "evaluation_timestamp_utc": evaluation_time,
            }
            for window in windows:
                left_ns = (evaluation_time - pd.Timedelta(hours=window)).value
                left = int(np.searchsorted(timestamps, left_ns, side="right"))
                count = right - left
                exposure = min(float(window), observed_hours)
                row[f"calibration_event_count_{window}h"] = count
                row[f"calibration_events_per_hour_{window}h"] = (
                    count / exposure if exposure > 0 else math.nan
                )
                row[f"observed_hours_{window}h"] = exposure
                row[f"window_complete_{window}h"] = observed_hours >= window
            rows.append(row)
    return pd.DataFrame(rows)


def compute_calibration_concentration(
    frequency: pd.DataFrame,
    *,
    windows_hours: Sequence[int] = DEFAULT_WINDOWS_HOURS,
) -> pd.DataFrame:
    windows = _validate_windows(windows_hours)
    _require_columns(
        frequency,
        {
            "backend",
            "component_type",
            "evaluation_timestamp_utc",
            *(f"calibration_event_count_{window}h" for window in windows),
            *(f"window_complete_{window}h" for window in windows),
        },
    )
    group_keys = ["backend", "component_type", "evaluation_timestamp_utc"]
    rows: list[dict[str, object]] = []
    for keys, group in frequency.groupby(group_keys, observed=True, sort=False):
        for window in windows:
            counts = group[f"calibration_event_count_{window}h"].to_numpy(dtype=float)
            n = counts.size
            total = float(counts.sum())
            active = int(np.count_nonzero(counts))
            if total > 0:
                shares = counts / total
                hhi = float(np.sum(shares**2))
                normalized_hhi = (hhi - 1.0 / n) / (1.0 - 1.0 / n) if n > 1 else 1.0
                positive_shares = shares[shares > 0]
                entropy = float(-np.sum(positive_shares * np.log(positive_shares)))
                normalized_entropy = entropy / math.log(n) if n > 1 else 0.0
                effective_components = float(math.exp(entropy))
                top_component_share = float(shares.max())
                top_decile_count = max(1, int(math.ceil(n * 0.1)))
                top_decile_share = float(np.sort(shares)[-top_decile_count:].sum())
            else:
                hhi = normalized_hhi = normalized_entropy = effective_components = math.nan
                top_component_share = top_decile_share = math.nan
            rows.append(
                {
                    "backend": keys[0],
                    "component_type": keys[1],
                    "evaluation_timestamp_utc": keys[2],
                    "window_hours": window,
                    "component_count": n,
                    "total_calibration_events": int(total),
                    "active_component_count": active,
                    "calibration_coverage_fraction": active / n if n else math.nan,
                    "hhi": hhi,
                    "normalized_hhi": normalized_hhi,
                    "normalized_entropy": normalized_entropy,
                    "effective_component_count": effective_components,
                    "top_component_share": top_component_share,
                    "top_decile_share": top_decile_share,
                    "component_count_gini": _gini(counts),
                    "component_count_cv": _coefficient_of_variation(counts),
                    "window_complete": bool(group[f"window_complete_{window}h"].all()),
                }
            )
    return pd.DataFrame(rows)


def compute_timing_discrepancy(
    events: pd.DataFrame,
    grid: pd.DataFrame,
    *,
    stale_thresholds_hours: Sequence[int] = DEFAULT_WINDOWS_HOURS,
) -> pd.DataFrame:
    thresholds = _validate_windows(stale_thresholds_hours)
    _require_columns(
        events,
        {"backend", "component_type", "component_id", "event_timestamp_utc"},
    )
    rows: list[dict[str, object]] = []
    universe = events[["backend", "component_type", "component_id"]].drop_duplicates()
    for (backend, component_type), components in universe.groupby(
        ["backend", "component_type"], observed=True, sort=False
    ):
        backend_grid = grid.loc[
            grid["backend"] == backend, "evaluation_timestamp_utc"
        ].sort_values()
        event_arrays: list[np.ndarray] = []
        for component_id in components["component_id"]:
            times = events.loc[
                (events["backend"] == backend)
                & (events["component_type"] == component_type)
                & (events["component_id"] == component_id),
                "event_timestamp_utc",
            ].sort_values()
            event_arrays.append(_timestamp_values_ns(times))
        for evaluation_time in backend_grid:
            ages: list[float] = []
            for times in event_arrays:
                position = int(np.searchsorted(times, evaluation_time.value, side="right")) - 1
                if position >= 0:
                    age = (evaluation_time.value - int(times[position])) / 3.6e12
                    ages.append(max(0.0, age))
            age_array = np.asarray(ages, dtype=float)
            summary: dict[str, object] = {
                "backend": backend,
                "component_type": component_type,
                "evaluation_timestamp_utc": evaluation_time,
                "component_count": len(event_arrays),
                "known_last_calibration_count": age_array.size,
                "known_last_calibration_fraction": (
                    age_array.size / len(event_arrays) if event_arrays else math.nan
                ),
            }
            if age_array.size:
                q10, q25, q50, q75, q90 = np.quantile(age_array, [0.1, 0.25, 0.5, 0.75, 0.9])
                summary.update(
                    {
                        "age_mean_hours": float(np.mean(age_array)),
                        "age_median_hours": float(q50),
                        "age_std_hours": _sample_std(age_array),
                        "age_mad_hours": float(np.median(np.abs(age_array - q50))),
                        "age_iqr_hours": float(q75 - q25),
                        "age_p90_p10_hours": float(q90 - q10),
                        "age_range_hours": float(np.max(age_array) - np.min(age_array)),
                        "mean_pairwise_age_gap_hours": _mean_pairwise_absolute_difference(
                            age_array
                        ),
                        "age_gini": _gini(age_array),
                    }
                )
                for threshold in thresholds:
                    summary[f"fraction_age_gt_{threshold}h"] = float(
                        np.mean(age_array > threshold)
                    )
            else:
                summary.update(
                    {
                        "age_mean_hours": math.nan,
                        "age_median_hours": math.nan,
                        "age_std_hours": math.nan,
                        "age_mad_hours": math.nan,
                        "age_iqr_hours": math.nan,
                        "age_p90_p10_hours": math.nan,
                        "age_range_hours": math.nan,
                        "mean_pairwise_age_gap_hours": math.nan,
                        "age_gini": math.nan,
                    }
                )
                for threshold in thresholds:
                    summary[f"fraction_age_gt_{threshold}h"] = math.nan
            rows.append(summary)
    return pd.DataFrame(rows)


def compute_component_cadence(events: pd.DataFrame, coverage: pd.DataFrame) -> pd.DataFrame:
    _require_columns(
        events,
        {"backend", "component_type", "component_id", "event_timestamp_utc"},
    )
    coverage_by_backend = coverage.set_index("backend")
    rows: list[dict[str, object]] = []
    for keys, group in events.groupby(
        ["backend", "component_type", "component_id"], observed=True, sort=False
    ):
        backend, component_type, component_id = keys
        start = pd.Timestamp(coverage_by_backend.loc[backend, "study_start_utc"])
        end = pd.Timestamp(coverage_by_backend.loc[backend, "study_end_utc"])
        in_study = group.loc[
            (group["event_timestamp_utc"] >= start)
            & (group["event_timestamp_utc"] <= end),
            "event_timestamp_utc",
        ].sort_values()
        intervals = in_study.diff().dropna().dt.total_seconds().to_numpy() / 3600.0
        exposure_days = max(0.0, (end - start).total_seconds() / 86400.0)
        mean_interval = float(np.mean(intervals)) if intervals.size else math.nan
        std_interval = _sample_std(intervals)
        rows.append(
            {
                "backend": backend,
                "component_type": component_type,
                "component_id": component_id,
                "study_start_utc": start,
                "study_end_utc": end,
                "exposure_days": exposure_days,
                "calibration_event_count": len(in_study),
                "calibration_events_per_day": (
                    len(in_study) / exposure_days if exposure_days > 0 else math.nan
                ),
                "first_event_utc": in_study.iloc[0] if len(in_study) else pd.NaT,
                "last_event_utc": in_study.iloc[-1] if len(in_study) else pd.NaT,
                "interarrival_count": intervals.size,
                "interarrival_mean_hours": mean_interval,
                "interarrival_median_hours": (
                    float(np.median(intervals)) if intervals.size else math.nan
                ),
                "interarrival_std_hours": std_interval,
                "interarrival_cv": (
                    std_interval / mean_interval
                    if intervals.size >= 2 and mean_interval > 0
                    else math.nan
                ),
                "interarrival_burstiness": (
                    (std_interval - mean_interval) / (std_interval + mean_interval)
                    if intervals.size >= 2 and std_interval + mean_interval > 0
                    else math.nan
                ),
                "interarrival_min_hours": (
                    float(np.min(intervals)) if intervals.size else math.nan
                ),
                "interarrival_max_hours": (
                    float(np.max(intervals)) if intervals.size else math.nan
                ),
            }
        )
    return pd.DataFrame(rows)


def compute_synchrony_episodes(
    events: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    tolerance_minutes: int,
) -> pd.DataFrame:
    """Group component events into backend-wide episodes to measure synchrony/coverage."""
    coverage_by_backend = coverage.set_index("backend")
    rows: list[dict[str, object]] = []
    universe = events.groupby(["backend", "component_type"], observed=True)[
        "component_id"
    ].nunique()
    tolerance = pd.Timedelta(minutes=tolerance_minutes)
    for (backend, component_type), group in events.groupby(
        ["backend", "component_type"], observed=True, sort=False
    ):
        start = pd.Timestamp(coverage_by_backend.loc[backend, "study_start_utc"])
        end = pd.Timestamp(coverage_by_backend.loc[backend, "study_end_utc"])
        group = group.loc[
            (group["event_timestamp_utc"] >= start)
            & (group["event_timestamp_utc"] <= end)
        ].sort_values("event_timestamp_utc")
        episode_rows: list[list[object]] = []
        current: list[object] = []
        anchor: pd.Timestamp | None = None
        for event in group.itertuples(index=False):
            timestamp = pd.Timestamp(event.event_timestamp_utc)
            if anchor is None or timestamp - anchor <= tolerance:
                current.append(event)
                if anchor is None:
                    anchor = timestamp
            else:
                episode_rows.append(current)
                current = [event]
                anchor = timestamp
        if current:
            episode_rows.append(current)
        component_total = int(universe.loc[(backend, component_type)])
        for episode_number, episode in enumerate(episode_rows):
            timestamps = [pd.Timestamp(event.event_timestamp_utc) for event in episode]
            component_ids = {str(event.component_id) for event in episode}
            drift_values = np.asarray(
                [float(event.drift_rms) for event in episode if pd.notna(event.drift_rms)]
            )
            rows.append(
                {
                    "backend": backend,
                    "component_type": component_type,
                    "episode_number": episode_number,
                    "episode_start_utc": min(timestamps),
                    "episode_end_utc": max(timestamps),
                    "episode_span_minutes": (
                        max(timestamps) - min(timestamps)
                    ).total_seconds()
                    / 60.0,
                    "component_event_count": len(episode),
                    "distinct_component_count": len(component_ids),
                    "component_count": component_total,
                    "component_coverage_fraction": len(component_ids) / component_total,
                    "components": ",".join(sorted(component_ids)),
                    "episode_drift_rms": _root_mean_square_array(drift_values),
                    "episode_drift_mean": (
                        float(np.mean(drift_values)) if drift_values.size else math.nan
                    ),
                }
            )
    return pd.DataFrame(rows)


def compute_component_property_summary(
    property_drift: pd.DataFrame,
    coverage: pd.DataFrame,
) -> pd.DataFrame:
    """Summarize raw-value volatility and drift for each component/property family."""
    _require_columns(
        property_drift,
        {
            "backend",
            "component_type",
            "component_id",
            "property_name",
            "unit",
            "calibration_timestamp_utc",
            "value",
            "drift_score",
        },
    )
    data = property_drift.merge(
        coverage[["backend", "study_start_utc", "study_end_utc"]],
        on="backend",
        how="left",
        validate="many_to_one",
    )
    data = data.loc[
        (data["calibration_timestamp_utc"] >= data["study_start_utc"])
        & (data["calibration_timestamp_utc"] <= data["study_end_utc"])
    ].copy()
    group_keys = ["backend", "component_type", "component_id", "property_name", "unit"]
    rows: list[dict[str, object]] = []
    for keys, group in data.groupby(group_keys, observed=True, sort=False, dropna=False):
        values = pd.to_numeric(group["value"], errors="coerce").dropna().to_numpy(dtype=float)
        drift = pd.to_numeric(group["drift_score"], errors="coerce").dropna().to_numpy(
            dtype=float
        )
        symmetric = pd.to_numeric(
            group["symmetric_relative_change"], errors="coerce"
        ).dropna().to_numpy(dtype=float)
        value_mean = float(np.mean(values)) if values.size else math.nan
        value_std = _sample_std(values)
        rows.append(
            {
                "backend": keys[0],
                "component_type": keys[1],
                "component_id": keys[2],
                "property_name": keys[3],
                "unit": keys[4],
                "calibration_value_count": values.size,
                "value_mean": value_mean,
                "value_median": float(np.median(values)) if values.size else math.nan,
                "value_std": value_std,
                "value_cv": (
                    value_std / abs(value_mean)
                    if values.size >= 2 and value_mean != 0
                    else math.nan
                ),
                "value_mad": (
                    float(np.median(np.abs(values - np.median(values))))
                    if values.size
                    else math.nan
                ),
                "value_min": float(np.min(values)) if values.size else math.nan,
                "value_max": float(np.max(values)) if values.size else math.nan,
                "drift_observation_count": drift.size,
                "drift_mean": float(np.mean(drift)) if drift.size else math.nan,
                "drift_rms": _root_mean_square_array(drift),
                "drift_p90": float(np.quantile(drift, 0.9)) if drift.size else math.nan,
                "drift_max": float(np.max(drift)) if drift.size else math.nan,
                "symmetric_relative_drift_median": (
                    float(np.median(symmetric)) if symmetric.size else math.nan
                ),
            }
        )
    return pd.DataFrame(rows)


def compute_property_staleness(
    property_drift: pd.DataFrame,
    grid: pd.DataFrame,
    *,
    thresholds_hours: Sequence[int] = (1, 6, 24, 48),
) -> pd.DataFrame:
    """Aggregate calibration age across property series, as specified in the draft."""
    thresholds = _validate_windows(thresholds_hours)
    _require_columns(
        property_drift,
        {
            "backend",
            "component_type",
            "component_id",
            "property_key",
            "calibration_timestamp_utc",
        },
    )
    rows: list[dict[str, object]] = []
    for (backend, component_type), component_properties in property_drift.groupby(
        ["backend", "component_type"], observed=True, sort=False
    ):
        backend_grid = grid.loc[
            grid["backend"] == backend, "evaluation_timestamp_utc"
        ].sort_values()
        event_arrays = [
            _timestamp_values_ns(series["calibration_timestamp_utc"].sort_values())
            for _, series in component_properties.groupby(
                ["component_id", "property_key"], observed=True, sort=False
            )
        ]
        for evaluation_time in backend_grid:
            ages: list[float] = []
            for times in event_arrays:
                position = int(np.searchsorted(times, evaluation_time.value, side="right")) - 1
                if position >= 0:
                    ages.append(max(0.0, (evaluation_time.value - int(times[position])) / 3.6e12))
            age_array = np.asarray(ages, dtype=float)
            row: dict[str, object] = {
                "backend": backend,
                "component_type": component_type,
                "evaluation_timestamp_utc": evaluation_time,
                "property_series_count": len(event_arrays),
                "known_property_age_count": age_array.size,
                "known_property_age_fraction": (
                    age_array.size / len(event_arrays) if event_arrays else math.nan
                ),
                "property_age_median_hours": (
                    float(np.median(age_array)) if age_array.size else math.nan
                ),
                "property_age_p90_hours": (
                    float(np.quantile(age_array, 0.9)) if age_array.size else math.nan
                ),
                "property_age_max_hours": (
                    float(np.max(age_array)) if age_array.size else math.nan
                ),
            }
            for threshold in thresholds:
                row[f"fraction_properties_age_gt_{threshold}h"] = (
                    float(np.mean(age_array > threshold)) if age_array.size else math.nan
                )
            rows.append(row)
    return pd.DataFrame(rows)


def compute_backend_snapshot_drift(
    property_drift: pd.DataFrame,
    grid: pd.DataFrame,
    *,
    grid_minutes: int,
) -> pd.DataFrame:
    """Approximate ||C[b,t] - C[b,t-1]|| on a common, robustly scaled state vector."""
    _require_columns(
        property_drift,
        {
            "backend",
            "component_type",
            "component_id",
            "property_key",
            "calibration_timestamp_utc",
            "signed_drift_score",
            "adverse_drift_score",
        },
    )
    rows: list[dict[str, object]] = []
    interval = pd.Timedelta(minutes=grid_minutes)
    identity = ["component_id", "property_key"]
    for (backend, component_type), group in property_drift.groupby(
        ["backend", "component_type"], observed=True, sort=False
    ):
        property_universe_count = len(group[identity].drop_duplicates())
        group = group.sort_values("calibration_timestamp_utc")
        group_times = _timestamp_values_ns(group["calibration_timestamp_utc"])
        backend_grid = grid.loc[
            grid["backend"] == backend, "evaluation_timestamp_utc"
        ].sort_values()
        for timestamp in backend_grid:
            left = int(
                np.searchsorted(
                    group_times,
                    (timestamp - interval).value,
                    side="right",
                )
            )
            right = int(np.searchsorted(group_times, timestamp.value, side="right"))
            changes = group.iloc[left:right]
            changes = changes.loc[changes["signed_drift_score"].notna()].copy()
            series_changes = changes.groupby(identity, observed=True, sort=False).agg(
                signed_vector_change=("signed_drift_score", "sum"),
                adverse_vector_change=("adverse_drift_score", "sum"),
                property_update_count=("signed_drift_score", "count"),
            ).reset_index()
            magnitudes = series_changes["signed_vector_change"].abs().to_numpy(dtype=float)
            changed_count = len(series_changes)
            component_adverse = series_changes.groupby(
                "component_id", observed=True, sort=False
            )["adverse_vector_change"].sum(min_count=1)
            component_adverse = component_adverse.dropna()
            rows.append(
                {
                    "backend": backend,
                    "component_type": component_type,
                    "evaluation_timestamp_utc": timestamp,
                    "interval_hours": grid_minutes / 60.0,
                    "property_series_count": property_universe_count,
                    "changed_property_series_count": changed_count,
                    "changed_property_series_fraction": (
                        changed_count / property_universe_count
                        if property_universe_count
                        else math.nan
                    ),
                    "raw_property_update_count": len(changes),
                    "changed_component_count": series_changes["component_id"].nunique(),
                    "degraded_component_count": int((component_adverse > 0).sum()),
                    "improved_component_count": int((component_adverse < 0).sum()),
                    "degraded_property_series_count": int(
                        (series_changes["adverse_vector_change"] > 0).sum()
                    ),
                    "improved_property_series_count": int(
                        (series_changes["adverse_vector_change"] < 0).sum()
                    ),
                    "device_drift_l2": (
                        float(np.sqrt(np.sum(magnitudes**2)))
                        if magnitudes.size
                        else 0.0
                    ),
                    "device_drift_rms_all_properties": (
                        float(np.sqrt(np.sum(magnitudes**2) / property_universe_count))
                        if property_universe_count
                        else math.nan
                    ),
                    "changed_property_drift_median": (
                        float(np.median(magnitudes)) if magnitudes.size else 0.0
                    ),
                    "changed_property_drift_p90": (
                        float(np.quantile(magnitudes, 0.9)) if magnitudes.size else 0.0
                    ),
                    "changed_property_drift_max": (
                        float(np.max(magnitudes)) if magnitudes.size else 0.0
                    ),
                }
            )
    return pd.DataFrame(rows)


def summarize_device_load(
    targets: pd.DataFrame,
    status: pd.DataFrame,
    *,
    target_timestamp_column: str,
    windows_hours: Sequence[int] = DEFAULT_WINDOWS_HOURS,
    max_staleness_minutes: int = 120,
) -> pd.DataFrame:
    """Attach current and strictly-prior queue summaries to arbitrary backend timestamps."""
    windows = _validate_windows(windows_hours)
    _require_columns(targets, {"backend", target_timestamp_column})
    _require_columns(status, {"backend", "status_timestamp_utc", "pending_jobs"})
    output = targets.copy()
    output[target_timestamp_column] = pd.to_datetime(output[target_timestamp_column], utc=True)
    output["load_observation_timestamp_utc"] = pd.Series(
        pd.NaT, index=output.index, dtype="datetime64[ns, UTC]"
    )
    output["pending_jobs_current"] = np.nan
    output["load_observation_age_minutes"] = np.nan
    for window in windows:
        output[f"pending_jobs_mean_previous_{window}h"] = np.nan
        output[f"pending_jobs_max_previous_{window}h"] = np.nan
        output[f"pending_jobs_std_previous_{window}h"] = np.nan
        output[f"pending_jobs_samples_previous_{window}h"] = 0

    for backend, target_index in output.groupby("backend", observed=True).groups.items():
        backend_status = status.loc[status["backend"] == backend].copy()
        backend_status["status_timestamp_utc"] = pd.to_datetime(
            backend_status["status_timestamp_utc"], utc=True
        )
        backend_status["pending_jobs"] = pd.to_numeric(
            backend_status["pending_jobs"], errors="coerce"
        )
        backend_status = backend_status.dropna(
            subset=["status_timestamp_utc", "pending_jobs"]
        ).sort_values("status_timestamp_utc")
        times = _timestamp_values_ns(backend_status["status_timestamp_utc"])
        values = backend_status["pending_jobs"].to_numpy(dtype=float)
        for index in target_index:
            timestamp = pd.Timestamp(output.at[index, target_timestamp_column])
            right_current = int(np.searchsorted(times, timestamp.value, side="right"))
            if right_current > 0:
                observed_ns = int(times[right_current - 1])
                age_minutes = (timestamp.value - observed_ns) / 6e10
                output.at[index, "load_observation_timestamp_utc"] = pd.Timestamp(
                    observed_ns, tz="UTC"
                )
                output.at[index, "load_observation_age_minutes"] = age_minutes
                if age_minutes <= max_staleness_minutes:
                    output.at[index, "pending_jobs_current"] = values[right_current - 1]
            right_prior = int(np.searchsorted(times, timestamp.value, side="left"))
            for window in windows:
                left_ns = (timestamp - pd.Timedelta(hours=window)).value
                left = int(np.searchsorted(times, left_ns, side="left"))
                window_values = values[left:right_prior]
                if window_values.size:
                    output.at[index, f"pending_jobs_mean_previous_{window}h"] = float(
                        np.mean(window_values)
                    )
                    output.at[index, f"pending_jobs_max_previous_{window}h"] = float(
                        np.max(window_values)
                    )
                    output.at[index, f"pending_jobs_std_previous_{window}h"] = _sample_std(
                        window_values
                    )
                    output.at[index, f"pending_jobs_samples_previous_{window}h"] = int(
                        window_values.size
                    )
    return output


def compute_grid_outcomes(
    events: pd.DataFrame,
    grid: pd.DataFrame,
    *,
    grid_minutes: int,
) -> pd.DataFrame:
    """Aggregate event incidence and drift once per backend/type/grid interval."""
    rows: list[dict[str, object]] = []
    component_types = sorted(events["component_type"].dropna().unique())
    interval = pd.Timedelta(minutes=grid_minutes)
    for backend, backend_grid in grid.groupby("backend", observed=True, sort=False):
        for component_type in component_types:
            subset = events.loc[
                (events["backend"] == backend)
                & (events["component_type"] == component_type)
            ].sort_values("event_timestamp_utc")
            for timestamp in backend_grid["evaluation_timestamp_utc"].sort_values():
                window = subset.loc[
                    (subset["event_timestamp_utc"] > timestamp - interval)
                    & (subset["event_timestamp_utc"] <= timestamp)
                ]
                drift_values = pd.to_numeric(window["drift_rms"], errors="coerce").dropna()
                rows.append(
                    {
                        "backend": backend,
                        "component_type": component_type,
                        "evaluation_timestamp_utc": timestamp,
                        "interval_hours": grid_minutes / 60.0,
                        "calibration_event_count": len(window),
                        "calibrated_component_count": window["component_id"].nunique(),
                        "drift_event_count": len(drift_values),
                        "drift_mean": drift_values.mean() if len(drift_values) else math.nan,
                        "drift_rms": _root_mean_square(drift_values),
                        "drift_max": drift_values.max() if len(drift_values) else math.nan,
                    }
                )
    return pd.DataFrame(rows)


def compute_correlations(
    data: pd.DataFrame,
    *,
    group_columns: Sequence[str],
    outcome_columns: Sequence[str],
    predictor_columns: Sequence[str],
    min_n: int = 8,
) -> pd.DataFrame:
    _require_columns(data, set(group_columns) | set(outcome_columns) | set(predictor_columns))
    rows: list[dict[str, object]] = []
    grouped: Iterable[tuple[object, pd.DataFrame]]
    if group_columns:
        grouped = data.groupby(list(group_columns), observed=True, sort=False, dropna=False)
    else:
        grouped = [((), data)]
    for keys, group in grouped:
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        key_values = dict(zip(group_columns, key_tuple, strict=True))
        for outcome in outcome_columns:
            for predictor in predictor_columns:
                pair = group[[outcome, predictor]].apply(pd.to_numeric, errors="coerce")
                pair = pair.replace([np.inf, -np.inf], np.nan).dropna()
                n = len(pair)
                for method in ("pearson", "spearman"):
                    coefficient = p_value = math.nan
                    if n >= min_n and pair[outcome].nunique() > 1 and pair[predictor].nunique() > 1:
                        if method == "pearson":
                            result = stats.pearsonr(pair[predictor], pair[outcome])
                        else:
                            result = stats.spearmanr(pair[predictor], pair[outcome])
                        coefficient = float(result.statistic)
                        p_value = float(result.pvalue)
                    rows.append(
                        {
                            **key_values,
                            "outcome": outcome,
                            "predictor": predictor,
                            "method": method,
                            "n": n,
                            "coefficient": coefficient,
                            "p_value_naive": p_value,
                        }
                    )
    result = pd.DataFrame(rows)
    if not result.empty:
        result["q_value_bh"] = _benjamini_hochberg(result["p_value_naive"])
    return result


def compute_lagged_correlations(
    grid_outcomes_with_load: pd.DataFrame,
    *,
    grid_minutes: int,
    lags_hours: Sequence[int] = DEFAULT_LAGS_HOURS,
    min_n: int = 8,
) -> pd.DataFrame:
    """Positive lag means queue pressure precedes the calibration outcome."""
    rows: list[pd.DataFrame] = []
    for (backend, component_type), group in grid_outcomes_with_load.groupby(
        ["backend", "component_type"], observed=True, sort=False
    ):
        group = group.sort_values("evaluation_timestamp_utc").copy()
        for lag_hours in lags_hours:
            steps_float = lag_hours * 60 / grid_minutes
            if not float(steps_float).is_integer():
                continue
            steps = int(steps_float)
            group["lagged_pending_jobs"] = group["pending_jobs_current"].shift(steps)
            correlations = compute_correlations(
                group,
                group_columns=[],
                outcome_columns=["calibration_event_count", "calibrated_component_count", "drift_mean"],
                predictor_columns=["lagged_pending_jobs"],
                min_n=min_n,
            )
            correlations.insert(0, "lag_hours_load_leads", lag_hours)
            correlations.insert(0, "component_type", component_type)
            correlations.insert(0, "backend", backend)
            rows.append(correlations)
    if not rows:
        return pd.DataFrame()
    result = pd.concat(rows, ignore_index=True)
    result["q_value_bh"] = _benjamini_hochberg(result["p_value_naive"])
    return result


def compute_two_way_fixed_effect_regressions(
    data: pd.DataFrame,
    *,
    subgroup_columns: Sequence[str],
    outcome_columns: Sequence[str],
    predictor_columns: Sequence[str],
    entity_column: str = "backend",
    time_column: str = "evaluation_timestamp_utc",
    min_n: int = 20,
) -> pd.DataFrame:
    """Estimate y=beta*x with backend and time effects removed by alternating projections."""
    required = (
        set(subgroup_columns)
        | set(outcome_columns)
        | set(predictor_columns)
        | {entity_column, time_column}
    )
    _require_columns(data, required)
    grouped: Iterable[tuple[object, pd.DataFrame]]
    if subgroup_columns:
        grouped = data.groupby(list(subgroup_columns), observed=True, sort=False, dropna=False)
    else:
        grouped = [((), data)]
    rows: list[dict[str, object]] = []
    for keys, group in grouped:
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        key_values = dict(zip(subgroup_columns, key_tuple, strict=True))
        for outcome in outcome_columns:
            for predictor in predictor_columns:
                model = group[[entity_column, time_column, outcome, predictor]].copy()
                model[outcome] = pd.to_numeric(model[outcome], errors="coerce")
                model[predictor] = pd.to_numeric(model[predictor], errors="coerce")
                model = model.replace([np.inf, -np.inf], np.nan).dropna()
                n = len(model)
                entity_count = model[entity_column].nunique()
                time_count = model[time_column].nunique()
                result: dict[str, object] = {
                    **key_values,
                    "outcome": outcome,
                    "predictor": predictor,
                    "n": n,
                    "backend_count": entity_count,
                    "time_period_count": time_count,
                    "coefficient": math.nan,
                    "two_way_cluster_se": math.nan,
                    "backend_cluster_se": math.nan,
                    "p_value_two_way_normal": math.nan,
                    "ci95_low": math.nan,
                    "ci95_high": math.nan,
                    "within_r_squared": math.nan,
                }
                if (
                    n >= min_n
                    and entity_count >= 2
                    and time_count >= 2
                    and model[outcome].nunique() > 1
                    and model[predictor].nunique() > 1
                ):
                    y = model[outcome].to_numpy(dtype=float)
                    x = model[predictor].to_numpy(dtype=float)
                    entities = model[entity_column].to_numpy()
                    times = model[time_column].astype(str).to_numpy()
                    y_within = _two_way_residualize(y, entities, times)
                    x_within = _two_way_residualize(x, entities, times)
                    denominator = float(np.dot(x_within, x_within))
                    if denominator > 1e-12:
                        beta = float(np.dot(x_within, y_within) / denominator)
                        residuals = y_within - beta * x_within
                        scores = x_within * residuals
                        backend_meat = _cluster_score_meat(scores, entities)
                        time_meat = _cluster_score_meat(scores, times)
                        white_meat = float(np.dot(scores, scores))
                        two_way_variance = (backend_meat + time_meat - white_meat) / (
                            denominator**2
                        )
                        backend_variance = backend_meat / (denominator**2)
                        two_way_se = (
                            math.sqrt(two_way_variance) if two_way_variance > 0 else math.nan
                        )
                        backend_se = (
                            math.sqrt(backend_variance) if backend_variance > 0 else math.nan
                        )
                        p_value = (
                            float(2 * stats.norm.sf(abs(beta / two_way_se)))
                            if two_way_se > 0
                            else math.nan
                        )
                        total_within = float(np.dot(y_within, y_within))
                        result.update(
                            {
                                "coefficient": beta,
                                "two_way_cluster_se": two_way_se,
                                "backend_cluster_se": backend_se,
                                "p_value_two_way_normal": p_value,
                                "ci95_low": beta - 1.96 * two_way_se,
                                "ci95_high": beta + 1.96 * two_way_se,
                                "within_r_squared": (
                                    1.0 - float(np.dot(residuals, residuals)) / total_within
                                    if total_within > 0
                                    else math.nan
                                ),
                            }
                        )
                rows.append(result)
    output = pd.DataFrame(rows)
    if not output.empty:
        output["q_value_bh"] = _benjamini_hochberg(output["p_value_two_way_normal"])
    return output


def _root_mean_square(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    return _root_mean_square_array(clean)


def _quality_direction(property_name: object) -> str | None:
    """Return the known direction of improving physical quality, if identifiable."""
    normalized = str(property_name).strip().lower()
    if "error" in normalized or "infidelity" in normalized:
        return "lower_is_better"
    if normalized in {"t1", "t2"} or "fidelity" in normalized:
        return "higher_is_better"
    return None


def _timestamp_values_ns(values: pd.Series) -> np.ndarray:
    """Return UTC epoch nanoseconds across pandas 2.x/3.x datetime resolutions."""
    timestamps = pd.to_datetime(values, utc=True)
    return np.fromiter(
        (pd.Timestamp(value).value for value in timestamps),
        dtype=np.int64,
        count=len(timestamps),
    )


def _two_way_residualize(
    values: np.ndarray,
    entities: np.ndarray,
    times: np.ndarray,
    *,
    tolerance: float = 1e-12,
    max_iterations: int = 100,
) -> np.ndarray:
    """Residualize an unbalanced panel against entity and time indicator columns."""
    residual = np.asarray(values, dtype=float).copy()
    for _ in range(max_iterations):
        previous = residual.copy()
        for labels in (entities, times):
            frame = pd.DataFrame({"value": residual, "label": labels})
            residual -= frame.groupby("label", observed=True)["value"].transform("mean").to_numpy()
        if float(np.max(np.abs(residual - previous))) <= tolerance:
            break
    return residual


def _cluster_score_meat(scores: np.ndarray, labels: np.ndarray) -> float:
    frame = pd.DataFrame({"score": scores, "label": labels})
    cluster_scores = frame.groupby("label", observed=True)["score"].sum().to_numpy(dtype=float)
    return float(np.dot(cluster_scores, cluster_scores))


def _root_mean_square_array(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(values**2))) if values.size else math.nan


def _sample_std(values: np.ndarray) -> float:
    return float(np.std(values, ddof=1)) if values.size >= 2 else math.nan


def _coefficient_of_variation(values: np.ndarray) -> float:
    mean = float(np.mean(values)) if values.size else math.nan
    return _sample_std(values) / mean if values.size >= 2 and mean > 0 else math.nan


def _gini(values: np.ndarray) -> float:
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    if clean.size == 0 or np.any(clean < 0):
        return math.nan
    total = float(clean.sum())
    if total == 0:
        return 0.0
    ordered = np.sort(clean)
    n = ordered.size
    index = np.arange(1, n + 1)
    return float((2.0 * np.sum(index * ordered) / (n * total)) - (n + 1.0) / n)


def _mean_pairwise_absolute_difference(values: np.ndarray) -> float:
    if values.size < 2:
        return 0.0 if values.size == 1 else math.nan
    ordered = np.sort(values)
    n = ordered.size
    weights = 2 * np.arange(1, n + 1) - n - 1
    pair_sum = float(np.sum(weights * ordered))
    return 2.0 * pair_sum / (n * (n - 1))


def _benjamini_hochberg(p_values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(p_values, errors="coerce")
    result = pd.Series(np.nan, index=p_values.index, dtype=float)
    valid = numeric.dropna().sort_values()
    if valid.empty:
        return result
    m = len(valid)
    adjusted = valid.to_numpy() * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.clip(adjusted, 0.0, 1.0)
    return result


def _validate_windows(windows_hours: Sequence[int]) -> tuple[int, ...]:
    windows = tuple(sorted(set(int(window) for window in windows_hours)))
    if not windows or any(window <= 0 for window in windows):
        raise ValueError("windows_hours must contain positive integers")
    return windows


def _require_columns(frame: pd.DataFrame, required: set[str]) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")
