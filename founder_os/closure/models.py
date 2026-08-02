"""Versioned data contracts for obligations, operational gates, and evidence."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Iterable, Mapping

from founder_os.models import parse_datetime, utc_now


OBLIGATION_STATES = {"open", "waiting", "blocked", "ready", "deferred", "closed", "cancelled"}
GATE_STATES = {"pending", "blocked", "satisfied", "waived"}
EVIDENCE_STATES = {"present", "failed", "stale"}
ACTIVE_OBLIGATION_STATES = OBLIGATION_STATES - {"closed", "cancelled"}


def stable_obligation_id(key: str) -> str:
    normalized = _text(key, limit=1024, required=True).casefold()
    return "obligation:" + sha256(normalized.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    state: str = "pending"
    owner: str = ""
    detail: str = ""
    required: bool = True
    evidence_ids: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        name = _slug(self.name, "gate name")
        state = _slug(self.state, "gate state")
        if state not in GATE_STATES:
            raise ValueError(f"unsupported gate state: {state}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "owner", _text(self.owner, limit=160))
        object.__setattr__(self, "detail", _text(self.detail, limit=512))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, limit=256))
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default=utc_now()) or utc_now())

    @property
    def complete(self) -> bool:
        return self.state in {"satisfied", "waived"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "owner": self.owner,
            "detail": self.detail,
            "required": self.required,
            "evidence_ids": list(self.evidence_ids),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Gate":
        return cls(
            name=str(value.get("name") or ""),
            state=str(value.get("state") or "pending"),
            owner=str(value.get("owner") or ""),
            detail=str(value.get("detail") or ""),
            required=bool(value.get("required", True)),
            evidence_ids=tuple(value.get("evidence_ids") or ()),
            updated_at=parse_datetime(value.get("updated_at"), default=utc_now()) or utc_now(),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    category: str
    state: str = "present"
    scope: str = ""
    source: str = ""
    source_event_id: str = ""
    owner: str = ""
    detail: str = ""
    observed_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        evidence_id = _text(self.id, limit=256, required=True)
        category = _slug(self.category, "evidence category")
        state = _slug(self.state, "evidence state")
        if state not in EVIDENCE_STATES:
            raise ValueError(f"unsupported evidence state: {state}")
        object.__setattr__(self, "id", evidence_id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "scope", _text(self.scope, limit=160))
        object.__setattr__(self, "source", _slug(self.source, "evidence source", required=False))
        object.__setattr__(self, "source_event_id", _text(self.source_event_id, limit=256))
        object.__setattr__(self, "owner", _text(self.owner, limit=160))
        object.__setattr__(self, "detail", _text(self.detail, limit=512))
        object.__setattr__(self, "observed_at", parse_datetime(self.observed_at, default=utc_now()) or utc_now())
        object.__setattr__(self, "expires_at", parse_datetime(self.expires_at))

    def is_valid(self, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return self.state == "present" and (self.expires_at is None or self.expires_at > current)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "state": self.state,
            "scope": self.scope,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "owner": self.owner,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Evidence":
        return cls(
            id=str(value.get("id") or ""),
            category=str(value.get("category") or ""),
            state=str(value.get("state") or "present"),
            scope=str(value.get("scope") or ""),
            source=str(value.get("source") or ""),
            source_event_id=str(value.get("source_event_id") or ""),
            owner=str(value.get("owner") or ""),
            detail=str(value.get("detail") or ""),
            observed_at=parse_datetime(value.get("observed_at"), default=utc_now()) or utc_now(),
            expires_at=parse_datetime(value.get("expires_at")),
        )


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    key: str
    title: str
    state: str = "open"
    owner: str = "self"
    counterparty: str = ""
    next_actor: str = "self"
    project: str = ""
    relationship_key: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    due_at: datetime | None = None
    resume_after: datetime | None = None
    last_interaction_at: datetime | None = None
    closed_at: datetime | None = None
    priority: int = 50
    confidence: float = 1.0
    followup_count: int = 0
    burst_count: int = 1
    sources: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    entity_keys: tuple[str, ...] = ()
    gates: tuple[Gate, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    url: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        obligation_id = _text(self.id, limit=256) or stable_obligation_id(self.key)
        key = _text(self.key, limit=1024, required=True)
        state = _slug(self.state, "obligation state")
        if state not in OBLIGATION_STATES:
            raise ValueError(f"unsupported obligation state: {state}")
        priority = int(self.priority)
        confidence = float(self.confidence)
        if not 0 <= priority <= 100:
            raise ValueError("obligation priority must be between 0 and 100")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("obligation confidence must be between 0 and 1")
        gate_values = tuple(value if isinstance(value, Gate) else Gate.from_mapping(value) for value in self.gates)
        evidence_values = tuple(
            value if isinstance(value, Evidence) else Evidence.from_mapping(value) for value in self.evidence
        )
        object.__setattr__(self, "id", obligation_id)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "title", _text(self.title, limit=512, required=True))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "owner", _text(self.owner, limit=160) or "self")
        object.__setattr__(self, "counterparty", _text(self.counterparty, limit=160))
        object.__setattr__(self, "next_actor", _text(self.next_actor, limit=160) or "self")
        object.__setattr__(self, "project", _text(self.project, limit=256))
        object.__setattr__(self, "relationship_key", _text(self.relationship_key, limit=256))
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default=utc_now()) or utc_now())
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default=utc_now()) or utc_now())
        object.__setattr__(self, "due_at", parse_datetime(self.due_at))
        object.__setattr__(self, "resume_after", parse_datetime(self.resume_after))
        object.__setattr__(self, "last_interaction_at", parse_datetime(self.last_interaction_at))
        object.__setattr__(self, "closed_at", parse_datetime(self.closed_at))
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "followup_count", max(0, int(self.followup_count)))
        object.__setattr__(self, "burst_count", max(1, int(self.burst_count)))
        object.__setattr__(self, "sources", _unique_text(self.sources, limit=64, slug=True))
        object.__setattr__(self, "source_event_ids", _unique_text(self.source_event_ids, limit=256))
        object.__setattr__(self, "entity_keys", _unique_text(self.entity_keys, limit=256))
        object.__setattr__(self, "gates", _dedupe_gates(gate_values))
        object.__setattr__(self, "evidence", _dedupe_evidence(evidence_values))
        object.__setattr__(self, "url", _text(self.url, limit=2048))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_OBLIGATION_STATES

    @property
    def missing_gates(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.gates if gate.required and not gate.complete)

    @property
    def blocked_gates(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.gates if gate.required and gate.state == "blocked")

    @property
    def operationally_ready(self) -> bool:
        return bool(self.gates) and not self.missing_gates

    def with_state(self, state: str, *, now: datetime | None = None, reason: str = "") -> "Obligation":
        current = now or utc_now()
        metadata = dict(self.metadata)
        if reason:
            metadata["last_transition_reason"] = _text(reason, limit=512)
        return replace(
            self,
            state=state,
            updated_at=current,
            closed_at=current if state in {"closed", "cancelled"} else None,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "state": self.state,
            "owner": self.owner,
            "counterparty": self.counterparty,
            "next_actor": self.next_actor,
            "project": self.project,
            "relationship_key": self.relationship_key,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "resume_after": self.resume_after.isoformat() if self.resume_after else None,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "priority": self.priority,
            "confidence": self.confidence,
            "followup_count": self.followup_count,
            "burst_count": self.burst_count,
            "sources": list(self.sources),
            "source_event_ids": list(self.source_event_ids),
            "entity_keys": list(self.entity_keys),
            "gates": [gate.to_dict() for gate in self.gates],
            "evidence": [item.to_dict() for item in self.evidence],
            "url": self.url,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Obligation":
        return cls(
            id=str(value.get("id") or ""),
            key=str(value.get("key") or value.get("id") or ""),
            title=str(value.get("title") or ""),
            state=str(value.get("state") or "open"),
            owner=str(value.get("owner") or "self"),
            counterparty=str(value.get("counterparty") or ""),
            next_actor=str(value.get("next_actor") or "self"),
            project=str(value.get("project") or ""),
            relationship_key=str(value.get("relationship_key") or ""),
            created_at=parse_datetime(value.get("created_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(value.get("updated_at"), default=utc_now()) or utc_now(),
            due_at=parse_datetime(value.get("due_at")),
            resume_after=parse_datetime(value.get("resume_after")),
            last_interaction_at=parse_datetime(value.get("last_interaction_at")),
            closed_at=parse_datetime(value.get("closed_at")),
            priority=int(value.get("priority", 50)),
            confidence=float(value.get("confidence", 1.0)),
            followup_count=int(value.get("followup_count", 0)),
            burst_count=int(value.get("burst_count", 1)),
            sources=tuple(value.get("sources") or ()),
            source_event_ids=tuple(value.get("source_event_ids") or ()),
            entity_keys=tuple(value.get("entity_keys") or ()),
            gates=tuple(value.get("gates") or ()),
            evidence=tuple(value.get("evidence") or ()),
            url=str(value.get("url") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class Relationship:
    key: str
    name: str
    stage: str = "active"
    last_interaction_at: datetime | None = None
    next_decision: str = ""
    resume_after: datetime | None = None
    cooling_off_until: datetime | None = None
    open_obligation_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, limit=256, required=True).casefold())
        object.__setattr__(self, "name", _text(self.name, limit=160, required=True))
        object.__setattr__(self, "stage", _slug(self.stage, "relationship stage"))
        object.__setattr__(self, "last_interaction_at", parse_datetime(self.last_interaction_at))
        object.__setattr__(self, "next_decision", _text(self.next_decision, limit=512))
        object.__setattr__(self, "resume_after", parse_datetime(self.resume_after))
        object.__setattr__(self, "cooling_off_until", parse_datetime(self.cooling_off_until))
        object.__setattr__(self, "open_obligation_ids", _unique_text(self.open_obligation_ids, limit=256))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "key": self.key,
            "name": self.name,
            "stage": self.stage,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
            "next_decision": self.next_decision,
            "resume_after": self.resume_after.isoformat() if self.resume_after else None,
            "cooling_off_until": self.cooling_off_until.isoformat() if self.cooling_off_until else None,
            "open_obligation_ids": list(self.open_obligation_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Relationship":
        return cls(
            key=str(value.get("key") or ""),
            name=str(value.get("name") or value.get("key") or ""),
            stage=str(value.get("stage") or "active"),
            last_interaction_at=parse_datetime(value.get("last_interaction_at")),
            next_decision=str(value.get("next_decision") or ""),
            resume_after=parse_datetime(value.get("resume_after")),
            cooling_off_until=parse_datetime(value.get("cooling_off_until")),
            open_obligation_ids=tuple(value.get("open_obligation_ids") or ()),
            metadata=dict(value.get("metadata") or {}),
        )


def _text(value: Any, *, limit: int, required: bool = False) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    text = " ".join(normalized.split())
    if required and not text:
        raise ValueError("required text is empty")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _slug(value: Any, context: str, *, required: bool = True) -> str:
    text = _text(value, limit=64, required=required).casefold().replace(" ", "_")
    if text and not all(character.isalnum() or character in {"_", "-"} for character in text):
        raise ValueError(f"{context} contains unsupported characters")
    return text


def _unique_text(values: Iterable[Any], *, limit: int, slug: bool = False) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _slug(value, "value", required=False) if slug else _text(value, limit=limit)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _dedupe_gates(values: Iterable[Gate]) -> tuple[Gate, ...]:
    result: dict[str, Gate] = {}
    for value in values:
        previous = result.get(value.name)
        if previous is None or value.updated_at >= previous.updated_at:
            result[value.name] = value
    return tuple(result[key] for key in sorted(result))


def _dedupe_evidence(values: Iterable[Evidence]) -> tuple[Evidence, ...]:
    result: dict[str, Evidence] = {}
    for value in values:
        previous = result.get(value.id)
        if previous is None or value.observed_at >= previous.observed_at:
            result[value.id] = value
    return tuple(result[key] for key in sorted(result))
