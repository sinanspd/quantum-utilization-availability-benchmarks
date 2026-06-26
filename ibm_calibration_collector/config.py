from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectorConfig:
    ibm_api_key: str
    ibm_service_crn: str
    ibm_host: str
    ibm_api_version: str
    backends: list[str]
    database_url: str
    collect_interval_seconds: int
    edge_id_mode: str
    request_timeout_seconds: int

    @staticmethod
    def from_env() -> "CollectorConfig":
        api_key = _required("IBM_QUANTUM_API_KEY")
        crn = _required("IBM_SERVICE_CRN")
        database_url = _required("DATABASE_URL")
        host = os.getenv("IBM_HOST", "quantum.cloud.ibm.com").strip()
        api_version = os.getenv("IBM_API_VERSION", "2026-02-15").strip()
        backends_raw = _required("IBM_BACKENDS")
        backends = [b.strip() for b in backends_raw.split(",") if b.strip()]
        if not backends:
            raise ValueError("IBM_BACKENDS must contain at least one backend name")

        interval = int(os.getenv("COLLECT_INTERVAL_SECONDS", "900"))
        if interval <= 0:
            raise ValueError("COLLECT_INTERVAL_SECONDS must be positive")

        edge_id_mode = os.getenv("EDGE_ID_MODE", "undirected").strip().lower()
        if edge_id_mode not in {"undirected", "directed"}:
            raise ValueError("EDGE_ID_MODE must be either 'undirected' or 'directed'")

        timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
        if timeout <= 0:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be positive")

        return CollectorConfig(
            ibm_api_key=api_key,
            ibm_service_crn=crn,
            ibm_host=host,
            ibm_api_version=api_version,
            backends=backends,
            database_url=database_url,
            collect_interval_seconds=interval,
            edge_id_mode=edge_id_mode,
            request_timeout_seconds=timeout,
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()
