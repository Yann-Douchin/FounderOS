"""Configuration loading with environment-variable interpolation."""

from __future__ import annotations

import json
import os
import re
import ipaddress
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from founder_os.paths import agent_state_root, state_root
from founder_os.secrets import SecretError, SecretResolver, build_secret_resolver


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "secrets": {
        "provider": "environment",
        "service": "com.founderos.runtime",
        "accounts": [],
    },
    "runtime": {
        "environment": "development",
        "timezone": "Europe/Madrid",
        "tick_seconds": 1.0,
        "refresh_seconds": 15.0,
        "event_ttl_minutes": 120,
        "connector_workers": 4,
        "force_poll_timeout_seconds": 30.0,
        "log_level": "INFO",
    },
    "operations": {
        "health_enabled": False,
        "health_path": str(state_root() / "health.json"),
        "heartbeat_seconds": 15.0,
    },
    "display": {
        "host": "127.0.0.1:8080",
        "application_name": "founderos",
        "device_priority": 90,
        "request_timeout_seconds": 3.0,
        "api_token_env": "BUSY_API_TOKEN",
        "api_semver": "25.0.0",
        "validate_on_start": True,
        "allow_insecure_http": False,
        "min_hold_seconds": 6.0,
        "show_idle": True,
        "content_icon": {
            "enabled": True,
            "frame_seconds": 1.0,
        },
    },
    "interaction": {
        "enabled": False,
        "mode": "signed_http",
        "event_url": "",
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "secret_env": "FOUNDEROS_INPUT_SECRET",
        "max_clock_skew_seconds": 30,
        "allow_key": "ok",
        "deny_key": "back",
        "acknowledge_key": "ok",
        "snooze_key": "back",
        "open_key": "custom",
        "snooze_minutes": 15,
        "action_outbox_path": str(state_root() / "actions"),
        "action_outbox_max_pending": 1000,
        "reconnect_seconds": 1.0,
    },
    "ranking": {
        "tie_threshold": 2.0,
        "source_weights": {
            "linear": 8,
            "slack": 4,
            "gmail": 2,
            "calendar": 6,
            "linkedin": 0,
            "claude": 1,
            "chatgpt_codex": 2,
        },
        "action_required_bonus": 12,
        "current_selection_bonus": 3,
        "fresh_minutes": 15,
        "age_penalty_per_hour": 1.5,
        "repeat_penalty": 7,
        "repeat_window_minutes": 20,
    },
    "memory": {
        "path": str(state_root() / "memory.json"),
        "retention_days": 30,
        "max_entries": 5000,
    },
    "llm": {
        "enabled": False,
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 8,
        "max_calls_per_hour": 6,
        "max_candidates": 4,
        "max_title_chars": 160,
        "max_output_tokens": 64,
    },
    "connectors": {
        "demo": {"enabled": False, "poll_interval_seconds": 2},
        "linear": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 30,
            "poll_timeout_seconds": 15,
            "request_timeout_seconds": 6,
            "critical": True,
            "token_env": "LINEAR_API_KEY",
            "refresh_token_env": "LINEAR_REFRESH_TOKEN",
            "client_id_env": "LINEAR_CLIENT_ID",
            "client_secret_env": "LINEAR_CLIENT_SECRET",
            "oauth_refresh": False,
            "auth_scheme": "api_key",
            "scope": "assigned",
            "team_keys": [],
            "page_size": 50,
            "max_issues": 200,
            "max_pages": 20,
            "portfolio_priority_ceiling": 2,
            "portfolio_due_horizon_days": 14,
            "rollup_projects": True,
            "timezone": "Europe/Madrid",
        },
        "slack": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 20,
            "request_timeout_seconds": 6,
            "critical": True,
            "token_env": "SLACK_BOT_TOKEN",
            "channel_ids": [],
            "mention_markers": [],
        },
        "gmail": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 20,
            "request_timeout_seconds": 4,
            "critical": True,
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "refresh_token_env": "GOOGLE_REFRESH_TOKEN",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "query": "is:unread newer_than:2d",
            "vip_senders": [],
        },
        "calendar": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 20,
            "request_timeout_seconds": 6,
            "critical": True,
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "refresh_token_env": "GOOGLE_REFRESH_TOKEN",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "calendar_id": "primary",
            "horizon_hours": 8,
            "page_size": 20,
            "max_events": 100,
            "max_pages": 20,
            "readiness_minutes": 30,
            "readiness_keywords": [
                "launch", "go-live", "client", "customer", "investor",
                "investisseur", "demo", "contrat", "contract", "stratégie", "strategy",
            ],
            "timezone": "Europe/Madrid",
        },
        "linkedin": {
            "enabled": False,
            "poll_interval_seconds": 120,
            "feed_url": "",
            "token_env": "LINKEDIN_FEED_TOKEN",
        },
        "claude": {
            "enabled": False,
            "mode": "agent_bridge",
            "poll_interval_seconds": 2,
            "state_dir": str(agent_state_root()),
            "usage": {
                "mode": "snapshot",
                "ttl_seconds": 900,
            },
        },
        "chatgpt_codex": {
            "enabled": False,
            "mode": "agent_bridge",
            "poll_interval_seconds": 2,
            "state_dir": str(agent_state_root()),
            "usage": {
                "mode": "codex_app_server",
                "codex_binary": "",
                "timeout_seconds": 5,
                "ttl_seconds": 120,
                "refresh_seconds": 60,
            },
        },
        "github": {"enabled": False, "status": "planned"},
        "stripe": {"enabled": False, "status": "planned"},
        "shopify": {"enabled": False, "status": "planned"},
        "home_assistant": {"enabled": False, "status": "planned"},
    },
}


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _merge(dict(base[key]), value)
        else:
            base[key] = deepcopy(value)
    return base


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str):
        def replacement(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"missing environment variable referenced by configuration: {name}")
            return os.environ[name]

        return _ENV_PATTERN.sub(replacement, value)
    return value


