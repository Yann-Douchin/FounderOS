"""Explicit descriptors for connectors reserved by the architecture."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError
from founder_os.models import Event


class PlannedConnector(Connector):
    def __init__(self, config: Mapping[str, Any], *, name: str) -> None:
        super().__init__(config)
        self.name = name
        raise ConnectorConfigurationError(f"connector {name} is planned and must remain disabled in V1")

    def poll(self, now: datetime) -> list[Event]:
        return []
