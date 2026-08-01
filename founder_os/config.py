"""Configuration loading with environment-variable interpolation."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "runtime": {
        "tick_seconds": 1.0,
        "refresh_seconds": 15.0,
        "event_ttl_minutes": 120,
        "log_level": "INFO",
    },
    "display": {
        "host": "127.0.0.1:8080",
        "application_name": "founderos",
        "device_priority": 90,
        "request_timeout_seconds": 3.0,
        "min_hold_seconds": 6.0,
        "show_idle": True,
        "content_icon": {
            "enabled": True,
            "frame_seconds": 1.0,
        },
    },
    "interaction": {
        "enabled": False,
        "mode": "emulator_sse",
        "event_url": "",
        "allow_key": "ok",
        "deny_key": "back",
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
    "memory": {"path": ".data/founderos-memory.json"},
    "llm": {
        "enabled": False,
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 8,
        "max_calls_per_hour": 6,
    },
    "connectors": {
        "demo": {"enabled": False, "poll_interval_seconds": 2},
        "linear": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 30,
            "token_env": "LINEAR_API_KEY",
            "team_keys": [],
        },
        "slack": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "token_env": "SLACK_BOT_TOKEN",
            "channel_ids": [],
            "mention_markers": [],
        },
        "gmail": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "query": "is:unread newer_than:2d",
            "vip_senders": [],
        },
        "calendar": {
            "enabled": False,
            "mode": "api",
            "poll_interval_seconds": 60,
            "access_token_env": "GOOGLE_ACCESS_TOKEN",
            "calendar_id": "primary",
            "horizon_hours": 8,
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
            "state_dir": ".data/agents",
            "usage": {
                "mode": "snapshot",
                "ttl_seconds": 900,
            },
        },
        "chatgpt_codex": {
            "enabled": False,
            "mode": "agent_bridge",
            "poll_interval_seconds": 2,
            "state_dir": ".data/agents",
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


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


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
        match = _ENV_PATTERN.match(value)
        if match:
            return os.environ.get(match.group(1), "")
    return value


def load_config(path: str | Path | None = None, *, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
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
    _validate(config)
    return config


def _validate(config: Mapping[str, Any]) -> None:
    display = config["display"]
    device_priority = int(display["device_priority"])
    if not 1 <= device_priority <= 100:
        raise ConfigError("display.device_priority must be between 1 and 100")
    if float(display["min_hold_seconds"]) < 0:
        raise ConfigError("display.min_hold_seconds cannot be negative")
    if float(display["content_icon"]["frame_seconds"]) <= 0:
        raise ConfigError("display.content_icon.frame_seconds must be positive")
    interaction = config["interaction"]
    if str(interaction["mode"]) != "emulator_sse":
        raise ConfigError("interaction.mode must be emulator_sse")
    if str(interaction["allow_key"]).strip() == str(interaction["deny_key"]).strip():
        raise ConfigError("interaction allow and deny keys must differ")
    if float(interaction["reconnect_seconds"]) <= 0:
        raise ConfigError("interaction.reconnect_seconds must be positive")
    if float(config["runtime"]["tick_seconds"]) <= 0:
        raise ConfigError("runtime.tick_seconds must be positive")
    if float(config["ranking"]["tie_threshold"]) < 0:
        raise ConfigError("ranking.tie_threshold cannot be negative")


def redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy safe to print in logs."""
    secret_markers = ("token", "secret", "password", "api_key")

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if any(marker in key.lower() for marker in secret_markers) and key.lower() != "token_env":
                    result[key] = "***" if item else item
                else:
                    result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(config)
