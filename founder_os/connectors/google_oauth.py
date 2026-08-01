"""In-memory Google OAuth access-token refresh without storing credentials."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import ConnectorConfigurationError, ConnectorError
from founder_os.connectors.http_client import request_json
from founder_os.models import UTC, utc_now


class GoogleAccessTokenProvider:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.static_token = _optional_secret(config, "access_token_env")
        self.refresh_token = _optional_secret(config, "refresh_token_env")
        self.client_id = _optional_secret(config, "client_id_env")
        self.client_secret = _optional_secret(config, "client_secret_env")
        refresh_values = (self.refresh_token, self.client_id, self.client_secret)
        if any(refresh_values) and not all(refresh_values):
            raise ConnectorConfigurationError(
                "Google OAuth refresh requires refresh_token_env, client_id_env, and client_secret_env"
            )
        if not self.static_token and not all(refresh_values):
            raise ConnectorConfigurationError(
                "Google connector requires either access_token_env or complete OAuth refresh credentials"
            )
        self.token_uri = str(config.get("token_uri", "https://oauth2.googleapis.com/token"))
        self.timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.refresh_skew = timedelta(seconds=max(10.0, float(config.get("token_refresh_skew_seconds", 60))))
        self._access_token = ""
        self._expires_at: datetime | None = None
        self._refresh_at: datetime | None = None
        self._lock = threading.Lock()

    @property
    def refreshable(self) -> bool:
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def token(self, now: datetime | None = None) -> str:
        now = (now or utc_now()).astimezone(UTC)
        with self._lock:
            if self._access_token and self._refresh_at and now < self._refresh_at:
                return self._access_token
            if not self.refreshable:
                return self.static_token
            payload = request_json(
                self.token_uri,
                method="POST",
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
                retries=0,
            )
            access_token = str(payload.get("access_token", "")).strip()
            try:
                expires_in = max(1.0, float(payload.get("expires_in", 3600)))
            except (TypeError, ValueError):
                expires_in = 3600.0
            if not access_token:
                raise ConnectorError("Google OAuth refresh response did not contain an access token")
            self._access_token = access_token
            self._expires_at = now + timedelta(seconds=expires_in)
            effective_skew = min(self.refresh_skew, timedelta(seconds=max(1.0, expires_in / 2)))
            self._refresh_at = self._expires_at - effective_skew
            return access_token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = ""
            self._expires_at = None
            self._refresh_at = None


def _optional_secret(config: Mapping[str, Any], field: str) -> str:
    environment_name = str(config.get(field, "")).strip()
    return os.environ.get(environment_name, "").strip() if environment_name else ""
