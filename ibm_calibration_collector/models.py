from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BackendStatusSnapshot:
    backend: str
    poll_timestamp_utc: datetime
    backend_version: str | None
    pending_jobs: int | None
    operational: bool | None
    status_msg: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class QubitPropertySnapshot:
    backend: str
    poll_timestamp_utc: datetime
    properties_last_update_date: datetime | None
    qubit: int
    property_name: str
    value: float | None
    unit: str | None
    property_date: datetime | None
    raw_property: dict[str, Any]


@dataclass(frozen=True)
class GatePropertySnapshot:
    backend: str
    poll_timestamp_utc: datetime
    properties_last_update_date: datetime | None
    gate_name: str
    qubits: list[int]
    qubits_key: str
    edge_id: str | None
    parameter_name: str
    value: float | None
    unit: str | None
    property_date: datetime | None
    raw_parameter: dict[str, Any]


@dataclass(frozen=True)
class ParsedBackendProperties:
    backend: str
    backend_version: str | None
    properties_last_update_date: datetime | None
    qubit_properties: list[QubitPropertySnapshot]
    gate_properties: list[GatePropertySnapshot]


@dataclass(frozen=True)
class ComponentAge:
    component_id: int | str
    latest_calibration_date: datetime
    age_seconds: float


@dataclass(frozen=True)
class FetchCycleMetrics:
    backend: str
    poll_timestamp_utc: datetime
    prev_fetch_cycle_id: str | None
    prev_poll_timestamp_utc: datetime | None
    num_qubits_calibrated_since_last_fetch: int
    num_edges_calibrated_since_last_fetch: int
    qubits_calibrated_since_last_fetch: list[int]
    edges_calibrated_since_last_fetch: list[str]
    max_qubit_calibration_age_seconds: float | None
    max_edge_calibration_age_seconds: float | None
    oldest_qubit: int | None
    oldest_qubit_calibration_timestamp: datetime | None
    oldest_edge_id: str | None
    oldest_edge_calibration_timestamp: datetime | None
    qubit_latest_calibration_dates: dict[str, str]
    edge_latest_calibration_dates: dict[str, str]