def load_config(path: str | Path | None = None, *, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    current_state_root = state_root()
    config["memory"]["path"] = str(current_state_root / "memory.json")
    config["operations"]["health_path"] = str(current_state_root / "health.json")
    config["interaction"]["action_outbox_path"] = str(current_state_root / "actions")
    current_agent_root = str(current_state_root / "agents")
    config["connectors"]["claude"]["state_dir"] = current_agent_root
    config["connectors"]["chatgpt_codex"]["state_dir"] = current_agent_root
    if path:
        config_path = Path(path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"configuration not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError("configuration root must be an object")
        _merge(config, payload)
    if overrides:
        _merge(config, overrides)
    config = _expand_env(config)
    try:
        secrets = build_secret_resolver(config["secrets"])
        _validate(config, secrets=secrets)
    except SecretError as exc:
        raise ConfigError(str(exc)) from exc
    return config


def _validate(config: Mapping[str, Any], *, secrets: SecretResolver | None = None) -> None:
    secret_config = config["secrets"]
    provider = str(secret_config["provider"]).strip().lower()
    if provider not in {"environment", "macos_keychain"}:
        raise ConfigError("secrets.provider must be environment or macos_keychain")
    accounts = secret_config.get("accounts")
    if not isinstance(accounts, list) or not all(isinstance(value, str) and value.strip() for value in accounts):
        raise ConfigError("secrets.accounts must be a list of non-empty environment names")
    operations = config["operations"]
    if float(operations["heartbeat_seconds"]) <= 0:
        raise ConfigError("operations.heartbeat_seconds must be positive")
    runtime = config["runtime"]
    environment = str(runtime["environment"]).strip().lower()
    if environment not in {"development", "test", "production"}:
        raise ConfigError("runtime.environment must be development, test, or production")
    timezone_name = str(runtime["timezone"]).strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"runtime.timezone is unknown: {timezone_name}") from exc
    if int(runtime["connector_workers"]) < 1:
        raise ConfigError("runtime.connector_workers must be positive")
    if float(runtime["force_poll_timeout_seconds"]) <= 0:
        raise ConfigError("runtime.force_poll_timeout_seconds must be positive")
    display = config["display"]
    device_priority = int(display["device_priority"])
    if not 1 <= device_priority <= 100:
        raise ConfigError("display.device_priority must be between 1 and 100")
    if float(display["min_hold_seconds"]) < 0:
        raise ConfigError("display.min_hold_seconds cannot be negative")
    if float(display["content_icon"]["frame_seconds"]) <= 0:
        raise ConfigError("display.content_icon.frame_seconds must be positive")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(display["api_semver"])):
        raise ConfigError("display.api_semver must use semantic version form, for example 25.0.0")
    display_host = str(display["host"])
    parsed_display = urlsplit(display_host if "://" in display_host else "http://" + display_host)
    if parsed_display.scheme not in {"http", "https"} or not parsed_display.hostname:
        raise ConfigError("display.host must be an HTTP or HTTPS endpoint with a host")
    if parsed_display.username or parsed_display.password:
        raise ConfigError("display.host must not contain credentials")
    interaction = config["interaction"]
    mode = str(interaction["mode"]).strip().lower()
    if mode not in {"emulator_sse", "signed_http"}:
        raise ConfigError("interaction.mode must be emulator_sse or signed_http")
    if bool(interaction["enabled"]) and environment == "production" and mode == "emulator_sse":
        raise ConfigError("interaction.emulator_sse is untrusted and cannot be enabled in production")
    if mode == "signed_http":
        if str(interaction["listen_host"]).strip() not in {"127.0.0.1", "localhost"}:
            raise ConfigError("interaction.listen_host must be loopback for signed_http")
        if not 1 <= int(interaction["listen_port"]) <= 65535:
            raise ConfigError("interaction.listen_port must be between 1 and 65535")
        if not str(interaction["secret_env"]).strip():
            raise ConfigError("interaction.secret_env is required for signed_http")
        if float(interaction["max_clock_skew_seconds"]) <= 0:
            raise ConfigError("interaction.max_clock_skew_seconds must be positive")
    if str(interaction["allow_key"]).strip() == str(interaction["deny_key"]).strip():
        raise ConfigError("interaction allow and deny keys must differ")
    if float(interaction["reconnect_seconds"]) <= 0:
        raise ConfigError("interaction.reconnect_seconds must be positive")
    if int(interaction["action_outbox_max_pending"]) < 1:
        raise ConfigError("interaction.action_outbox_max_pending must be positive")
    if float(runtime["tick_seconds"]) <= 0:
        raise ConfigError("runtime.tick_seconds must be positive")
    if float(runtime["refresh_seconds"]) <= 0:
        raise ConfigError("runtime.refresh_seconds must be positive")
    if float(runtime["event_ttl_minutes"]) <= 0:
        raise ConfigError("runtime.event_ttl_minutes must be positive")
    if float(config["ranking"]["tie_threshold"]) < 0:
        raise ConfigError("ranking.tie_threshold cannot be negative")
    if float(config["llm"]["timeout_seconds"]) <= 0:
        raise ConfigError("llm.timeout_seconds must be positive")
    if int(config["llm"]["max_calls_per_hour"]) < 0:
        raise ConfigError("llm.max_calls_per_hour cannot be negative")
    if int(config["llm"]["max_candidates"]) < 2:
        raise ConfigError("llm.max_candidates must be at least 2")
    if int(config["llm"]["max_title_chars"]) < 32:
        raise ConfigError("llm.max_title_chars must be at least 32")
    if int(config["llm"]["max_output_tokens"]) < 16:
        raise ConfigError("llm.max_output_tokens must be at least 16")
    if float(config["memory"]["retention_days"]) <= 0:
        raise ConfigError("memory.retention_days must be positive")
    if int(config["memory"]["max_entries"]) < 100:
        raise ConfigError("memory.max_entries must be at least 100")
    linear = config["connectors"]["linear"]
    if str(linear.get("scope", "assigned")).strip().lower() not in {"assigned", "portfolio"}:
        raise ConfigError("connectors.linear.scope must be assigned or portfolio")
    if str(linear.get("auth_scheme", "api_key")).strip().lower() not in {"api_key", "bearer"}:
        raise ConfigError("connectors.linear.auth_scheme must be api_key or bearer")
    linear_refresh_names = (
        str(linear.get("refresh_token_env", "")).strip(),
        str(linear.get("client_id_env", "")).strip(),
    )
    linear_refresh_enabled = bool(linear.get("oauth_refresh", False))
    if linear_refresh_enabled and not all(linear_refresh_names):
        raise ConfigError("Linear OAuth refresh requires refresh_token_env and client_id_env")
    if linear_refresh_enabled and str(linear.get("auth_scheme", "")).strip().lower() != "bearer":
        raise ConfigError("Linear OAuth refresh requires auth_scheme bearer")
    team_keys = linear.get("team_keys")
    if not isinstance(team_keys, list) or not all(str(value).strip() for value in team_keys):
        raise ConfigError("connectors.linear.team_keys must be a list of non-empty keys")
    page_size = int(linear.get("page_size", 50))
    max_issues = int(linear.get("max_issues", 200))
    if not 1 <= page_size <= 100:
        raise ConfigError("connectors.linear.page_size must be between 1 and 100")
    if not page_size <= max_issues <= 500:
        raise ConfigError("connectors.linear.max_issues must be between page_size and 500")
    if not 1 <= int(linear.get("max_pages", 20)) <= 50:
        raise ConfigError("connectors.linear.max_pages must be between 1 and 50")
    if float(linear.get("portfolio_due_horizon_days", 14)) <= 0:
        raise ConfigError("connectors.linear.portfolio_due_horizon_days must be positive")
    if not 1 <= int(linear.get("portfolio_priority_ceiling", 2)) <= 4:
        raise ConfigError("connectors.linear.portfolio_priority_ceiling must be between 1 and 4")
    portfolio_enabled = (
        linear.get("enabled")
        and linear.get("mode", "api") == "api"
        and linear.get("scope") == "portfolio"
    )
    if portfolio_enabled and not team_keys:
        raise ConfigError("connectors.linear.team_keys is required for portfolio scope")
    gmail = config["connectors"]["gmail"]
    for field in ("action_keywords", "fyi_keywords", "urgent_keywords", "non_action_keywords"):
        if field in gmail and not isinstance(gmail[field], list):
            raise ConfigError(f"connectors.gmail.{field} must be a list")
    slack = config["connectors"]["slack"]
    for field in ("urgent_keywords", "risk_keywords", "decision_keywords"):
        if field in slack and not isinstance(slack[field], list):
            raise ConfigError(f"connectors.slack.{field} must be a list")
    calendar = config["connectors"]["calendar"]
    calendar_page_size = int(calendar.get("page_size", 20))
    calendar_max_events = int(calendar.get("max_events", 100))
    if not 1 <= calendar_page_size <= 250:
        raise ConfigError("connectors.calendar.page_size must be between 1 and 250")
    if not calendar_page_size <= calendar_max_events <= 500:
        raise ConfigError("connectors.calendar.max_events must be between page_size and 500")
    if not 1 <= int(calendar.get("max_pages", 20)) <= 50:
        raise ConfigError("connectors.calendar.max_pages must be between 1 and 50")
    if float(calendar.get("readiness_minutes", 30)) < 5:
        raise ConfigError("connectors.calendar.readiness_minutes must be at least 5")
    if not isinstance(calendar.get("readiness_keywords", []), list):
        raise ConfigError("connectors.calendar.readiness_keywords must be a list")
    display_hostname = parsed_display.hostname or ""
    try:
        display_is_loopback = ipaddress.ip_address(display_hostname).is_loopback
    except ValueError:
        display_is_loopback = display_hostname == "localhost"
    if provider == "macos_keychain":
        allowed_accounts = {str(value).strip() for value in accounts}

        def require_account(name: Any, context: str) -> None:
            account = str(name or "").strip()
            if not account or account not in allowed_accounts:
                raise ConfigError(f"{context} must be listed in secrets.accounts: {account or '<empty>'}")

        if linear.get("enabled") and linear.get("mode", "api") == "api":
            if linear_refresh_enabled:
                require_account(linear.get("refresh_token_env"), "Linear refresh token")
                require_account(linear.get("client_id_env"), "Linear client id")
            else:
                require_account(linear.get("token_env"), "Linear API token")
        if slack.get("enabled") and slack.get("mode", "api") == "api":
            require_account(slack.get("token_env"), "Slack token")
        for connector_name, google_connector in (("Gmail", gmail), ("Calendar", calendar)):
            if not google_connector.get("enabled") or google_connector.get("mode", "api") != "api":
                continue
            access_account = str(google_connector.get("access_token_env", "")).strip()
            refresh_account = str(google_connector.get("refresh_token_env", "")).strip()
            client_account = str(google_connector.get("client_id_env", "")).strip()
            static_allowed = bool(access_account and access_account in allowed_accounts)
            refresh_allowed = bool(
                refresh_account
                and refresh_account in allowed_accounts
                and client_account
                and client_account in allowed_accounts
            )
            if not static_allowed and not refresh_allowed:
                raise ConfigError(
                    f"{connector_name} requires either its access token or both refresh token and client id "
                    "in secrets.accounts"
                )
        linkedin = config["connectors"]["linkedin"]
        if linkedin.get("enabled") and str(linkedin.get("feed_url", "")).strip():
            require_account(linkedin.get("token_env"), "LinkedIn feed token")
        if bool(config["llm"]["enabled"]):
            require_account(config["llm"].get("api_key_env"), "LLM API key")
        if bool(interaction["enabled"]) and mode == "signed_http":
            require_account(interaction.get("secret_env"), "signed input secret")
        if not display_is_loopback:
            require_account(display.get("api_token_env"), "BUSY Bar API token")
    if environment == "production":
        demo = config["connectors"].get("demo")
        if isinstance(demo, Mapping) and demo.get("enabled"):
            raise ConfigError("the demo connector cannot be enabled in production")
        enabled_connectors = [
            name
            for name, connector in config["connectors"].items()
            if isinstance(connector, Mapping)
            and connector.get("enabled")
            and name not in {"demo", "github", "stripe", "shopify", "home_assistant"}
        ]
        if not enabled_connectors:
            raise ConfigError("production requires at least one active connector")
        private_paths = [Path(str(config["memory"]["path"])), Path(str(interaction["action_outbox_path"]))]
        if bool(operations["health_enabled"]):
            private_paths.append(Path(str(operations["health_path"])))
        for name in ("claude", "chatgpt_codex"):
            connector = config["connectors"].get(name)
            if isinstance(connector, Mapping):
                private_paths.append(Path(str(connector.get("state_dir", ""))))
        for connector in config["connectors"].values():
            if isinstance(connector, Mapping) and connector.get("enabled") and connector.get("mode") == "snapshot":
                private_paths.append(Path(str(connector.get("snapshot_path", ""))))
        approved_state_root = state_root().resolve()
        if approved_state_root == PROJECT_ROOT or approved_state_root.is_relative_to(PROJECT_ROOT):
            raise ConfigError("FOUNDEROS_STATE_DIR must be outside the source checkout in production")
        invalid_paths = [path for path in private_paths if not path.expanduser().is_absolute() or not path.expanduser().resolve().is_relative_to(approved_state_root)]
        if invalid_paths:
            raise ConfigError(f"production private paths must be inside FOUNDEROS_STATE_DIR ({approved_state_root}): {invalid_paths}")
        is_loopback = display_is_loopback
        token_env = str(display.get("api_token_env", "")).strip()
        secret_resolver = secrets or SecretResolver()
        if not is_loopback and (not token_env or not secret_resolver.get(token_env)):
            raise ConfigError("a BUSY Bar API token is required for a non-loopback production display")
        if (
            not is_loopback
            and parsed_display.scheme != "https"
            and not bool(display.get("allow_insecure_http", False))
        ):
            raise ConfigError(
                "non-loopback production HTTP requires display.allow_insecure_http=true"
            )
        if linear_refresh_enabled and not secret_resolver.persistent:
            raise ConfigError("production Linear OAuth refresh requires a persistent secret provider")


def redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy safe to print in logs."""
    secret_markers = ("token", "secret", "password", "api_key")

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if any(marker in key.lower() for marker in secret_markers) and not key.lower().endswith("_env"):
                    result[key] = "***" if item else item
                else:
                    result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(config)
