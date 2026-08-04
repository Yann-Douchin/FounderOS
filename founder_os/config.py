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
    "automations": {
        "calendar_busy_indicator": {
            "enabled": False,
            "mode": "busybar_matter",
            "host": "",
            "api_token_env": "",
            "api_semver": "",
            "request_timeout_seconds": 3.0,
            "allow_insecure_http": False,
            "require_pairing": True,
            "include_all_day": False,
            "include_tentative": True,
            "off_delay_seconds": 15.0,
            "verify_interval_seconds": 60.0,
            "retry_seconds": 5.0,
            "retry_max_seconds": 60.0,
            "force_wait_seconds": 15.0,
            "lease_min_ttl_seconds": 5.0,
            "lease_max_ttl_seconds": 28_800.0,
        },
    },
    "closure": {
        "enabled": False,
        "rank_raw_events": False,
        "ledger_path": str(state_root() / "obligations.sqlite3"),
        "snapshot_path": str(state_root() / "obligations.json"),
        "snapshot_interval_seconds": 15,
        "audit_max_entries": 100000,
        "default_owner": "self",
        "self_aliases": ["self", "me"],
        "timezone": "Europe/Madrid",
        "source_priority_cap": 72,
        "burst_window_minutes": 240,
        "burst_threshold": 4,
        "stale_after_days": 45,
        "evidence_ttl_hours": 168,
        "event_lease_seconds": 180,
        "entity_aliases": {},
        "capacity": {
            "due_day_threshold": 5,
            "require_handoff_when_unavailable": True,
        },
        "proof_profiles": {
            "release": {
                "required_categories": [
                    "deployment", "analytics", "market", "language", "pricing", "device",
                ],
                "minimum_categories": 6,
                "required_scopes": {},
            },
            "commitment": {"required_categories": [], "minimum_categories": 0},
            "decision": {"required_categories": [], "minimum_categories": 0},
            "feedback": {"required_categories": [], "minimum_categories": 0},
            "meeting": {"required_categories": [], "minimum_categories": 0},
        },
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
        "lease_seconds": 300.0,
        "lease_refresh_ratio": 0.8,
        "conflict_retry_seconds": 2.0,
        "conflict_retry_max_seconds": 30.0,
        "clear_on_shutdown": True,
        "text_rendering": "auto",
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
        "action_consumer_enabled": True,
        "action_consumer_poll_seconds": 0.5,
        "action_consumer_max_history": 1000,
        "action_consumer_max_age_seconds": 300.0,
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
            "closure": 10,
            "notion": 4,
            "drive": 3,
            "sheets": 5,
            "github": 6,
            "deployment": 8,
            "sentry": 8,
            "posthog": 7,
            "shopify": 6,
            "superhuman": 4,
            "stripe": 5,
            "home_assistant": 0,
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
            "channel_projects": {},
            "channel_relationships": {},
            "channel_customers": {},
            "mention_markers": [],
            "self_user_ids": [],
            "max_threads_per_poll": 10,
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
            "availability_owner_map": {},
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
        "notion": {
            "enabled": False,
            "mode": "api",
            "token_env": "NOTION_API_TOKEN",
            "database_ids": [],
            "allow_all_shared_pages": False,
            "poll_interval_seconds": 120,
            "poll_timeout_seconds": 20,
        },
        "drive": {
            "enabled": False,
            "mode": "api",
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "refresh_token_env": "GOOGLE_REFRESH_TOKEN",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "folder_ids": [],
            "allow_all_files": False,
            "poll_interval_seconds": 120,
            "poll_timeout_seconds": 20,
        },
        "sheets": {
            "enabled": False,
            "mode": "api",
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "refresh_token_env": "GOOGLE_REFRESH_TOKEN",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "spreadsheets": [],
            "poll_interval_seconds": 120,
            "poll_timeout_seconds": 20,
        },
        "github": {
            "enabled": False,
            "mode": "api",
            "token_env": "GITHUB_TOKEN",
            "repositories": [],
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 20,
        },
        "deployment": {
            "enabled": False,
            "mode": "api",
            "endpoint": "",
            "token_env": "DEPLOYMENT_API_TOKEN",
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 15,
        },
        "sentry": {
            "enabled": False,
            "mode": "api",
            "token_env": "SENTRY_AUTH_TOKEN",
            "organization": "",
            "projects": [],
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 20,
        },
        "posthog": {
            "enabled": False,
            "mode": "api",
            "token_env": "POSTHOG_PERSONAL_API_KEY",
            "project_id": "",
            "checks": [],
            "poll_interval_seconds": 120,
            "poll_timeout_seconds": 20,
        },
        "shopify": {
            "enabled": False,
            "mode": "api",
            "token_env": "SHOPIFY_ACCESS_TOKEN",
            "shop": "",
            "required_scopes": ["read_products"],
            "poll_interval_seconds": 300,
            "poll_timeout_seconds": 20,
        },
        "superhuman": {
            "enabled": False,
            "mode": "api",
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "refresh_token_env": "GOOGLE_REFRESH_TOKEN",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "query": "label:reminder newer_than:30d",
            "poll_interval_seconds": 120,
            "poll_timeout_seconds": 20,
        },
        "stripe": {
            "enabled": False,
            "mode": "api",
            "token_env": "STRIPE_RESTRICTED_KEY",
            "poll_interval_seconds": 300,
            "poll_timeout_seconds": 20,
        },
        "home_assistant": {
            "enabled": False,
            "mode": "api",
            "endpoint": "",
            "token_env": "HOME_ASSISTANT_TOKEN",
            "entities": [],
            "poll_interval_seconds": 300,
            "poll_timeout_seconds": 15,
        },
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
    config["closure"]["ledger_path"] = str(current_state_root / "obligations.sqlite3")
    config["closure"]["snapshot_path"] = str(current_state_root / "obligations.json")
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
    closure = config["closure"]
    if not isinstance(closure.get("self_aliases"), list) or not all(
        isinstance(value, str) and value.strip()
        for value in closure.get("self_aliases", [])
    ):
        raise ConfigError("closure.self_aliases must be a list of non-empty strings")
    if not isinstance(closure.get("entity_aliases", {}), Mapping):
        raise ConfigError("closure.entity_aliases must be an object")
    if not str(closure.get("default_owner", "")).strip():
        raise ConfigError("closure.default_owner is required")
    if not 1 <= int(closure.get("source_priority_cap", 72)) <= 100:
        raise ConfigError("closure.source_priority_cap must be between 1 and 100")
    if float(closure.get("burst_window_minutes", 240)) <= 0:
        raise ConfigError("closure.burst_window_minutes must be positive")
    if int(closure.get("burst_threshold", 4)) < 2:
        raise ConfigError("closure.burst_threshold must be at least 2")
    if float(closure.get("stale_after_days", 45)) <= 0:
        raise ConfigError("closure.stale_after_days must be positive")
    if float(closure.get("evidence_ttl_hours", 168)) <= 0:
        raise ConfigError("closure.evidence_ttl_hours must be positive")
    if float(closure.get("event_lease_seconds", 180)) < 30:
        raise ConfigError("closure.event_lease_seconds must be at least 30")
    if float(closure.get("snapshot_interval_seconds", 15)) <= 0:
        raise ConfigError("closure.snapshot_interval_seconds must be positive")
    if not 1_000 <= int(closure.get("audit_max_entries", 100_000)) <= 10_000_000:
        raise ConfigError("closure.audit_max_entries must be between 1000 and 10000000")
    try:
        ZoneInfo(str(closure.get("timezone", runtime["timezone"])))
    except ZoneInfoNotFoundError as exc:
        raise ConfigError("closure.timezone is unknown") from exc
    capacity = closure.get("capacity")
    if not isinstance(capacity, Mapping) or int(capacity.get("due_day_threshold", 5)) < 2:
        raise ConfigError("closure.capacity.due_day_threshold must be at least 2")
    profiles = closure.get("proof_profiles")
    if not isinstance(profiles, Mapping):
        raise ConfigError("closure.proof_profiles must be an object")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, Mapping) or not isinstance(profile.get("required_categories", []), list):
            raise ConfigError(f"closure.proof_profiles.{profile_name} must define a category list")
        required = [str(value).strip().casefold() for value in profile.get("required_categories", []) if str(value).strip()]
        minimum = int(profile.get("minimum_categories", len(required)))
        if not 0 <= minimum <= len(set(required)):
            raise ConfigError(f"closure.proof_profiles.{profile_name}.minimum_categories is invalid")
        required_scopes = profile.get("required_scopes", {})
        if not isinstance(required_scopes, Mapping):
            raise ConfigError(f"closure.proof_profiles.{profile_name}.required_scopes must be an object")
        for category, scopes in required_scopes.items():
            if str(category).strip().casefold() not in required or not isinstance(scopes, list) or not all(str(scope).strip() for scope in scopes):
                raise ConfigError(f"closure.proof_profiles.{profile_name}.required_scopes is invalid")
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
    if float(display["lease_seconds"]) < 30:
        raise ConfigError("display.lease_seconds must be at least 30 seconds")
    lease_refresh_ratio = float(display["lease_refresh_ratio"])
    if not 0.5 <= lease_refresh_ratio < 1:
        raise ConfigError("display.lease_refresh_ratio must be between 0.5 and 1")
    if float(display["conflict_retry_seconds"]) <= 0:
        raise ConfigError("display.conflict_retry_seconds must be positive")
    if float(display["conflict_retry_max_seconds"]) < float(display["conflict_retry_seconds"]):
        raise ConfigError("display.conflict_retry_max_seconds must not be below conflict_retry_seconds")
    if str(display["text_rendering"]).strip().lower() not in {"auto", "native", "raster_non_ascii"}:
        raise ConfigError("display.text_rendering must be auto, native, or raster_non_ascii")
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
    automations = config["automations"]
    if not isinstance(automations, Mapping):
        raise ConfigError("automations must be an object")
    calendar_indicator = automations.get("calendar_busy_indicator")
    if not isinstance(calendar_indicator, Mapping):
        raise ConfigError("automations.calendar_busy_indicator must be an object")
    if str(calendar_indicator.get("mode", "")).strip().lower() != "busybar_matter":
        raise ConfigError("automations.calendar_busy_indicator.mode must be busybar_matter")
    for field in ("enabled", "allow_insecure_http", "require_pairing", "include_all_day", "include_tentative"):
        if not isinstance(calendar_indicator.get(field), bool):
            raise ConfigError(f"automations.calendar_busy_indicator.{field} must be boolean")
    if float(calendar_indicator.get("request_timeout_seconds", 0)) <= 0:
        raise ConfigError("automations.calendar_busy_indicator.request_timeout_seconds must be positive")
    if not 0 <= float(calendar_indicator.get("off_delay_seconds", -1)) <= 300:
        raise ConfigError("automations.calendar_busy_indicator.off_delay_seconds must be between 0 and 300")
    if float(calendar_indicator.get("verify_interval_seconds", 0)) < 10:
        raise ConfigError("automations.calendar_busy_indicator.verify_interval_seconds must be at least 10")
    if float(calendar_indicator.get("retry_seconds", 0)) <= 0:
        raise ConfigError("automations.calendar_busy_indicator.retry_seconds must be positive")
    if float(calendar_indicator.get("retry_max_seconds", 0)) < float(calendar_indicator.get("retry_seconds", 0)):
        raise ConfigError("automations.calendar_busy_indicator.retry_max_seconds must not be below retry_seconds")
    if float(calendar_indicator.get("force_wait_seconds", 0)) <= 0:
        raise ConfigError("automations.calendar_busy_indicator.force_wait_seconds must be positive")
    if float(calendar_indicator.get("lease_min_ttl_seconds", 0)) < 1:
        raise ConfigError("automations.calendar_busy_indicator.lease_min_ttl_seconds must be at least 1")
    if float(calendar_indicator.get("lease_max_ttl_seconds", 0)) < float(
        calendar_indicator.get("lease_min_ttl_seconds", 0)
    ):
        raise ConfigError(
            "automations.calendar_busy_indicator.lease_max_ttl_seconds must not be below "
            "lease_min_ttl_seconds"
        )
    if float(calendar_indicator.get("lease_max_ttl_seconds", 0)) > 86_400:
        raise ConfigError(
            "automations.calendar_busy_indicator.lease_max_ttl_seconds cannot exceed 86400"
        )
    indicator_host = str(calendar_indicator.get("host") or display_host)
    parsed_indicator = urlsplit(indicator_host if "://" in indicator_host else "http://" + indicator_host)
    if parsed_indicator.scheme not in {"http", "https"} or not parsed_indicator.hostname:
        raise ConfigError("automations.calendar_busy_indicator.host must be an HTTP or HTTPS endpoint with a host")
    if parsed_indicator.username or parsed_indicator.password or parsed_indicator.path not in {"", "/"} or parsed_indicator.query or parsed_indicator.fragment:
        raise ConfigError("automations.calendar_busy_indicator.host must not contain credentials, path, query, or fragment")
    indicator_semver = str(calendar_indicator.get("api_semver") or display["api_semver"])
    if not re.fullmatch(r"\d+\.\d+\.\d+", indicator_semver):
        raise ConfigError("automations.calendar_busy_indicator.api_semver must use semantic version form")
    if calendar_indicator.get("enabled") and not config["connectors"]["calendar"].get("enabled"):
        raise ConfigError("calendar_busy_indicator requires the Calendar connector")
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
    action_keys = {
        name: str(interaction[name]).strip().lower()
        for name in (
            "allow_key",
            "deny_key",
            "acknowledge_key",
            "snooze_key",
            "open_key",
        )
    }
    invalid_action_keys = [
        name
        for name, value in action_keys.items()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", value)
    ]
    if invalid_action_keys:
        raise ConfigError(
            f"interaction action keys must use 1 to 32 URL-safe lowercase characters: "
            f"{invalid_action_keys}"
        )
    if action_keys["allow_key"] == action_keys["deny_key"]:
        raise ConfigError("interaction allow and deny keys must differ")
    normal_action_keys = {
        action_keys["acknowledge_key"],
        action_keys["snooze_key"],
        action_keys["open_key"],
    }
    if len(normal_action_keys) != 3:
        raise ConfigError("interaction acknowledge, snooze, and open keys must differ")
    if float(interaction["reconnect_seconds"]) <= 0:
        raise ConfigError("interaction.reconnect_seconds must be positive")
    if int(interaction["action_outbox_max_pending"]) < 1:
        raise ConfigError("interaction.action_outbox_max_pending must be positive")
    if not isinstance(interaction.get("action_consumer_enabled"), bool):
        raise ConfigError("interaction.action_consumer_enabled must be boolean")
    if float(interaction.get("action_consumer_poll_seconds", 0)) <= 0:
        raise ConfigError("interaction.action_consumer_poll_seconds must be positive")
    if int(interaction.get("action_consumer_max_history", 0)) < 1:
        raise ConfigError("interaction.action_consumer_max_history must be positive")
    if not 10 <= float(interaction.get("action_consumer_max_age_seconds", 0)) <= 3600:
        raise ConfigError("interaction.action_consumer_max_age_seconds must be between 10 and 3600")
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
    if "queries" in gmail and not isinstance(gmail["queries"], list):
        raise ConfigError("connectors.gmail.queries must be a list")
    slack = config["connectors"]["slack"]
    for field in ("urgent_keywords", "risk_keywords", "decision_keywords", "promise_keywords"):
        if field in slack and not isinstance(slack[field], list):
            raise ConfigError(f"connectors.slack.{field} must be a list")
    if not isinstance(slack.get("self_user_ids", []), list):
        raise ConfigError("connectors.slack.self_user_ids must be a list")
    for field in ("channel_projects", "channel_relationships", "channel_customers"):
        if not isinstance(slack.get(field, {}), Mapping):
            raise ConfigError(f"connectors.slack.{field} must be an object")
    if not 0 <= int(slack.get("max_threads_per_poll", 10)) <= 50:
        raise ConfigError("connectors.slack.max_threads_per_poll must be between 0 and 50")
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
    if not isinstance(calendar.get("availability_keywords", []), list):
        raise ConfigError("connectors.calendar.availability_keywords must be a list")
    if not isinstance(calendar.get("availability_owner_map", {}), Mapping):
        raise ConfigError("connectors.calendar.availability_owner_map must be an object")
    connectors = config["connectors"]
    notion = connectors["notion"]
    if not isinstance(notion.get("database_ids", []), list) or not all(
        str(value).strip() for value in notion.get("database_ids", [])
    ):
        raise ConfigError("connectors.notion.database_ids must be a list of non-empty ids")
    if not all(re.fullmatch(r"[A-Za-z0-9-]+", str(value)) for value in notion.get("database_ids", [])):
        raise ConfigError("connectors.notion.database_ids contains an invalid id")
    if not isinstance(notion.get("allow_all_shared_pages", False), bool):
        raise ConfigError("connectors.notion.allow_all_shared_pages must be a boolean")
    if notion.get("enabled") and not notion.get("database_ids") and not notion.get("allow_all_shared_pages"):
        raise ConfigError("connectors.notion requires database_ids or allow_all_shared_pages=true")
    drive = connectors["drive"]
    if not isinstance(drive.get("folder_ids", []), list) or not all(
        str(value).strip() for value in drive.get("folder_ids", [])
    ):
        raise ConfigError("connectors.drive.folder_ids must be a list of non-empty ids")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", str(value)) for value in drive.get("folder_ids", [])):
        raise ConfigError("connectors.drive.folder_ids contains an invalid id")
    if not isinstance(drive.get("allow_all_files", False), bool):
        raise ConfigError("connectors.drive.allow_all_files must be a boolean")
    if drive.get("enabled") and not drive.get("folder_ids") and not drive.get("allow_all_files"):
        raise ConfigError("connectors.drive requires folder_ids or allow_all_files=true")
    sheets = connectors["sheets"]
    if not isinstance(sheets.get("spreadsheets", []), list) or not all(
        isinstance(item, Mapping) for item in sheets.get("spreadsheets", [])
    ):
        raise ConfigError("connectors.sheets.spreadsheets must be a list")
    if sheets.get("enabled") and not sheets.get("spreadsheets"):
        raise ConfigError("connectors.sheets.spreadsheets is required when enabled")
    for item in sheets.get("spreadsheets", []):
        if (
            not str(item.get("id", "")).strip()
            or not isinstance(item.get("ranges"), list)
            or not item.get("ranges")
            or not all(isinstance(value, str) and value.strip() for value in item.get("ranges", []))
        ):
            raise ConfigError("each connectors.sheets.spreadsheets entry requires id and ranges")
    github = connectors["github"]
    if not isinstance(github.get("repositories", []), list) or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(value))
        for value in github.get("repositories", [])
    ):
        raise ConfigError("connectors.github.repositories must contain owner/repository names")
    if github.get("enabled") and not github.get("repositories"):
        raise ConfigError("connectors.github.repositories is required when enabled")
    if not isinstance(github.get("deployment_workflows", []), list) or not all(
        isinstance(value, str) and value.strip()
        for value in github.get("deployment_workflows", [])
    ):
        raise ConfigError("connectors.github.deployment_workflows must be a list of exact names")
    if not isinstance(github.get("project_map", {}), Mapping):
        raise ConfigError("connectors.github.project_map must be an object")
    deployment = connectors["deployment"]
    if deployment.get("enabled") and not str(deployment.get("endpoint", "")).strip():
        raise ConfigError("connectors.deployment.endpoint is required when enabled")
    sentry = connectors["sentry"]
    if not isinstance(sentry.get("projects", []), list) or not all(
        isinstance(value, str) and value.strip() for value in sentry.get("projects", [])
    ):
        raise ConfigError("connectors.sentry.projects must be a list of non-empty slugs")
    if sentry.get("enabled") and (not str(sentry.get("organization", "")).strip() or not sentry.get("projects")):
        raise ConfigError("connectors.sentry.organization and projects are required when enabled")
    posthog = connectors["posthog"]
    if not isinstance(posthog.get("checks", []), list) or not all(
        isinstance(value, Mapping) for value in posthog.get("checks", [])
    ):
        raise ConfigError("connectors.posthog.checks must be a list of objects")
    if posthog.get("enabled") and (not str(posthog.get("project_id", "")).strip() or not posthog.get("checks")):
        raise ConfigError("connectors.posthog.project_id and checks are required when enabled")
    shopify = connectors["shopify"]
    if not isinstance(shopify.get("required_scopes", []), list) or not all(
        isinstance(value, str) and value.strip() for value in shopify.get("required_scopes", [])
    ):
        raise ConfigError("connectors.shopify.required_scopes must be a list of non-empty scopes")
    if shopify.get("enabled") and not re.fullmatch(
        r"[a-z0-9][a-z0-9-]*\.myshopify\.com",
        str(shopify.get("shop", "")).strip().lower(),
    ):
        raise ConfigError("connectors.shopify.shop must be a myshopify.com hostname")
    home_assistant = connectors["home_assistant"]
    if not isinstance(home_assistant.get("entities", []), list) or not all(
        isinstance(value, Mapping) and str(value.get("entity_id", "")).strip()
        for value in home_assistant.get("entities", [])
    ):
        raise ConfigError("connectors.home_assistant.entities must contain entity objects")
    if home_assistant.get("enabled") and (
        not str(home_assistant.get("endpoint", "")).strip() or not home_assistant.get("entities")
    ):
        raise ConfigError("connectors.home_assistant.endpoint and entities are required when enabled")
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
        for connector_name, google_connector in (
            ("Gmail", gmail),
            ("Calendar", calendar),
            ("Drive", drive),
            ("Sheets", sheets),
            ("Superhuman", connectors["superhuman"]),
        ):
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
        for connector_name, connector in (
            ("Notion", notion),
            ("GitHub", github),
            ("Sentry", sentry),
            ("PostHog", posthog),
            ("Shopify", shopify),
            ("Stripe", connectors["stripe"]),
            ("Home Assistant", home_assistant),
        ):
            if connector.get("enabled") and connector.get("mode", "api") == "api":
                require_account(connector.get("token_env"), f"{connector_name} token")
        if deployment.get("enabled") and str(deployment.get("token_env", "")).strip():
            require_account(deployment.get("token_env"), "Deployment token")
        if bool(config["llm"]["enabled"]):
            require_account(config["llm"].get("api_key_env"), "LLM API key")
        if bool(interaction["enabled"]) and mode == "signed_http":
            require_account(interaction.get("secret_env"), "signed input secret")
        if not display_is_loopback:
            require_account(display.get("api_token_env"), "BUSY Bar API token")
        indicator_hostname = parsed_indicator.hostname or ""
        try:
            indicator_is_loopback = ipaddress.ip_address(indicator_hostname).is_loopback
        except ValueError:
            indicator_is_loopback = indicator_hostname == "localhost"
        if calendar_indicator.get("enabled") and not indicator_is_loopback:
            require_account(
                calendar_indicator.get("api_token_env") or display.get("api_token_env"),
                "calendar busy indicator BUSY Bar API token",
            )
    if environment == "production":
        demo = config["connectors"].get("demo")
        if isinstance(demo, Mapping) and demo.get("enabled"):
            raise ConfigError("the demo connector cannot be enabled in production")
        enabled_connectors = [
            name
            for name, connector in config["connectors"].items()
            if isinstance(connector, Mapping)
            and connector.get("enabled")
            and name != "demo"
        ]
        if not enabled_connectors:
            raise ConfigError("production requires at least one active connector")
        private_paths = [
            Path(str(config["memory"]["path"])),
            Path(str(interaction["action_outbox_path"])),
            Path(str(closure["ledger_path"])),
            Path(str(closure["snapshot_path"])),
        ]
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
        indicator_hostname = parsed_indicator.hostname or ""
        try:
            indicator_is_loopback = ipaddress.ip_address(indicator_hostname).is_loopback
        except ValueError:
            indicator_is_loopback = indicator_hostname == "localhost"
        indicator_token_env = str(
            calendar_indicator.get("api_token_env") or display.get("api_token_env") or ""
        ).strip()
        if calendar_indicator.get("enabled") and not indicator_is_loopback:
            if not indicator_token_env or not secret_resolver.get(indicator_token_env):
                raise ConfigError("calendar busy indicator requires a BUSY Bar API token")
            allow_insecure_indicator = bool(
                calendar_indicator.get("allow_insecure_http")
                or (not calendar_indicator.get("host") and display.get("allow_insecure_http"))
            )
            if parsed_indicator.scheme != "https" and not allow_insecure_indicator:
                raise ConfigError(
                    "non-loopback calendar busy indicator HTTP requires allow_insecure_http=true"
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
