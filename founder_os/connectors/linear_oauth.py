"""Linear access-token refresh with durable refresh-token rotation."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import ConnectorConfigurationError, ConnectorError
from founder_os.connectors.http_client import request_json
from founder_os.models import UTC, utc_now
from founder_os.secrets import SecretError, SecretResolver


class LinearAccessTokenProvider:
    def __init__(self, config: Mapping[str, Any], *, secrets: SecretResolver | None = None) -> None:
        self.secrets = secrets or SecretResolver()
        self.auth_scheme = str(config.get("auth_scheme", "api_key")).strip().lower()
        self.oauth_refresh = bool(config.get("oauth_refresh", False))
        self.token_env = str(config.get("token_env", "LINEAR_API_KEY")).strip()
        self.refresh_token_env = str(config.get("refresh_token_env", "LINEAR_REFRESH_TOKEN")).strip()
        self.client_id_env = str(config.get("client_id_env", "LINEAR_CLIENT_ID")).strip()
        self.client_secret_env = str(config.get("client_secret_env", "LINEAR_CLIENT_SECRET")).strip()
        self.token_uri = str(config.get("token_uri", "https://api.linear.app/oauth/token"))
        self.timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.refresh_skew = timedelta(seconds=max(10.0, float(config.get("token_refresh_skew_seconds", 60))))
        self._access_token = ""
        self._refresh_at: datetime | None = None
        self._lock = threading.Lock()

        if self.auth_scheme not in {"api_key", "bearer"}:
            raise ConnectorConfigurationError("linear.auth_scheme must be api_key or bearer")
        if self.oauth_refresh:
            if self.auth_scheme != "bearer":
                raise ConnectorConfigurationError("Linear OAuth refresh requires bearer authentication")
            if not self.secrets.persistent:
                raise ConnectorConfigurationError(
                    "Linear OAuth refresh requires a persistent secret store for token rotation"
                )
            self.refresh_token = self.secrets.require(
                self.refresh_token_env,
                context="Linear OAuth refresh token",
            )
            self.client_id = self.secrets.require(
                self.client_id_env,
                context="Linear OAuth client id",
            )
            self.client_secret = self.secrets.get(self.client_secret_env) if self.client_secret_env else ""
            self.static_token = ""
        else:
            self.static_token = self.secrets.require(self.token_env, context="Linear API token")
            self.refresh_token = ""
            self.client_id = ""
            self.client_secret = ""

    @property
    def refreshable(self) -> bool:
        return self.oauth_refresh

    def token(
        self,
        now: datetime | None = None,
        *,
        deadline_monotonic: float | None = None,
    ) -> str:
        if not self.oauth_refresh:
            return self.static_token
        now = (now or utc_now()).astimezone(UTC)
        with self._lock:
            if self._access_token and self._refresh_at and now < self._refresh_at:
                return self._access_token
            timeout = self.timeout
            if deadline_monotonic is not None:
                remaining = float(deadline_monotonic) - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Linear OAuth refresh exceeded its deadline")
                timeout = min(timeout, remaining)
            form = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
            }
            if self.client_secret:
                form["client_secret"] = self.client_secret
            payload = request_json(
                self.token_uri,
                method="POST",
                form=form,
                timeout=timeout,
                retries=0,
                deadline_monotonic=deadline_monotonic,
            )
            access_token = str(payload.get("access_token", "")).strip()
            rotated_refresh = str(payload.get("refresh_token", "")).strip()
            if not access_token or not rotated_refresh:
                raise ConnectorError("Linear OAuth refresh response did not contain both rotated tokens")
            try:
                expires_in = max(1.0, float(payload.get("expires_in", 86400)))
            except (TypeError, ValueError):
                expires_in = 86400.0
            if rotated_refresh != self.refresh_token:
                try:
                    self.secrets.persist(self.refresh_token_env, rotated_refresh)
                except SecretError as exc:
                    raise ConnectorError("Linear refresh token rotation could not be persisted") from exc
                self.refresh_token = rotated_refresh
            self._access_token = access_token
            effective_skew = min(self.refresh_skew, timedelta(seconds=max(1.0, expires_in / 2)))
            self._refresh_at = now + timedelta(seconds=expires_in) - effective_skew
            return self._access_token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = ""
            self._refresh_at = None

    def authorization(
        self,
        now: datetime | None = None,
        *,
        deadline_monotonic: float | None = None,
    ) -> str:
        token = self.token(now, deadline_monotonic=deadline_monotonic)
        return token if self.auth_scheme == "api_key" else f"Bearer {token}"
