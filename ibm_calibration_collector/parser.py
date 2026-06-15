from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import (
    BackendStatusSnapshot,
    GatePropertySnapshot,
    ParsedBackendProperties,
    QubitPropertySnapshot,
)
from .time_utils import parse_datetime


def parse_status(
    backend: str,
    status_json: dict[str, Any],
    poll_timestamp_utc: datetime,
) -> BackendStatusSnapshot:
    return BackendStatusSnapshot(
        backend=backend,
        poll_timestamp_utc=poll_timestamp_utc,
        backend_version=_as_str(status_json.get("backend_version")),
        pending_jobs=_as_int(status_json.get("pending_jobs")),
        operational=_as_bool(status_json.get("operational")),
        status_msg=_as_str(status_json.get("status_msg")),
        raw=status_json,
    )


def parse_properties(
    backend: str,
    properties_json: dict[str, Any],
    poll_timestamp_utc: datetime,
    edge_id_mode: str = "undirected",
) -> ParsedBackendProperties:
    last_update = parse_datetime(properties_json.get("last_update_date"))
    backend_version = _as_str(properties_json.get("backend_version"))

    qubit_rows: list[QubitPropertySnapshot] = []
    qubits = properties_json.get("qubits") or []
    if not isinstance(qubits, list):
        raise TypeError("properties.qubits must be a list")

    for qubit_idx, property_list in enumerate(qubits):
        if not isinstance(property_list, list):
            continue
        for prop in property_list:
            if not isinstance(prop, dict):
                continue
            name = _as_str(prop.get("name"))
            if name is None:
                continue
            qubit_rows.append(
                QubitPropertySnapshot(
                    backend=backend,
                    poll_timestamp_utc=poll_timestamp_utc,
                    properties_last_update_date=last_update,
                    qubit=qubit_idx,
                    property_name=name,
                    value=_as_float(prop.get("value")),
                    unit=_as_str(prop.get("unit")),
                    property_date=parse_datetime(prop.get("date")),
                    raw_property=prop,
                )
            )

    gate_rows: list[GatePropertySnapshot] = []
    gates = properties_json.get("gates") or []
    if not isinstance(gates, list):
        raise TypeError("properties.gates must be a list")

    for gate in gates:
        if not isinstance(gate, dict):
            continue
        gate_name = _as_str(gate.get("gate")) or _as_str(gate.get("name"))
        qubits_raw = gate.get("qubits") or []
        if gate_name is None or not isinstance(qubits_raw, list):
            continue
        qubits_list = [_as_int(q) for q in qubits_raw]
        qubits = [q for q in qubits_list if q is not None]
        if len(qubits) != len(qubits_raw):
            continue
        qubits_key = ",".join(str(q) for q in qubits)
        edge_id = make_edge_id(qubits, edge_id_mode=edge_id_mode) if len(qubits) == 2 else None
        parameters = gate.get("parameters") or []
        if not isinstance(parameters, list):
            continue
        for param in parameters:
            if not isinstance(param, dict):
                continue
            param_name = _as_str(param.get("name"))
            if param_name is None:
                continue
            gate_rows.append(
                GatePropertySnapshot(
                    backend=backend,
                    poll_timestamp_utc=poll_timestamp_utc,
                    properties_last_update_date=last_update,
                    gate_name=gate_name,
                    qubits=qubits,
                    qubits_key=qubits_key,
                    edge_id=edge_id,
                    parameter_name=param_name,
                    value=_as_float(param.get("value")),
                    unit=_as_str(param.get("unit")),
                    property_date=parse_datetime(param.get("date")),
                    raw_parameter=param,
                )
            )

    return ParsedBackendProperties(
        backend=backend,
        backend_version=backend_version,
        properties_last_update_date=last_update,
        qubit_properties=qubit_rows,
        gate_properties=gate_rows,
    )


def make_edge_id(qubits: list[int], edge_id_mode: str = "undirected") -> str | None:
    if len(qubits) != 2:
        return None
    a, b = qubits
    if edge_id_mode == "undirected":
        a, b = sorted((a, b))
    elif edge_id_mode != "directed":
        raise ValueError("edge_id_mode must be 'undirected' or 'directed'")
    return f"{a}-{b}"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return bool(value)
