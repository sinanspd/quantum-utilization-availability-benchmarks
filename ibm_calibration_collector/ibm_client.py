from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


@dataclass
class _BearerToken:
    access_token: str
    expires_at: datetime


class IBMQuantumClient:

    def __init__(
        self,
        api_key: str,
        service_crn: str,
        host: str = "quantum.cloud.ibm.com",
        api_version: str = "2026-02-15",
        timeout_seconds: int = 60,
    ) -> None:
        self.api_key = api_key
        self.service_crn = service_crn
        self.host = host.removeprefix("https://").rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self._token: _BearerToken | None = None

    def get_backend_status(self, backend: str) -> dict[str, Any]:
        return self._get_json(f"/api/v1/backends/{backend}/status")

    def get_backend_properties(self, backend: str) -> dict[str, Any]:
        return self._get_json(f"/api/v1/backends/{backend}/properties")

    def list_backends(self, *, include_wait_time_seconds: bool = False) -> dict[str, Any]:
        params = {"fields": "wait_time_seconds"} if include_wait_time_seconds else None
        return self._get_json("/api/v1/backends", params=params)

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = self._get_bearer_token()
        url = f"https://{self.host}{path}"
        response = self.session.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Service-CRN": self.service_crn,
                "IBM-API-Version": self.api_version,
            },
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError(f"Expected object from {url}, got {type(data).__name__}")
        if data.get("errors"):
            raise RuntimeError(f"IBM API returned errors from {url}: {data['errors']}")
        return data

    def _get_bearer_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token is not None and self._token.expires_at - now > timedelta(seconds=60):
            return self._token.access_token

        response = self.session.post(
            "https://iam.cloud.ibm.com/identity/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": self.api_key,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        self._token = _BearerToken(
            access_token=token,
            expires_at=now + timedelta(seconds=max(60, expires_in)),
        )
        return token
