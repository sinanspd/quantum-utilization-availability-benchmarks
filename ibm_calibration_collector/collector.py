from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import replace
from typing import Any, NoReturn

from .config import CollectorConfig
from .db import PostgresStore
from .ibm_client import IBMQuantumClient
from .metrics import compute_fetch_cycle_metrics, latest_edge_dates, latest_qubit_dates
from .parser import parse_properties, parse_status
from .time_utils import utc_now

LOG = logging.getLogger("ibm_calibration_collector")


def collect_backend_once(
    *,
    backend: str,
    config: CollectorConfig,
    client: IBMQuantumClient,
    store: PostgresStore,
    raw_backend_summary: dict[str, Any] | None = None,
) -> str:
    poll_started_at = utc_now()
    try:
        raw_status = client.get_backend_status(backend)
        raw_properties = client.get_backend_properties(backend)
        poll_finished_at = utc_now()

        status = parse_status(
            backend,
            raw_status,
            poll_finished_at,
            backend_json=raw_backend_summary,
        )
        properties = parse_properties(
            backend,
            raw_properties,
            poll_finished_at,
            edge_id_mode=config.edge_id_mode,
        )

        prev_fetch_id, prev_poll_ts, prev_qubit_dates, prev_edge_dates = (
            store.get_previous_summaries(backend)
        )
        current_qubit_dates = latest_qubit_dates(properties.qubit_properties)
        current_edge_dates = latest_edge_dates(properties.gate_properties)

        metrics = compute_fetch_cycle_metrics(
            backend=backend,
            poll_timestamp_utc=poll_finished_at,
            current_qubit_dates=current_qubit_dates,
            current_edge_dates=current_edge_dates,
            previous_fetch_cycle_id=prev_fetch_id,
            previous_poll_timestamp_utc=prev_poll_ts,
            previous_qubit_dates=prev_qubit_dates,
            previous_edge_dates=prev_edge_dates,
        )

        fetch_id = store.insert_successful_cycle(
            backend=backend,
            poll_started_at=poll_started_at,
            poll_finished_at=poll_finished_at,
            raw_backend_summary=raw_backend_summary,
            raw_status=raw_status,
            raw_properties=raw_properties,
            status_snapshot=status,
            parsed_properties=properties,
            metrics=metrics,
        )
        LOG.info(
            "stored backend=%s fetch_id=%s pending_jobs=%s qubits_updated=%s edges_updated=%s max_q_age_s=%s max_e_age_s=%s",
            backend,
            fetch_id,
            status.pending_jobs,
            metrics.num_qubits_calibrated_since_last_fetch,
            metrics.num_edges_calibrated_since_last_fetch,
            metrics.max_qubit_calibration_age_seconds,
            metrics.max_edge_calibration_age_seconds,
        )
        return fetch_id
    except Exception as exc:  # noqa: BLE001 - collector should persist failures too.
        poll_finished_at = utc_now()
        fetch_id = store.insert_failed_cycle(
            backend=backend,
            poll_started_at=poll_started_at,
            poll_finished_at=poll_finished_at,
            error_message=repr(exc),
        )
        LOG.exception("failed backend=%s fetch_id=%s", backend, fetch_id)
        return fetch_id


def run_once(config: CollectorConfig, *, init_db: bool = False) -> None:
    store = PostgresStore(config.database_url)
    if init_db:
        store.ensure_schema()
    client = IBMQuantumClient(
        api_key=config.ibm_api_key,
        service_crn=config.ibm_service_crn,
        host=config.ibm_host,
        api_version=config.ibm_api_version,
        timeout_seconds=config.request_timeout_seconds,
    )
    try:
        raw_backend_summaries = get_backend_summaries(client)
    except Exception:  # noqa: BLE001 - status/properties may still be collectible.
        LOG.exception("failed to fetch backend queue summaries; falling back to status payloads")
        raw_backend_summaries = {}
    for backend in config.backends:
        collect_backend_once(
            backend=backend,
            config=config,
            client=client,
            store=store,
            raw_backend_summary=raw_backend_summaries.get(backend),
        )


def get_backend_summaries(client: IBMQuantumClient) -> dict[str, dict[str, Any]]:
    raw_backends = client.list_backends(include_wait_time_seconds=True)
    devices = raw_backends.get("devices")
    if not isinstance(devices, list):
        raise TypeError("backends.devices must be a list")

    summaries: dict[str, dict[str, Any]] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        name = device.get("name")
        if name is None:
            continue
        summaries[str(name)] = device
    return summaries


def run_forever(config: CollectorConfig, *, init_db: bool = False) -> NoReturn:
    while True:
        started = time.monotonic()
        run_once(config, init_db=init_db)
        init_db = False
        elapsed = time.monotonic() - started
        sleep_for = max(0.0, config.collect_interval_seconds - elapsed)
        LOG.info("sleeping %.1f seconds", sleep_for)
        time.sleep(sleep_for)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect IBM Quantum backend status and calibration properties into PostgreSQL."
    )
    parser.add_argument("--once", action="store_true", help="Run one fetch cycle and exit.")
    parser.add_argument("--init-db", action="store_true", help="Apply sql/schema.sql before collecting.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Override COLLECT_INTERVAL_SECONDS for daemon mode.",
    )
    parser.add_argument(
        "--backends",
        type=str,
        default=None,
        help="Comma-separated backend names, overriding IBM_BACKENDS.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Python logging level.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config = CollectorConfig.from_env()
    if args.interval_seconds is not None:
        config = replace(config, collect_interval_seconds=args.interval_seconds)
    if args.backends is not None:
        backends = [b.strip() for b in args.backends.split(",") if b.strip()]
        config = replace(config, backends=backends)

    if args.once:
        run_once(config, init_db=args.init_db)
    else:
        run_forever(config, init_db=args.init_db)


if __name__ == "__main__":
    main()
