"""Normalized data contracts shared by connectors, ranking, and display."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=UTC)
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


def _stable_event_id(source: str, dedupe_key: str, title: str) -> str:
    raw = "\x1f".join((source, dedupe_key, title)).encode("utf-8")
    return f"{source}:{sha256(raw).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
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
        source = self.source.strip().lower()
        title = " ".join(unicodedata.normalize("NFC", self.title).split())
        if not source:
            raise ValueError("event source is required")
        if not title:
            raise ValueError("event title is required")
        if not 0 <= int(self.priority) <= 100:
            raise ValueError("event priority must be between 0 and 100")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("event confidence must be between 0 and 1")
        occurred_at = parse_datetime(self.occurred_at, default=utc_now())
        due_at = parse_datetime(self.due_at)
        expires_at = parse_datetime(self.expires_at)
        dedupe_key = self.dedupe_key.strip() or self.id.strip() or title.casefold()
        event_id = self.id.strip() or _stable_event_id(source, dedupe_key, title)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "title", title)
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


@dataclass(frozen=True, slots=True)
class RankedEvent:
    event: Event
    score: float
    components: Mapping[str, float]

    def explanation(self) -> str:
        visible = sorted(self.components.items(), key=lambda item: (-abs(item[1]), item[0]))
        parts = [f"{name}={value:+.1f}" for name, value in visible if value]
        return f"{self.score:.1f}: " + ", ".join(parts)
