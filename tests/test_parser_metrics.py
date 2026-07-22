from datetime import datetime, timezone

import pytest

from ibm_calibration_collector.ibm_client import IBMQuantumClient
from ibm_calibration_collector.metrics import (
    compute_fetch_cycle_metrics,
    latest_edge_dates,
    latest_qubit_dates,
)
from ibm_calibration_collector.parser import parse_properties, parse_status


def test_parse_and_metrics():
    poll = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    props = {
        "backend_name": "ibm_test",
        "backend_version": "1.2.3",
        "last_update_date": "2026-06-15T11:45:00Z",
        "qubits": [
            [
                {"name": "T1", "value": 0.0001, "unit": "s", "date": "2026-06-15T11:00:00Z"},
                {"name": "readout_error", "value": 0.01, "unit": "", "date": "2026-06-15T11:30:00Z"},
            ],
            [
                {"name": "T1", "value": 0.0002, "unit": "s", "date": "2026-06-15T10:00:00Z"},
            ],
        ],
        "gates": [
            {
                "gate": "sx",
                "qubits": ["0"],
                "parameters": [
                    {
                        "name": "gate_error",
                        "value": 0.001,
                        "unit": "",
                        "date": "2026-06-15T11:40:00Z",
                    }
                ],
            },
            {
                "gate": "cz",
                "qubits": ["1", "0"],
                "parameters": [
                    {"name": "gate_error", "value": 0.002, "unit": "", "date": "2026-06-15T11:15:00Z"},
                    {"name": "gate_length", "value": 1e-7, "unit": "s", "date": "2026-06-15T11:20:00Z"},
                ],
            }
        ],
    }
    parsed = parse_properties("ibm_test", props, poll, edge_id_mode="undirected")
    q_dates = latest_qubit_dates(parsed.qubit_properties, parsed.gate_properties)
    e_dates = latest_edge_dates(parsed.gate_properties)
    assert q_dates[0].isoformat() == "2026-06-15T11:40:00+00:00"
    assert q_dates[1].isoformat() == "2026-06-15T10:00:00+00:00"
    assert e_dates["0-1"].isoformat() == "2026-06-15T11:20:00+00:00"

    metrics = compute_fetch_cycle_metrics(
        backend="ibm_test",
        poll_timestamp_utc=poll,
        current_qubit_dates=q_dates,
        current_edge_dates=e_dates,
        previous_fetch_cycle_id="prev",
        previous_poll_timestamp_utc=datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc),
        previous_qubit_dates={0: datetime(2026, 6, 15, 11, 40, tzinfo=timezone.utc), 1: datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)},
        previous_edge_dates={"0-1": datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)},
    )
    assert metrics.qubits_calibrated_since_last_fetch == [1]
    assert metrics.edges_calibrated_since_last_fetch == ["0-1"]
    assert metrics.oldest_qubit == 1
    assert metrics.max_qubit_calibration_age_seconds == 7200.0


def test_parse_status_uses_documented_backend_queue_fields():
    poll = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    status = {
        "backend_version": "1.2.3",
        "state": True,
        "status": "active",
        "message": "Ready",
        "length_queue": 3,
    }
    backend_summary = {
        "name": "ibm_test",
        "queue_length": 7,
        "status": {"name": "online", "reason": "QPU is available"},
    }

    parsed = parse_status("ibm_test", status, poll, backend_json=backend_summary)

    assert parsed.backend_version == "1.2.3"
    assert parsed.pending_jobs == 7
    assert parsed.operational is True
    assert parsed.status_name == "active"
    assert parsed.status_msg == "Ready"
    assert parsed.raw == status
    assert parsed.raw_backend == backend_summary


def test_parse_status_falls_back_to_status_queue_length():
    poll = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    parsed = parse_status(
        "ibm_test",
        {"state": False, "message": "Paused", "length_queue": 4},
        poll,
    )

    assert parsed.pending_jobs == 4
    assert parsed.operational is False
    assert parsed.status_msg == "Paused"


def test_parse_properties_requires_qubits_and_gates():
    poll = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(
        ValueError,
        match="backend properties JSON missing required qubits/gates fields",
    ):
        parse_properties("ibm_test", {"qubits": []}, poll)


def test_ibm_client_rejects_json_error_payload(monkeypatch):
    class ErrorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errors": [{"code": "backend_unavailable"}]}

    client = IBMQuantumClient("api-key", "service-crn")
    monkeypatch.setattr(client, "_get_bearer_token", lambda: "token")
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: ErrorResponse())

    with pytest.raises(RuntimeError, match="IBM API returned errors"):
        client.get_backend_properties("ibm_test")
