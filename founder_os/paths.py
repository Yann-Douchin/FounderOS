"""Platform-safe locations for FounderOS runtime state."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def state_root() -> Path:
    """Return a private state root outside the source checkout by default."""
    configured = os.environ.get("FOUNDEROS_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FounderOS"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "FounderOS"
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "founderos"


def agent_state_root() -> Path:
    return state_root() / "agents"


def connector_state_root() -> Path:
    return state_root() / "connectors"


def ensure_private_directory(path: str | Path) -> Path:
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory
