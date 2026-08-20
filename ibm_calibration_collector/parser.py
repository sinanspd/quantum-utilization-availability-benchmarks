from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import (
    BackendStatusSnapshot,
    GateOperationalSnapshot,
    GatePropertySnapshot,
    ParsedBackendProperties,
    QubitOperationalSnapshot,
    QubitPropertySnapshot,
)
from .time_utils import parse_datetime


def parse_status(
    backend: str,
    status_json: dict[str, Any],
    poll_timestamp_utc: datetime,
    backend_json: dict[str, Any] | None = None,
) -> BackendStatusSnapshot:
    backend_status = _as_dict(backend_json.get("status")) if backend_json is not None else None
    status_name = _first_str(
        status_json.get("status"),
        _dict_get(backend_status, "name"),
    )

    return BackendStatusSnapshot(
        backend=backend,
        poll_timestamp_utc=poll_timestamp_utc,
        backend_version=_as_str(status_json.get("backend_version")),
        pending_jobs=_first_int(
            _dict_get(backend_json, "queue_length"),
            status_json.get("length_queue"),
            status_json.get("pending_jobs"),
        ),
        operational=_first_bool(
            status_json.get("state"),
            status_json.get("operational"),
            _operational_from_status_name(status_name),
        ),
        status_name=status_name,
        status_msg=_first_str(
            status_json.get("message"),
            status_json.get("status_msg"),
            _dict_get(backend_status, "reason"),
        ),
        raw=status_json,
        raw_backend=backend_json,
    )


def parse_properties(
    backend: str,
    properties_json: dict[str, Any],
    poll_timestamp_utc: datetime,
    edge_id_mode: str = "undirected",
) -> ParsedBackendProperties:
    if "qubits" not in properties_json or "gates" not in properties_json:
        raise ValueError("backend properties JSON missing required qubits/gates fields")

    last_update = parse_datetime(properties_json.get("last_update_date"))
    backend_version = _as_str(properties_json.get("backend_version"))

    qubit_rows: list[QubitPropertySnapshot] = []
    qubit_operational_rows: list[QubitOperationalSnapshot] = []
    qubits = properties_json.get("qubits") or []
    if not isinstance(qubits, list):
        raise TypeError("properties.qubits must be a list")

    for qubit_idx, property_list in enumerate(qubits):
        if not isinstance(property_list, list):
            continue
        operational_property = _find_named_property(property_list, "operational")
        operational_reported = _parse_explicit_operational(
            operational_property,
            context=f"qubit {qubit_idx}",
        )
        qubit_operational_rows.append(
            QubitOperationalSnapshot(
                backend=backend,
                poll_timestamp_utc=poll_timestamp_utc,
                qubit=qubit_idx,
                operational_reported=operational_reported,
                operational_effective=(
                    operational_reported if operational_reported is not None else True
                ),
                operational_is_explicit=operational_property is not None,
                operational_property_date=(
                    parse_datetime(operational_property.get("date"))
                    if operational_property is not None
                    else None
                ),
                raw_operational_property=operational_property,
            )
        )
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
    gate_operational_rows: list[GateOperationalSnapshot] = []
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
        operational_parameter = _find_named_property(parameters, "operational")
        operational_reported = _parse_explicit_operational(
            operational_parameter,
            context=f"gate {gate_name}@{qubits_key}",
        )
        if operational_parameter is None and "operational" in gate:
            operational_reported = _parse_operational_value(
                gate.get("operational"),
                context=f"gate {gate_name}@{qubits_key}",
            )
        operational_is_explicit = operational_parameter is not None or "operational" in gate
        gate_operational_rows.append(
            GateOperationalSnapshot(
                backend=backend,
                poll_timestamp_utc=poll_timestamp_utc,
                gate_name=gate_name,
                qubits=qubits,
                qubits_key=qubits_key,
                edge_id=edge_id,
                operational_reported=operational_reported,
                operational_effective=(
                    operational_reported if operational_reported is not None else True
                ),
                operational_is_explicit=operational_is_explicit,
                operational_property_date=(
                    parse_datetime(operational_parameter.get("date"))
                    if operational_parameter is not None
                    else None
                ),
                raw_gate=gate,
                raw_operational_parameter=operational_parameter,
            )
        )
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
        qubit_operational_snapshots=qubit_operational_rows,
        gate_operational_snapshots=gate_operational_rows,
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


def _first_str(*values: Any) -> str | None:
    for value in values:
        parsed = _as_str(value)
        if parsed is not None:
            return parsed
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
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


def _find_named_property(items: list[Any], name: str) -> dict[str, Any] | None:
    normalized_name = name.strip().lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = _as_str(item.get("name"))
        if item_name is not None and item_name.strip().lower() == normalized_name:
            return item
    return None


def _parse_explicit_operational(
    operational_property: dict[str, Any] | None,
    *,
    context: str,
) -> bool | None:
    if operational_property is None:
        return None
    return _parse_operational_value(operational_property.get("value"), context=context)


def _parse_operational_value(value: Any, *, context: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"invalid operational value for {context}: {value!r}")


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        parsed = _as_bool(value)
        if parsed is not None:
            return parsed
    return None


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _dict_get(mapping: dict[str, Any] | None, key: str) -> Any:
    if mapping is None:
        return None
    return mapping.get(key)


def _operational_from_status_name(status_name: str | None) -> bool | None:
    if status_name is None:
        return None
    normalized = status_name.strip().lower()
    if normalized in {"online", "active", "available", "running"}:
        return True
    if normalized in {"offline", "paused", "maintenance", "unavailable"}:
        return False
    return None
