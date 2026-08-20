from __future__ import annotations

from datetime import datetime

from .models import FetchCycleMetrics, GatePropertySnapshot, QubitPropertySnapshot
from .time_utils import seconds_between


def latest_qubit_dates(
    rows: list[QubitPropertySnapshot],
    gate_rows: list[GatePropertySnapshot] | None = None,
) -> dict[int, datetime]:
    latest: dict[int, datetime] = {}
    for row in rows:
        if row.property_date is None or _is_operational_name(row.property_name):
            continue
        old = latest.get(row.qubit)
        if old is None or row.property_date > old:
            latest[row.qubit] = row.property_date
    for row in gate_rows or []:
        if (
            len(row.qubits) != 1
            or row.property_date is None
            or _is_operational_name(row.parameter_name)
        ):
            continue
        qubit = row.qubits[0]
        old = latest.get(qubit)
        if old is None or row.property_date > old:
            latest[qubit] = row.property_date
    return latest


def latest_edge_dates(rows: list[GatePropertySnapshot]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for row in rows:
        if (
            row.edge_id is None
            or row.property_date is None
            or _is_operational_name(row.parameter_name)
        ):
            continue
        old = latest.get(row.edge_id)
        if old is None or row.property_date > old:
            latest[row.edge_id] = row.property_date
    return latest


def _is_operational_name(name: str) -> bool:
    return name.strip().lower() == "operational"


def compute_fetch_cycle_metrics(
    *,
    backend: str,
    poll_timestamp_utc: datetime,
    current_qubit_dates: dict[int, datetime],
    current_edge_dates: dict[str, datetime],
    previous_fetch_cycle_id: str | None,
    previous_poll_timestamp_utc: datetime | None,
    previous_qubit_dates: dict[int, datetime] | None,
    previous_edge_dates: dict[str, datetime] | None,
) -> FetchCycleMetrics:
    if previous_qubit_dates is None:
        qubits_calibrated: list[int] = []
    else:
        qubits_calibrated = sorted(
            q
            for q, current_date in current_qubit_dates.items()
            if q in previous_qubit_dates and current_date > previous_qubit_dates[q]
        )

    if previous_edge_dates is None:
        edges_calibrated: list[str] = []
    else:
        edges_calibrated = sorted(
            edge_id
            for edge_id, current_date in current_edge_dates.items()
            if edge_id in previous_edge_dates and current_date > previous_edge_dates[edge_id]
        )

    oldest_qubit, oldest_qubit_ts, max_qubit_age = _oldest_component_age(
        poll_timestamp_utc, current_qubit_dates
    )
    oldest_edge, oldest_edge_ts, max_edge_age = _oldest_component_age(
        poll_timestamp_utc, current_edge_dates
    )

    return FetchCycleMetrics(
        backend=backend,
        poll_timestamp_utc=poll_timestamp_utc,
        prev_fetch_cycle_id=previous_fetch_cycle_id,
        prev_poll_timestamp_utc=previous_poll_timestamp_utc,
        num_qubits_calibrated_since_last_fetch=len(qubits_calibrated),
        num_edges_calibrated_since_last_fetch=len(edges_calibrated),
        qubits_calibrated_since_last_fetch=qubits_calibrated,
        edges_calibrated_since_last_fetch=edges_calibrated,
        max_qubit_calibration_age_seconds=max_qubit_age,
        max_edge_calibration_age_seconds=max_edge_age,
        oldest_qubit=oldest_qubit if isinstance(oldest_qubit, int) else None,
        oldest_qubit_calibration_timestamp=oldest_qubit_ts,
        oldest_edge_id=oldest_edge if isinstance(oldest_edge, str) else None,
        oldest_edge_calibration_timestamp=oldest_edge_ts,
        qubit_latest_calibration_dates={
            str(k): v.isoformat() for k, v in sorted(current_qubit_dates.items())
        },
        edge_latest_calibration_dates={
            str(k): v.isoformat() for k, v in sorted(current_edge_dates.items())
        },
    )


def _oldest_component_age(
    reference_time: datetime,
    latest_dates: dict[int | str, datetime],
) -> tuple[int | str | None, datetime | None, float | None]:
    if not latest_dates:
        return None, None, None
    component_id, oldest_latest_timestamp = min(latest_dates.items(), key=lambda kv: kv[1])
    return (
        component_id,
        oldest_latest_timestamp,
        seconds_between(reference_time, oldest_latest_timestamp),
    )
