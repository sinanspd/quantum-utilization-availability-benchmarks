import pytest
import psycopg

from ibm_calibration_collector import backfill
from ibm_calibration_collector.backfill import parse_args


def test_backfill_arguments_reject_conflicting_run_selection():
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--new-run",
                "--run-id",
                "00000000-0000-0000-0000-000000000001",
            ]
        )


def test_preparation_reconnects_after_transient_timeout(monkeypatch):
    attempts = 0

    def flaky_prepare(_database_url):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.OperationalError("connection timed out")

    monkeypatch.setattr(backfill, "ensure_control_schema", lambda _database_url: None)
    monkeypatch.setattr(backfill, "prepare_indexes", flaky_prepare)
    monkeypatch.setattr(backfill.time, "sleep", lambda _seconds: None)

    backfill.prepare_with_retries(
        "postgresql://test",
        skip_index_preparation=False,
        max_retries=1,
    )

    assert attempts == 2


def test_preparation_raises_after_retry_limit(monkeypatch):
    def always_fails(_database_url):
        raise psycopg.OperationalError("connection timed out")

    monkeypatch.setattr(backfill, "ensure_control_schema", lambda _database_url: None)
    monkeypatch.setattr(backfill, "prepare_indexes", always_fails)
    monkeypatch.setattr(backfill.time, "sleep", lambda _seconds: None)

    with pytest.raises(psycopg.OperationalError, match="connection timed out"):
        backfill.prepare_with_retries(
            "postgresql://test",
            skip_index_preparation=False,
            max_retries=2,
        )
