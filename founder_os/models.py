"""Normalized data contracts shared by connectors, ranking, and display."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from hashlib import sha256
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"invalid datetime: {value!r}") from exc
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid datetime: {value!r}") from exc
    else:
        raise ValueError(f"invalid datetime type: {type(value).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_local_date(
    value: Any,
    timezone_name: str,
    *,
    end_of_day: bool = False,
) -> datetime | None:
    """Parse a date-only API value at a named local-day boundary."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return parse_datetime(value)
    if isinstance(value, date):
        local_date = value
    elif isinstance(value, str):
        text_value = value.strip()
        if "T" in text_value or " " in text_value:
            return parse_datetime(text_value)
        try:
            local_date = date.fromisoformat(text_value)
        except ValueError as exc:
            raise ValueError(f"invalid local date: {value!r}") from exc
    else:
        raise ValueError(f"invalid local date type: {type(value).__name__}")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    boundary = time(23, 59, 59, 999999) if end_of_day else time.min
    return datetime.combine(local_date, boundary, tzinfo=local_timezone).astimezone(UTC)


def _stable_event_id(source: str, dedupe_key: str, title: str) -> str:
    raw = "\x1f".join((source, dedupe_key, title)).encode("utf-8")
    return f"{source}:{sha256(raw).hexdigest()[:16]}"


def _normalized_text(value: Any, *, field_name: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"event {field_name} must be text")
    normalized = unicodedata.normalize("NFC", value)
    text = " ".join(normalized.split())
    if required and not text:
        raise ValueError(f"event {field_name} is required")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class Event:
    """One normalized fact that may deserve the founder's attention."""

    source: str
    title: str
    priority: int = 50
    action_required: bool = False
    kind: str = "information"
    occurred_at: datetime = field(default_factory=utc_now)
    id: str = ""
    dedupe_key: str = ""
    urgency: str = "normal"
    impact: str = "medium"
    due_at: datetime | None = None
    expires_at: datetime | None = None
    body: str = ""
    url: str = ""
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = _normalized_text(self.source, field_name="source", limit=64, required=True).lower()
        title = _normalized_text(self.title, field_name="title", limit=512, required=True)
        body = _normalized_text(self.body, field_name="body", limit=4096)
        kind = _normalized_text(self.kind, field_name="kind", limit=64, required=True).lower()
        urgency = _normalized_text(self.urgency, field_name="urgency", limit=32, required=True).lower()
        impact = _normalized_text(self.impact, field_name="impact", limit=32, required=True).lower()
        url = _normalized_text(self.url, field_name="url", limit=2048)
        if not isinstance(self.action_required, bool):
            raise ValueError("event action_required must be a boolean")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("event metadata must be an object")
        if not 0 <= int(self.priority) <= 100:
            raise ValueError("event priority must be between 0 and 100")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("event confidence must be between 0 and 1")
        occurred_at = parse_datetime(self.occurred_at, default=utc_now())
        due_at = parse_datetime(self.due_at)
        expires_at = parse_datetime(self.expires_at)
        raw_id = _normalized_text(self.id, field_name="id", limit=256)
        dedupe_key = _normalized_text(self.dedupe_key, field_name="dedupe_key", limit=256)
        dedupe_key = dedupe_key or raw_id or title.casefold()
        event_id = raw_id or _stable_event_id(source, dedupe_key, title)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "urgency", urgency)
        object.__setattr__(self, "impact", impact)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "dedupe_key", dedupe_key)
        object.__setattr__(self, "id", event_id)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, source: str | None = None) -> "Event":
        known = {
            "source", "title", "priority", "action_required", "kind", "occurred_at",
            "id", "dedupe_key", "urgency", "impact", "due_at", "expires_at",
            "body", "url", "confidence", "metadata",
        }
        values = {key: payload[key] for key in known if key in payload}
        if source is not None:
            values["source"] = source
        metadata = dict(values.get("metadata") or {})
        metadata.update({key: value for key, value in payload.items() if key not in known})
        values["metadata"] = metadata
        if "source" not in values:
            raise ValueError("event source is required")
        if "title" not in values:
            raise ValueError("event title is required")
        return cls(**values)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "priority": self.priority,
            "title": self.title,
            "body": self.body,
            "action_required": self.action_required,
            "urgency": self.urgency,
            "impact": self.impact,
            "occurred_at": self.occurred_at.isoformat(),
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "dedupe_key": self.dedupe_key,
            "url": self.url,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RankedEvent:
    event: Event
    score: float
    components: Mapping[str, float]

    def explanation(self) -> str:
        visible = sorted(self.components.items(), key=lambda item: (-abs(item[1]), item[0]))
        parts = [f"{name}={value:+.1f}" for name, value in visible if value]
        return f"{self.score:.1f}: " + ", ".join(parts)
