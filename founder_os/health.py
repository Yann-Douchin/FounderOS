"""Private, content-free service heartbeat for local operations."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from founder_os.models import UTC, utc_now
from founder_os.paths import ensure_private_directory


class HealthReporter:
    def __init__(self, path: str | Path, *, heartbeat_seconds: float = 15.0) -> None:
        self.path = Path(path).expanduser()
        ensure_private_directory(self.path.parent)
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self.started_at = utc_now()
        self._last_write_monotonic = 0.0

    def publish(
        self,
        *,
        selected_source: str = "",
        event_count: int,
        connector_health: Mapping[str, Mapping[str, Any]],
        automation_health: Mapping[str, Mapping[str, Any]] | None = None,
        displayed: bool,
        display_error: str = "",
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        monotonic = time.monotonic()
        if not force and monotonic - self._last_write_monotonic < self.heartbeat_seconds:
            return False
        now = (now or utc_now()).astimezone(UTC)
        payload = {
            "schema_version": 1,
            "service": "founderos",
            "status": "running",
            "pid": os.getpid(),
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "generated_at": now.isoformat(),
            "event_count": max(0, int(event_count)),
            "selected_source": str(selected_source),
            "display": {
                "healthy": not bool(display_error),
                "updated": bool(displayed),
                "error_present": bool(display_error),
            },
            "connectors": {
                str(name): {
                    "status": str(state.get("status", "unknown")),
                    "critical": bool(state.get("critical", False)),
                    "failures": max(0, int(state.get("failures", 0))),
                    "last_event_count": max(0, int(state.get("last_event_count", 0))),
                    "last_success_at": state.get("last_success_at"),
                    "error_present": bool(state.get("last_error")),
                }
                for name, state in connector_health.items()
            },
            "automations": {
                str(name): {
                    "status": str(state.get("status", "unknown")),
                    "critical": bool(state.get("critical", False)),
                    "last_success_at": state.get("last_success_at"),
                    "error_present": bool(state.get("last_error")),
                }
                for name, state in (automation_health or {}).items()
            },
        }
        _atomic_private_json(self.path, payload)
        self._last_write_monotonic = monotonic
        return True

    def close(self, *, now: datetime | None = None) -> None:
        timestamp = (now or utc_now()).astimezone(UTC).isoformat()
        _atomic_private_json(
            self.path,
            {
                "schema_version": 1,
                "service": "founderos",
                "status": "stopped",
                "pid": os.getpid(),
                "started_at": self.started_at.astimezone(UTC).isoformat(),
                "generated_at": timestamp,
            },
        )


def _atomic_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    directory = ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
