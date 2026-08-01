"""Connector contracts and configuration helpers."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Mapping

from founder_os.models import Event


class ConnectorError(RuntimeError):
    pass


class ConnectorConfigurationError(ConnectorError):
    pass


class ConnectorUnavailableError(ConnectorError):
    pass


class ConnectorStaleError(ConnectorError):
    pass


class Connector(ABC):
    name: str
    emits_snapshot = True

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.poll_interval_seconds = max(1.0, float(config.get("poll_interval_seconds", 60)))
        self.poll_timeout_seconds = max(1.0, float(config.get("poll_timeout_seconds", 20)))
        self.critical = bool(config.get("critical", False))

    @abstractmethod
    def poll(self, now: datetime) -> list[Event]:
        raise NotImplementedError

    def close(self) -> None:
        return None


def configured_secret(config: Mapping[str, Any], env_field: str) -> str:
    env_name = str(config.get(env_field, "")).strip()
    value = os.environ.get(env_name, "").strip() if env_name else ""
    if not value:
        raise ConnectorConfigurationError(f"missing environment variable configured by {env_field}: {env_name or '<empty>'}")
    return value
