"""Persistent memory used to reduce flicker and repeated interruptions."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

from founder_os.models import Event, parse_datetime, utc_now
from founder_os.paths import ensure_private_directory


class RankingMemory:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        retention_days: float = 30,
        max_entries: int = 5000,
    ) -> None:
        self.path = Path(path).expanduser() if path else None
        self.retention = timedelta(days=max(1.0, float(retention_days)))
        self.max_entries = max(100, int(max_entries))
        self.displayed: dict[str, dict[str, Any]] = {}
        self.acknowledged: dict[str, datetime] = {}
        self.snoozed_until: dict[str, datetime] = {}
        self.current_event_id: str | None = None
        self._lock = RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path or not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(data, dict):
                return
            displayed = data.get("displayed")
            if isinstance(displayed, dict):
                for key, value in displayed.items():
                    if not isinstance(value, dict):
                        continue
                    last_displayed_at = _safe_datetime(value.get("last_displayed_at"))
                    if last_displayed_at is None:
                        continue
                    try:
                        count = max(0, int(value.get("count", 0)))
                    except (TypeError, ValueError):
                        count = 0
                    self.displayed[str(key)] = {
                        "count": count,
                        "last_displayed_at": last_displayed_at.isoformat(),
                        "event_id": str(value.get("event_id") or ""),
                    }
            acknowledged = data.get("acknowledged")
            if isinstance(acknowledged, dict):
                self.acknowledged = {
                    str(key): timestamp
                    for key, value in acknowledged.items()
                    if (timestamp := _safe_datetime(value)) is not None
                }
            elif isinstance(acknowledged, list):
                legacy_time = _safe_datetime(self.path.stat().st_mtime) or utc_now()
                self.acknowledged = {
                    str(key): legacy_time for key in acknowledged if isinstance(key, str) and key
                }
            snoozed: dict[str, datetime] = {}
            raw_snoozed = data.get("snoozed_until")
            if isinstance(raw_snoozed, dict):
                for key, value in raw_snoozed.items():
                    parsed = _safe_datetime(value)
                    if parsed is not None:
                        snoozed[str(key)] = parsed
            self.snoozed_until = snoozed
            current_event_id = data.get("current_event_id")
            self.current_event_id = current_event_id if isinstance(current_event_id, str) else None
            self._prune_unlocked(utc_now())

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        if not self.path:
            return
        self._prune_unlocked(utc_now())
        directory = ensure_private_directory(self.path.parent)
        payload = {
            "schema_version": 1,
            "displayed": self.displayed,
            "acknowledged": {
                key: value.isoformat() for key, value in sorted(self.acknowledged.items())
            },
            "snoozed_until": {key: value.isoformat() for key, value in self.snoozed_until.items()},
            "current_event_id": self.current_event_id,
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=directory)
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            _sync_directory(directory)
        finally:
            temporary_path.unlink(missing_ok=True)

    def is_suppressed(self, event: Event, now: datetime | None = None) -> bool:
        now = now or utc_now()
        with self._lock:
            acknowledged_at = self.acknowledged.get(event.dedupe_key)
            if acknowledged_at is not None and event.occurred_at <= acknowledged_at:
                return True
            snoozed = self.snoozed_until.get(event.dedupe_key)
            return snoozed is not None and snoozed > now

    def score_adjustment(
        self,
        event: Event,
        *,
        now: datetime | None = None,
        repeat_penalty: float = 7,
        repeat_window_minutes: float = 20,
        current_selection_bonus: float = 3,
    ) -> float:
        now = now or utc_now()
        with self._lock:
            adjustment = current_selection_bonus if event.id == self.current_event_id else 0.0
            record = self.displayed.get(event.dedupe_key)
            if not record:
                return adjustment
            try:
                last = parse_datetime(record.get("last_displayed_at"))
            except ValueError:
                last = None
            if not last:
                return adjustment
            age = now - last
            window = timedelta(minutes=repeat_window_minutes)
            if age < window and event.id != self.current_event_id:
                remaining = 1.0 - max(0.0, age.total_seconds()) / window.total_seconds()
                adjustment -= repeat_penalty * remaining
            return adjustment

    def mark_displayed(self, event: Event, now: datetime | None = None) -> None:
        now = now or utc_now()
        with self._lock:
            record = self.displayed.setdefault(event.dedupe_key, {"count": 0})
            record["count"] = int(record.get("count", 0)) + 1
            record["last_displayed_at"] = now.isoformat()
            record["event_id"] = event.id
            self.current_event_id = event.id
            self._save_unlocked()

    def acknowledge(self, event: Event, now: datetime | None = None) -> None:
        now = now or utc_now()
        with self._lock:
            self.acknowledged[event.dedupe_key] = now
            if self.current_event_id == event.id:
                self.current_event_id = None
            self._save_unlocked()

    def snooze(self, event: Event, minutes: float, now: datetime | None = None) -> None:
        now = now or utc_now()
        with self._lock:
            self.snoozed_until[event.dedupe_key] = now + timedelta(minutes=max(1.0, float(minutes)))
            if self.current_event_id == event.id:
                self.current_event_id = None
            self._save_unlocked()

    def clear_current(self) -> None:
        with self._lock:
            if self.current_event_id is None:
                return
            self.current_event_id = None
            self._save_unlocked()

    def _prune_unlocked(self, now: datetime) -> None:
        cutoff = now - self.retention
        displayed_rows: list[tuple[str, dict[str, Any], datetime]] = []
        for key, record in self.displayed.items():
            last_displayed_at = _safe_datetime(record.get("last_displayed_at"))
            if last_displayed_at is None:
                continue
            if last_displayed_at >= cutoff or record.get("event_id") == self.current_event_id:
                displayed_rows.append((key, record, last_displayed_at))
        displayed_rows.sort(key=lambda item: item[2], reverse=True)
        self.displayed = {
            key: record for key, record, _ in displayed_rows[: self.max_entries]
        }
        acknowledged_rows = sorted(
            (
                (key, timestamp)
                for key, timestamp in self.acknowledged.items()
                if timestamp >= cutoff
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        self.acknowledged = dict(acknowledged_rows[: self.max_entries])
        self.snoozed_until = {
            key: timestamp
            for key, timestamp in self.snoozed_until.items()
            if timestamp > now
        }


def _safe_datetime(value: Any) -> datetime | None:
    try:
        return parse_datetime(value)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
