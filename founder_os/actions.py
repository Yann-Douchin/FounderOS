"""Private, atomic outbox for actions delegated by trusted device input."""

from __future__ import annotations

import json
import ipaddress
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from founder_os.models import Event, utc_now
from founder_os.paths import ensure_private_directory


class ActionOutbox:
    def __init__(self, root: str | Path, *, max_pending: int = 1000) -> None:
        self.root = Path(root).expanduser()
        self.max_pending = max(1, int(max_pending))

    def publish(self, event: Event, action: str, *, now: datetime | None = None) -> Path | None:
        action = action.strip().lower()
        if action != "open" or not _is_safe_open_url(event.url):
            return None
        now = now or utc_now()
        record = {
            "schema_version": 1,
            "action": action,
            "event_id": event.id,
            "source": event.source,
            "url": event.url,
            "created_at": now.isoformat(),
        }
        directory = ensure_private_directory(self.root / "pending")
        if sum(1 for _ in directory.glob("*.json")) >= self.max_pending:
            return None
        filename = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(8)}.json"
        destination = directory / filename
        _atomic_create_json(destination, record)
        return destination


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _is_safe_open_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname == "localhost"
