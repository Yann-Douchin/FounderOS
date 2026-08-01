"""Persistent memory used to reduce flicker and repeated interruptions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from founder_os.models import Event, parse_datetime, utc_now


class RankingMemory:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.displayed: dict[str, dict[str, Any]] = {}
        self.acknowledged: set[str] = set()
        self.snoozed_until: dict[str, datetime] = {}
        self.current_event_id: str | None = None
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.displayed = dict(data.get("displayed") or {})
        self.acknowledged = set(data.get("acknowledged") or [])
        self.snoozed_until = {
            key: parsed
            for key, value in (data.get("snoozed_until") or {}).items()
            if (parsed := parse_datetime(value)) is not None
        }
        self.current_event_id = data.get("current_event_id") or None

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "displayed": self.displayed,
            "acknowledged": sorted(self.acknowledged),
            "snoozed_until": {key: value.isoformat() for key, value in self.snoozed_until.items()},
            "current_event_id": self.current_event_id,
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def is_suppressed(self, event: Event, now: datetime | None = None) -> bool:
        now = now or utc_now()
        if event.dedupe_key in self.acknowledged:
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
        adjustment = current_selection_bonus if event.id == self.current_event_id else 0.0
        record = self.displayed.get(event.dedupe_key)
        if not record:
            return adjustment
        last = parse_datetime(record.get("last_displayed_at"))
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
        record = self.displayed.setdefault(event.dedupe_key, {"count": 0})
        record["count"] = int(record.get("count", 0)) + 1
        record["last_displayed_at"] = now.isoformat()
        record["event_id"] = event.id
        self.current_event_id = event.id
        self.save()

    def acknowledge(self, event: Event) -> None:
        self.acknowledged.add(event.dedupe_key)
        if self.current_event_id == event.id:
            self.current_event_id = None
        self.save()

    def snooze(self, event: Event, minutes: float, now: datetime | None = None) -> None:
        now = now or utc_now()
        self.snoozed_until[event.dedupe_key] = now + timedelta(minutes=minutes)
        if self.current_event_id == event.id:
            self.current_event_id = None
        self.save()

    def clear_current(self) -> None:
        self.current_event_id = None
        self.save()
