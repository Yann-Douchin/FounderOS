"""Build configured connectors without giving them access to the display."""

from __future__ import annotations

from typing import Any, Mapping

from founder_os.connectors.agents import AgentBridgeConnector
from founder_os.connectors.base import Connector, ConnectorConfigurationError
from founder_os.connectors.calendar import GoogleCalendarConnector
from founder_os.connectors.commerce import ShopifyConnector, StripeConnector
from founder_os.connectors.demo import DemoConnector
from founder_os.connectors.drive import GoogleDriveConnector, GoogleSheetsConnector
from founder_os.connectors.feed import ChatGPTCodexConnector, ClaudeConnector, LinkedInConnector
from founder_os.connectors.gmail import GmailConnector
from founder_os.connectors.github import DeploymentConnector, GitHubConnector
from founder_os.connectors.home_assistant import HomeAssistantConnector
from founder_os.connectors.linear import LinearConnector
from founder_os.connectors.notion import NotionConnector
from founder_os.connectors.observability import PostHogConnector, SentryConnector
from founder_os.connectors.snapshot import JsonSnapshotConnector
from founder_os.connectors.slack import SlackConnector
from founder_os.connectors.superhuman import SuperhumanReminderConnector
from founder_os.secrets import SecretResolver


ACTIVE_CONNECTORS = {
    "demo": DemoConnector,
    "linear": LinearConnector,
    "slack": SlackConnector,
    "gmail": GmailConnector,
    "calendar": GoogleCalendarConnector,
    "linkedin": LinkedInConnector,
    "claude": ClaudeConnector,
    "chatgpt_codex": ChatGPTCodexConnector,
    "notion": NotionConnector,
    "drive": GoogleDriveConnector,
    "sheets": GoogleSheetsConnector,
    "github": GitHubConnector,
    "deployment": DeploymentConnector,
    "sentry": SentryConnector,
    "posthog": PostHogConnector,
    "shopify": ShopifyConnector,
    "superhuman": SuperhumanReminderConnector,
    "stripe": StripeConnector,
    "home_assistant": HomeAssistantConnector,
}


def build_connectors(
    config: Mapping[str, Any],
    *,
    secrets: SecretResolver | None = None,
) -> list[Connector]:
    result: list[Connector] = []
    for name, connector_config in config.items():
        if not isinstance(connector_config, Mapping) or not connector_config.get("enabled", False):
            continue
        runtime_config = dict(connector_config)
        if secrets is not None:
            runtime_config["_secret_resolver"] = secrets
        mode = str(runtime_config.get("mode", "api")).strip().lower()
        if mode == "snapshot":
            if name not in ACTIVE_CONNECTORS:
                raise ConnectorConfigurationError(f"snapshot mode is not supported for connector: {name}")
            result.append(JsonSnapshotConnector(runtime_config, source=name))
            continue
        if mode == "agent_bridge":
            if name not in {"claude", "chatgpt_codex"}:
                raise ConnectorConfigurationError(f"agent bridge mode is not supported for connector: {name}")
            result.append(AgentBridgeConnector(runtime_config, source=name))
            continue
        if mode != "api":
            raise ConnectorConfigurationError(f"unsupported connector mode for {name}: {mode}")
        connector_type = ACTIVE_CONNECTORS.get(name)
        if connector_type is None:
            raise ConnectorConfigurationError(f"unknown connector: {name}")
        result.append(connector_type(runtime_config))
    return result
