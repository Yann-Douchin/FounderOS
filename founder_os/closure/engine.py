"""Turn noisy source events into persistent, auditable obligations."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from founder_os.closure.entities import EntityGraph, clean, fold
from founder_os.closure.ledger import ObligationLedger
from founder_os.closure.models import Evidence, Gate, Obligation, Relationship, stable_obligation_id
from founder_os.closure.signals import (
    EXPLICIT_GATE_DETAIL,
    availability_signal,
    event_text,
    infer_counterparty,
    infer_due_at,
    infer_evidence,
    infer_gates,
    infer_next_actor,
    infer_owner,
    infer_profile,
    infer_project,
    infer_relationship_key,
    is_feedback,
    is_followup,
    is_obligation_candidate,
)
from founder_os.models import Event, parse_datetime, utc_now


SYSTEM_EVENT_KINDS = {"permission_request", "connector_health", "agent_usage"}
LINEAR_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{1,15}-\d+\b")
GATE_LABELS = {
    "code": "CODE NOT READY",
    "deployment": "DEPLOYMENT NEEDED",
    "access": "ACCESS BLOCKED",
    "evidence": "PROOF MISSING",
    "validation": "VALIDATION NEEDED",
    "ownership": "NO OWNER",
    "decision": "NEED DECISION",
    "delivery": "DELIVERY OPEN",
    "acceptance": "AWAITING ACCEPTANCE",
    "next_move": "ASSIGN NEXT MOVE",
    "capacity": "OWNER OVERLOADED",
    "handoff": "NO BACKUP",
}
GATE_ORDER = {
    name: index
    for index, name in enumerate((
        "access", "code", "deployment", "evidence", "validation", "ownership",
        "decision", "delivery", "acceptance", "next_move", "capacity", "handoff",
    ))
}


class ClosureEngine:
    def __init__(self, config: Mapping[str, Any], ledger: ObligationLedger) -> None:
        self.config = dict(config)
        self.ledger = ledger
        self.enabled = bool(self.config.get("enabled", True))
        self.default_owner = clean(self.config.get("default_owner")) or "self"
        aliases = self.config.get("self_aliases") or ["self", "me", self.default_owner]
        self.self_aliases = {fold(value) for value in aliases if clean(value)} | {"self", "me"}
        self.source_priority_cap = max(1, min(100, int(self.config.get("source_priority_cap", 72))))
        self.burst_window = timedelta(minutes=max(1.0, float(self.config.get("burst_window_minutes", 240))))
        self.burst_threshold = max(2, int(self.config.get("burst_threshold", 4)))
        self.stale_after = timedelta(days=max(1.0, float(self.config.get("stale_after_days", 45))))
        self.evidence_ttl_hours = max(1.0, float(self.config.get("evidence_ttl_hours", 168)))
        self.event_lease = timedelta(seconds=max(30.0, float(self.config.get("event_lease_seconds", 180))))
        self.export_path = Path(str(self.config.get("snapshot_path", ""))).expanduser() if self.config.get("snapshot_path") else None
        self.export_seconds = max(5.0, float(self.config.get("snapshot_interval_seconds", 15)))
        self._last_export_monotonic = 0.0
        self._last_export_digest = ""
        self.timezone = ZoneInfo(str(self.config.get("timezone", "Europe/Madrid")))
        capacity = self.config.get("capacity") or {}
        self.capacity_due_day_threshold = max(2, int(capacity.get("due_day_threshold", 5)))
        self.capacity_require_handoff = bool(capacity.get("require_handoff_when_unavailable", True))
        proof_profiles = self.config.get("proof_profiles") or {}
        self.proof_profiles = proof_profiles if isinstance(proof_profiles, Mapping) else {}

    def close(self) -> None:
        self.ledger.close()

    def reconcile(self, events: Iterable[Event], now: datetime | None = None) -> list[Event]:
        current = now or utc_now()
        source_events = self._route_feedback_to_linear(list(events))
        if not self.enabled:
            return source_events
        graph = EntityGraph(source_events, self.config)
        self._ingest_relationship_interactions(source_events, current)
        availability = [signal for event in source_events if (signal := availability_signal(event))]
        evidence_by_entity = self._evidence_by_entity(source_events, graph, current)
        grouped: dict[str, list[Event]] = defaultdict(list)
        for event in source_events:
            existing = self.ledger.for_event(event.id)
            if existing is None and not is_obligation_candidate(event):
                continue
            # The age limit is an import boundary, never an obligation expiry.
            # Once accepted into the ledger, open work remains governed until
            # source truth or an operator explicitly closes it.
            if existing is None and event.occurred_at + self.stale_after <= current:
                continue
            key = existing.key if existing else self._obligation_key(event, graph)
            grouped[key].append(event)

        touched: dict[str, Obligation] = {}
        for key, observations in grouped.items():
            existing = self.ledger.get_by_key(key)
            obligation = self._merge_obligation(
                key,
                observations,
                existing,
                graph,
                evidence_by_entity,
                current,
            )
            if existing and existing.state in {"closed", "cancelled"} and not self._should_reopen(existing, observations):
                obligation = existing
            else:
                self.ledger.upsert(obligation, reason="source reconciliation")
            for event in observations:
                self.ledger.bind_observation(
                    event.id,
                    obligation.id,
                    source=event.source,
                    fingerprint=_event_fingerprint(event),
                    observed_at=current,
                )
            touched[obligation.id] = obligation

        active = {
            obligation.id: obligation
            for obligation in self.ledger.list(active_only=True)
        }
        active.update({key: value for key, value in touched.items() if value.active})
        governed = self._refresh_temporal_governance(list(active.values()), current)
        governed = self._apply_capacity_and_availability(governed, availability, current)
        governed = self._apply_relationship_memory(governed, current)
        for obligation in governed:
            self.ledger.upsert(obligation, reason="closure governance")
        self._export_if_due(governed, current)
        return [
            self._to_event(obligation, current)
            for obligation in governed
            if obligation.active
            and obligation.state != "deferred"
            and (obligation.resume_after is None or obligation.resume_after <= current)
        ]

    def system_events(self, events: Iterable[Event]) -> list[Event]:
        return [event for event in events if event.kind in SYSTEM_EVENT_KINDS]

    def _route_feedback_to_linear(self, events: Sequence[Event]) -> list[Event]:
        issues: dict[str, Mapping[str, Any]] = {}
        for event in events:
            if event.source != "linear":
                continue
            identifier = clean(event.metadata.get("identifier")).upper()
            if identifier:
                issues[identifier] = {
                    "id": event.id,
                    "identifier": identifier,
                    "owner": clean(event.metadata.get("assignee")),
                    "project": infer_project(event),
                }
            summaries = event.metadata.get("issue_summaries")
            if isinstance(summaries, list):
                for summary in summaries:
                    if not isinstance(summary, Mapping):
                        continue
                    summary_identifier = clean(summary.get("identifier")).upper()
                    if summary_identifier:
                        issues[summary_identifier] = {
                            "id": clean(summary.get("id")),
                            "identifier": summary_identifier,
                            "owner": clean(summary.get("owner")),
                            "project": infer_project(event),
                        }
        if not issues:
            return list(events)
        routed: list[Event] = []
        for event in events:
            if not is_feedback(event):
                routed.append(event)
                continue
            configured = event.metadata.get("linked_issue_ids") or event.metadata.get("linear_issue_ids") or ()
            explicit = (
                [str(value).upper() for value in configured]
                if isinstance(configured, list)
                else []
            )
            references = set(explicit) | set(LINEAR_IDENTIFIER.findall(f"{event.title} {event.body}".upper()))
            matches = [issues[value] for value in sorted(references) if value in issues]
            if not matches:
                routed.append(event)
                continue
            metadata = dict(event.metadata)
            metadata["roadmap_issue_ids"] = [str(value["identifier"]) for value in matches]
            metadata.setdefault("decision_id", f"linear:{matches[0]['identifier']}")
            owner = next(
                (
                    str(value["owner"])
                    for value in matches
                    if clean(value["owner"]).casefold() not in {"", "unassigned", "none"}
                ),
                "",
            )
            if owner and not clean(metadata.get("owner") or metadata.get("assignee")):
                metadata["owner"] = owner
            project = next((str(value["project"]) for value in matches if clean(value["project"])), "")
            if project and not clean(metadata.get("project")):
                metadata["project"] = project
            routed.append(replace(event, metadata=metadata))
        return routed

    def _obligation_key(self, event: Event, graph: EntityGraph) -> str:
        explicit = clean(event.metadata.get("obligation_key"))
        if explicit:
            return "explicit:" + fold(explicit)
        profile = infer_profile(event)
        entity = graph.primary(event)
        if event.kind == "meeting" or event.metadata.get("meeting_id"):
            meeting_id = clean(event.metadata.get("meeting_id") or event.metadata.get("calendar_id") or event.id)
            entity = "meeting:" + fold(meeting_id)
        return f"{entity}|{profile}"

    def _merge_obligation(
        self,
        key: str,
        observations: Sequence[Event],
        existing: Obligation | None,
        graph: EntityGraph,
        evidence_by_entity: Mapping[str, tuple[Evidence, ...]],
        now: datetime,
    ) -> Obligation:
        ordered = sorted(observations, key=lambda event: (-event.priority, -event.occurred_at.timestamp(), event.id))
        lead = ordered[0]
        source_semantic_changed = existing is None or set(event.id for event in observations) != set(existing.source_event_ids)
        if existing is not None and not source_semantic_changed:
            source_semantic_changed = any(
                (record := self.ledger.observation(event.id)) is None
                or record.get("fingerprint") != _event_fingerprint(event)
                for event in observations
            )
        profile = infer_profile(lead)
        entity_keys = tuple(dict.fromkeys(key for event in observations for key in graph.keys_for(event)))
        owner = self._select_owner(observations)
        counterparty = next((infer_counterparty(event) for event in ordered if infer_counterparty(event)), "")
        next_actor = self._select_next_actor(observations, owner)
        project = next((infer_project(event) for event in ordered if infer_project(event)), "")
        relationship_key = next(
            (infer_relationship_key(event) for event in ordered if infer_relationship_key(event)),
            existing.relationship_key if existing else "",
        )
        due_values = [value for event in observations if (value := infer_due_at(event, self.timezone))]
        due_at = min(due_values) if due_values else existing.due_at if existing else None
        if existing and not source_semantic_changed:
            due_at = existing.due_at
        manual_fields = set(
            (existing.metadata.get("manual_correction") or {}).get("fields", [])
            if existing and isinstance(existing.metadata.get("manual_correction"), Mapping)
            else []
        )
        if existing:
            owner = existing.owner if "owner" in manual_fields else owner
            counterparty = existing.counterparty if "counterparty" in manual_fields else counterparty
            next_actor = existing.next_actor if "next_actor" in manual_fields else next_actor
            project = existing.project if "project" in manual_fields else project
            relationship_key = existing.relationship_key if "relationship_key" in manual_fields else relationship_key
            due_at = existing.due_at if "due_at" in manual_fields else due_at
        created_at = existing.created_at if existing else min(event.occurred_at for event in observations)
        updated_at = max(event.occurred_at for event in observations)
        if existing and existing.updated_at > updated_at:
            updated_at = existing.updated_at
        recent_cutoff = now - self.burst_window
        burst_count = sum(1 for event in observations if event.occurred_at >= recent_cutoff)
        followup_count = max(
            existing.followup_count if existing else 0,
            sum(1 for event in observations if is_followup(event)),
        )
        gates = self._merge_gates(
            existing.gates if existing else (),
            tuple(
                gate
                for event in sorted(
                    observations,
                    key=lambda value: (value.occurred_at, value.priority, value.id),
                )
                for gate in infer_gates(event, profile, event.occurred_at)
            ),
        )
        current_event_ids = {event.id for event in observations}
        retained_evidence = tuple(
            item for item in (existing.evidence if existing else ())
            if not item.source_event_id or item.source_event_id not in current_event_ids
        )
        evidence = self._merge_evidence(
            retained_evidence,
            tuple(item for event in observations for item in self._event_evidence(event, graph, evidence_by_entity, now)),
            now,
        )
        gates = self._apply_evidence_quorum(gates, evidence, profile, now)
        gates = self._apply_manual_gates(gates, existing, now)
        state = self._state_for(gates, next_actor)
        automatically_closed = profile == "feedback" and bool(gates) and all(
            gate.complete for gate in gates if gate.required
        )
        if automatically_closed:
            state = "closed"
        elif existing and existing.state in {"closed", "cancelled"}:
            state = "open"
        priority = self._priority(observations, gates, state, due_at, followup_count, burst_count, now)
        title = self._title_for(lead, key, graph, gates, state, next_actor, evidence, profile, now)
        if existing:
            priority = existing.priority if "priority" in manual_fields else priority
            title = existing.title if "title" in manual_fields else title
        metadata = dict(existing.metadata) if existing else {}
        metadata.update({
            "profile": profile,
            "source_priority_max": max(event.priority for event in observations),
            "source_priority_normalized": min(self.source_priority_cap, max(event.priority for event in observations)),
            "priority_diluted": burst_count >= self.burst_threshold,
            "burst_window_minutes": int(self.burst_window.total_seconds() / 60),
            "source_event_count": len(observations),
            "feedback": any(is_feedback(event) for event in observations),
            "roadmap_issue_ids": sorted({
                str(value)
                for event in observations
                for value in (
                    event.metadata.get("roadmap_issue_ids", [])
                    if isinstance(event.metadata.get("roadmap_issue_ids", []), list)
                    else []
                )
                if str(value)
            }),
            "decision_id": next(
                (
                    clean(event.metadata.get("decision_id"))
                    for event in ordered
                    if clean(event.metadata.get("decision_id"))
                ),
                "",
            ),
            "manual_correction": metadata.get("manual_correction"),
            "base_priority": priority,
            "base_title": title,
            "meeting_phase": clean(lead.metadata.get("meeting_phase")) if profile == "meeting" else "",
            "meeting_id": clean(lead.metadata.get("meeting_id")) if profile == "meeting" else "",
        })
        candidate = Obligation(
            id=existing.id if existing else stable_obligation_id(key),
            key=key,
            title=title,
            state=state,
            owner=owner,
            counterparty=counterparty,
            next_actor=next_actor,
            project=project,
            relationship_key=relationship_key,
            created_at=created_at,
            updated_at=updated_at,
            due_at=due_at,
            resume_after=existing.resume_after if existing else None,
            last_interaction_at=(
                existing.last_interaction_at
                if existing and not source_semantic_changed
                else max(event.occurred_at for event in observations)
            ),
            closed_at=now if automatically_closed else None,
            priority=priority,
            confidence=min(event.confidence for event in observations),
            followup_count=followup_count,
            burst_count=max(1, burst_count),
            sources=tuple(event.source for event in observations),
            source_event_ids=tuple(event.id for event in observations),
            entity_keys=entity_keys,
            gates=gates,
            evidence=evidence,
            url=next((event.url for event in ordered if event.url), existing.url if existing else ""),
            metadata=metadata,
        )
        if existing is not None:
            comparison = replace(candidate, updated_at=existing.updated_at)
            if comparison == existing:
                return comparison
            return replace(candidate, updated_at=max(now, existing.updated_at))
        return candidate

    def _event_evidence(
        self,
        event: Event,
        graph: EntityGraph,
        evidence_by_entity: Mapping[str, tuple[Evidence, ...]],
        now: datetime,
    ) -> tuple[Evidence, ...]:
        direct = list(infer_evidence(event, now, self.evidence_ttl_hours))
        for entity in graph.keys_for(event):
            direct.extend(evidence_by_entity.get(entity, ()))
        return tuple({item.id: item for item in direct}.values())

    def _evidence_by_entity(
        self,
        events: Sequence[Event],
        graph: EntityGraph,
        now: datetime,
    ) -> Mapping[str, tuple[Evidence, ...]]:
        result: dict[str, dict[str, Evidence]] = defaultdict(dict)
        for event in events:
            evidence = infer_evidence(event, now, self.evidence_ttl_hours)
            if not evidence:
                continue
            for key in graph.keys_for(event):
                for item in evidence:
                    result[key][item.id] = item
        return {key: tuple(values.values()) for key, values in result.items()}

    def _select_owner(self, events: Sequence[Event]) -> str:
        values = [infer_owner(event, self.default_owner) for event in events]
        non_default = [value for value in values if fold(value) not in self.self_aliases]
        return non_default[0] if non_default else values[0] if values else self.default_owner

    def _select_next_actor(self, events: Sequence[Event], owner: str) -> str:
        values = [infer_next_actor(event, owner, self.default_owner) for event in events]
        external = [value for value in values if fold(value) not in self.self_aliases]
        return external[0] if external else values[0] if values else owner

    @staticmethod
    def _merge_gates(existing: Sequence[Gate], inferred: Sequence[Gate]) -> tuple[Gate, ...]:
        grouped: dict[str, list[Gate]] = defaultdict(list)
        for gate in (*existing, *inferred):
            grouped[gate.name].append(gate)
        result: list[Gate] = []
        for name, values in grouped.items():
            # Current source truth wins. Inferred gates are appended after
            # persisted gates, so an equally timestamped regression is not
            # hidden behind a formerly satisfied state.
            _, newest = max(enumerate(values), key=lambda item: (item[1].updated_at, item[0]))
            candidate = replace(
                newest,
                evidence_ids=tuple(dict.fromkeys(item for gate in values for item in gate.evidence_ids)),
            )
            previous = max(
                (gate for gate in existing if gate.name == name),
                key=lambda gate: gate.updated_at,
                default=None,
            )
            if previous is not None and _same_gate_content(previous, candidate):
                candidate = replace(candidate, updated_at=previous.updated_at)
            result.append(candidate)
        return tuple(sorted(result, key=lambda gate: gate.name))

    @staticmethod
    def _apply_manual_gates(
        gates: Sequence[Gate],
        existing: Obligation | None,
        now: datetime,
    ) -> tuple[Gate, ...]:
        if existing is None:
            return tuple(gates)
        raw = existing.metadata.get("manual_gates")
        if not isinstance(raw, Mapping):
            return tuple(gates)
        values = {gate.name: gate for gate in gates}
        for name, override in raw.items():
            if not isinstance(override, Mapping):
                continue
            previous = values.get(str(name))
            candidate = Gate(
                name=str(name),
                state=str(override.get("state") or (previous.state if previous else "pending")),
                owner=str(override.get("owner") or (previous.owner if previous else "")),
                detail=str(override.get("detail") or (previous.detail if previous else "")),
                required=bool(override.get("required", previous.required if previous else True)),
                evidence_ids=previous.evidence_ids if previous else (),
                updated_at=parse_datetime(override.get("at"), default=previous.updated_at if previous else now)
                or (previous.updated_at if previous else now),
            )
            if previous is not None and _same_gate_content(previous, candidate):
                candidate = replace(candidate, updated_at=previous.updated_at)
            values[str(name)] = candidate
        return tuple(values[key] for key in sorted(values))

    @staticmethod
    def _merge_evidence(existing: Sequence[Evidence], inferred: Sequence[Evidence], now: datetime) -> tuple[Evidence, ...]:
        values: dict[str, Evidence] = {}
        for item in (*existing, *inferred):
            if item.expires_at is not None and item.expires_at <= now:
                if item.id in values:
                    del values[item.id]
                continue
            previous = values.get(item.id)
            if previous is None or item.observed_at >= previous.observed_at:
                values[item.id] = item
        return tuple(values[key] for key in sorted(values))

    def _apply_evidence_quorum(
        self,
        gates: Sequence[Gate],
        evidence: Sequence[Evidence],
        profile: str,
        now: datetime,
    ) -> tuple[Gate, ...]:
        valid_categories = {item.category for item in evidence if item.is_valid(now)}
        valid_scopes: dict[str, set[str]] = defaultdict(set)
        for item in evidence:
            if item.is_valid(now):
                valid_scopes[item.category].add(fold(item.scope))
        profile_config = self.proof_profiles.get(profile) or {}
        required = tuple(str(value).strip().casefold() for value in profile_config.get("required_categories", ()) if str(value).strip())
        raw_required_scopes = profile_config.get("required_scopes") or {}
        required_scopes = {
            str(category).strip().casefold(): {fold(scope) for scope in scopes if clean(scope)}
            for category, scopes in raw_required_scopes.items()
            if isinstance(scopes, (list, tuple))
        } if isinstance(raw_required_scopes, Mapping) else {}
        minimum = max(0, int(profile_config.get("minimum_categories", len(required))))
        missing: list[str] = []
        complete_categories: set[str] = set()
        for category in required:
            if category not in valid_categories:
                missing.append(category)
                continue
            absent_scopes = required_scopes.get(category, set()) - valid_scopes.get(category, set())
            if absent_scopes:
                missing.extend(f"{category}:{scope}" for scope in sorted(absent_scopes))
                continue
            complete_categories.add(category)
        quorum = len(complete_categories) >= minimum
        result: list[Gate] = []
        for gate in gates:
            if gate.detail == EXPLICIT_GATE_DETAIL:
                result.append(gate)
                continue
            state = gate.state
            detail = gate.detail
            evidence_ids = gate.evidence_ids
            if gate.name == "evidence":
                relevant: dict[tuple[str, str], Evidence] = {}
                for item in evidence:
                    if not item.is_valid(now) or (required and item.category not in required):
                        continue
                    evidence_key = (item.category, fold(item.scope))
                    previous = relevant.get(evidence_key)
                    if previous is None or item.observed_at >= previous.observed_at:
                        relevant[evidence_key] = item
                if quorum:
                    state = "satisfied"
                    detail = (
                        f"Evidence quorum complete: {len(complete_categories)}/{len(required)} "
                        "configured categories"
                    )
                else:
                    state = "blocked" if valid_categories else "pending"
                    detail = "Missing evidence: " + ", ".join(missing or required or ("configured proof",))
                evidence_ids = tuple(relevant[key].id for key in sorted(relevant))
            elif gate.name == "deployment" and "deployment" in valid_categories:
                state, detail = "satisfied", "Deployment evidence is present"
            elif gate.name == "access" and "access" in valid_categories:
                state, detail = "satisfied", "Access evidence is present"
            elif gate.name == "validation" and "validation" in valid_categories:
                state, detail = "satisfied", "Validation evidence is present"
            candidate = replace(gate, state=state, detail=detail, evidence_ids=evidence_ids)
            if not _same_gate_content(gate, candidate):
                candidate = replace(candidate, updated_at=now)
            result.append(candidate)
        return tuple(result)

    def _refresh_temporal_governance(
        self,
        obligations: Sequence[Obligation],
        now: datetime,
    ) -> list[Obligation]:
        result: list[Obligation] = []
        for obligation in obligations:
            evidence = tuple(item for item in obligation.evidence if item.is_valid(now))
            profile = clean(obligation.metadata.get("profile")) or "commitment"
            gates = self._apply_evidence_quorum(obligation.gates, evidence, profile, now)
            gates = self._apply_manual_gates(gates, obligation, now)
            state = self._state_for(gates, obligation.next_actor)
            if evidence == obligation.evidence and gates == obligation.gates and state == obligation.state:
                result.append(obligation)
                continue
            metadata = dict(obligation.metadata)
            candidate = replace(
                obligation,
                evidence=evidence,
                gates=gates,
                state=state,
                title=self._title_from_obligation(obligation, gates, state, now),
                updated_at=now,
            )
            metadata["base_title"] = candidate.title
            result.append(replace(candidate, metadata=metadata))
        return result

    def _apply_capacity_and_availability(
        self,
        obligations: Sequence[Obligation],
        availability: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> list[Obligation]:
        due_groups: dict[tuple[str, str], list[Obligation]] = defaultdict(list)
        for obligation in obligations:
            if obligation.due_at:
                day = obligation.due_at.astimezone(self.timezone).date().isoformat()
                due_groups[(fold(obligation.owner), day)].append(obligation)
        result: list[Obligation] = []
        for obligation in obligations:
            previous_governance = {
                gate.name: gate for gate in obligation.gates if gate.name in {"capacity", "handoff"}
            }
            gates = [gate for gate in obligation.gates if gate.name not in {"capacity", "handoff"}]
            capacity_details: list[str] = []
            concentration_details: list[str] = []
            if obligation.due_at:
                day = obligation.due_at.astimezone(self.timezone).date().isoformat()
                concentrated = due_groups.get((fold(obligation.owner), day), [])
                if len(concentrated) >= self.capacity_due_day_threshold:
                    concentration_details.append(f"{len(concentrated)} obligations are due for {obligation.owner} on {day}")
            unavailable = next(
                (
                    signal for signal in availability
                    if self._same_actor(obligation.owner, str(signal["owner"]))
                    and (obligation.due_at or now) >= signal["start"]
                    and (obligation.due_at or now) <= signal["end"]
                ),
                None,
            )
            if unavailable:
                capacity_details.append(f"{obligation.owner} is unavailable: {unavailable['reason']}")
            capacity_details = [*concentration_details, *capacity_details]
            delegate = clean(obligation.metadata.get("delegate"))
            blocking_capacity = bool(concentration_details or (unavailable and not delegate))
            if blocking_capacity:
                gates.append(_stable_gate(previous_governance.get("capacity"), Gate(
                    name="capacity",
                    state="blocked",
                    owner=obligation.owner,
                    detail="; ".join(capacity_details),
                    updated_at=now,
                )))
                if unavailable and self.capacity_require_handoff and not delegate:
                    gates.append(_stable_gate(previous_governance.get("handoff"), Gate(
                        name="handoff",
                        state="blocked",
                        owner=obligation.owner,
                        detail="No delegate is recorded for the unavailable owner",
                        updated_at=now,
                    )))
            if unavailable and delegate:
                gates.append(_stable_gate(previous_governance.get("handoff"), Gate(
                    name="handoff",
                    state="satisfied",
                    owner=delegate,
                    detail=f"Delegated to {delegate}",
                    updated_at=now,
                )))
            next_actor = delegate if unavailable and delegate else obligation.next_actor
            state = self._state_for(gates, next_actor)
            metadata = dict(obligation.metadata)
            base_priority = max(0, min(100, int(metadata.get("base_priority", obligation.priority))))
            base_title = clean(metadata.get("base_title")) or obligation.title
            metadata["base_priority"] = base_priority
            metadata["base_title"] = base_title
            priority = min(100, base_priority + (12 if blocking_capacity else 0))
            title = base_title
            if blocking_capacity:
                entity = obligation.project or obligation.counterparty or obligation.title.split(" | ", 1)[0]
                title = f"{entity} | {'NO BACKUP' if unavailable else 'OWNER OVERLOADED'}"
            candidate = replace(
                obligation,
                gates=tuple(gates),
                state=state,
                priority=priority,
                title=title,
                next_actor=next_actor,
                metadata=metadata,
            )
            if candidate != obligation:
                candidate = replace(candidate, updated_at=now)
            result.append(candidate)
        return result

    def _apply_relationship_memory(self, obligations: Sequence[Obligation], now: datetime) -> list[Obligation]:
        grouped: dict[str, list[Obligation]] = defaultdict(list)
        for obligation in obligations:
            if obligation.relationship_key:
                grouped[obligation.relationship_key].append(obligation)
        transformed: dict[str, Obligation] = {}
        for relationship_key, peers in grouped.items():
            existing = self.ledger.relationship(relationship_key)
            manual_fields = {
                str(value) for value in existing.metadata.get("manual_fields", [])
            } if existing and isinstance(existing.metadata.get("manual_fields", []), list) else set()
            cooling = existing.cooling_off_until if existing else None
            resume = existing.resume_after if existing else None
            updated_peers: list[Obligation] = []
            for obligation in peers:
                state = obligation.state
                if cooling and cooling > now and not obligation.blocked_gates and not (obligation.due_at and obligation.due_at <= now):
                    state = "deferred"
                candidate = replace(obligation, state=state, resume_after=resume or obligation.resume_after)
                candidate = self._meeting_context(candidate, peers, existing, now)
                if candidate != obligation:
                    candidate = replace(candidate, updated_at=now)
                transformed[obligation.id] = candidate
                updated_peers.append(candidate)
            next_decision = (
                existing.next_decision
                if existing and "next_decision" in manual_fields
                else next(
                    (
                        peer.title
                        for peer in updated_peers
                        if peer.metadata.get("profile") != "meeting"
                        and any(gate.name == "decision" and not gate.complete for gate in peer.gates)
                    ),
                    "",
                )
            )
            relationship_times = [
                peer.last_interaction_at for peer in updated_peers if peer.last_interaction_at
            ]
            if existing and existing.last_interaction_at:
                relationship_times.append(existing.last_interaction_at)
            relationship = Relationship(
                key=relationship_key,
                name=(
                    existing.name
                    if existing and "name" in manual_fields
                    else next(
                        (peer.counterparty for peer in updated_peers if peer.counterparty),
                        existing.name if existing else relationship_key,
                    )
                ),
                stage=(
                    existing.stage
                    if existing and "stage" in manual_fields
                    else next(
                        (
                            clean(peer.metadata.get("relationship_stage"))
                            for peer in updated_peers
                            if clean(peer.metadata.get("relationship_stage"))
                        ),
                        existing.stage if existing else "active",
                    )
                ),
                last_interaction_at=max(relationship_times, default=None),
                next_decision=next_decision,
                resume_after=resume,
                cooling_off_until=cooling,
                open_obligation_ids=tuple(peer.id for peer in updated_peers if peer.active),
                metadata=dict(existing.metadata) if existing else {},
            )
            self.ledger.upsert_relationship(relationship)
        for relationship in self.ledger.relationships():
            manual_fields = {
                str(value) for value in relationship.metadata.get("manual_fields", [])
            } if isinstance(relationship.metadata.get("manual_fields", []), list) else set()
            inferred_decision = bool(
                relationship.next_decision and "next_decision" not in manual_fields
            )
            if relationship.key not in grouped and (relationship.open_obligation_ids or inferred_decision):
                self.ledger.upsert_relationship(
                    replace(
                        relationship,
                        open_obligation_ids=(),
                        next_decision=(
                            relationship.next_decision
                            if "next_decision" in manual_fields
                            else ""
                        ),
                    ),
                    reason="closed obligation relationship cleanup",
                )
        return [transformed.get(obligation.id, obligation) for obligation in obligations]

    def _ingest_relationship_interactions(self, events: Sequence[Event], now: datetime) -> None:
        grouped: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            key = infer_relationship_key(event)
            if key:
                grouped[key].append(event)
        for key, interactions in grouped.items():
            latest = max(interactions, key=lambda event: (event.occurred_at, event.id))
            existing = self.ledger.relationship(key)
            metadata = dict(existing.metadata) if existing else {}
            manual_fields = {
                str(value) for value in metadata.get("manual_fields", [])
            } if isinstance(metadata.get("manual_fields", []), list) else set()
            previous_sources = metadata.get("sources")
            if not isinstance(previous_sources, list):
                previous_sources = []
            metadata["sources"] = sorted({
                *(str(value) for value in previous_sources),
                *(event.source for event in interactions),
            })
            metadata["last_event_id"] = latest.id
            interaction_times = [event.occurred_at for event in interactions]
            if existing and existing.last_interaction_at:
                interaction_times.append(existing.last_interaction_at)
            relationship = Relationship(
                key=key,
                name=(
                    existing.name
                    if existing and "name" in manual_fields
                    else next(
                        (
                            value
                            for event in sorted(
                                interactions,
                                key=lambda item: (item.occurred_at, item.id),
                                reverse=True,
                            )
                            if (value := infer_counterparty(event))
                        ),
                        existing.name if existing else key,
                    )
                ),
                stage=existing.stage if existing else "active",
                last_interaction_at=max(interaction_times, default=now),
                next_decision=existing.next_decision if existing else "",
                resume_after=existing.resume_after if existing else None,
                cooling_off_until=existing.cooling_off_until if existing else None,
                open_obligation_ids=existing.open_obligation_ids if existing else (),
                metadata=metadata,
            )
            self.ledger.upsert_relationship(relationship, reason="source relationship interaction")

    def _meeting_context(
        self,
        obligation: Obligation,
        peers: Sequence[Obligation],
        relationship: Relationship | None,
        now: datetime,
    ) -> Obligation:
        if obligation.metadata.get("profile") != "meeting" or obligation.metadata.get("meeting_phase") != "before":
            return obligation
        candidates = [
            peer
            for peer in peers
            if peer.id != obligation.id and peer.active and peer.metadata.get("profile") != "meeting"
        ]
        metadata = dict(obligation.metadata)
        if not candidates:
            metadata.pop("meeting_context_obligation_id", None)
            metadata.pop("meeting_context_title", None)
            return replace(
                obligation,
                title=clean(metadata.get("base_title")) or obligation.title,
                priority=int(metadata.get("base_priority", obligation.priority)),
                metadata=metadata,
            )
        context = max(
            candidates,
            key=lambda peer: (
                bool(peer.due_at and peer.due_at <= now),
                bool(peer.blocked_gates),
                peer.priority,
                peer.updated_at,
            ),
        )
        missing = sorted(
            context.missing_gates,
            key=lambda gate: (GATE_ORDER.get(gate.name, 999), gate.name),
        )
        if context.due_at and context.due_at <= now:
            suffix = GATE_LABELS.get(missing[0].name, "COMMITMENT") if missing else "COMMITMENT"
            label = "OVERDUE " + suffix
        elif missing:
            label = GATE_LABELS.get(missing[0].name, "COMMITMENT OPEN")
        elif context.state == "waiting":
            label = f"WAITING ON {context.next_actor}"
        else:
            label = "COMMITMENT OPEN"
        entity = (
            obligation.counterparty
            or next((peer.counterparty for peer in candidates if peer.counterparty), "")
            or (relationship.name if relationship else "")
            or obligation.title.split(" | ", 1)[0]
        )
        metadata["meeting_context_obligation_id"] = context.id
        metadata["meeting_context_title"] = context.title
        return replace(
            obligation,
            title=f"{entity} | {label}",
            priority=max(obligation.priority, min(100, context.priority + 6)),
            metadata=metadata,
        )

    def _priority(
        self,
        events: Sequence[Event],
        gates: Sequence[Gate],
        state: str,
        due_at: datetime | None,
        followups: int,
        burst_count: int,
        now: datetime,
    ) -> int:
        score = min(self.source_priority_cap, max(event.priority for event in events))
        if state == "blocked":
            score += 18
        elif state == "waiting":
            score += 8
        elif state == "ready":
            score += 10
        if due_at:
            seconds = (due_at - now).total_seconds()
            score += 18 if seconds <= 0 else 12 if seconds <= 3600 else 6 if seconds <= 86400 else 0
        if any(is_feedback(event) for event in events):
            score += 6
        if any(infer_profile(event) == "meeting" for event in events):
            score += 10
        score += min(9, followups * 3)
        if any(gate.name == "evidence" and gate.state == "blocked" for gate in gates):
            score += 7
        if burst_count >= self.burst_threshold:
            score = min(score, self.source_priority_cap + 25)
        return max(0, min(100, int(score)))

    def _title_for(
        self,
        lead: Event,
        key: str,
        graph: EntityGraph,
        gates: Sequence[Gate],
        state: str,
        next_actor: str,
        evidence: Sequence[Evidence],
        profile: str,
        now: datetime,
    ) -> str:
        entity_key = key.split("|", 1)[0]
        entity = infer_project(lead) or infer_counterparty(lead) or graph.display_name(entity_key, lead.title)
        missing = sorted(
            (gate for gate in gates if gate.required and not gate.complete),
            key=lambda gate: (GATE_ORDER.get(gate.name, 999), gate.name),
        )
        blocked = [gate for gate in missing if gate.state == "blocked"]
        if profile == "meeting" and any(gate.name == "next_move" for gate in missing) and clean(lead.metadata.get("meeting_phase")).casefold() == "after":
            label = "ASSIGN NEXT MOVE"
        elif profile == "meeting" and any(gate.name == "decision" for gate in missing):
            label = "NEED DECISION"
        elif profile == "meeting" and not missing:
            label = "READY FOR FINAL CLOSE"
        elif profile == "meeting":
            phase = clean(lead.metadata.get("meeting_phase")).casefold()
            label = "ASSIGN NEXT MOVE" if phase == "after" else "NEED DECISION"
        elif blocked:
            gate = blocked[0]
            if gate.name == "evidence":
                detail = gate.detail.removeprefix("Missing evidence: ")
                count = len([value for value in detail.split(",") if value.strip()])
                label = f"{max(1, count)} PROOFS MISSING"
            else:
                label = GATE_LABELS.get(gate.name, "BLOCKED")
        elif missing:
            label = GATE_LABELS.get(missing[0].name, "NEXT STEP")
        elif state == "waiting":
            label = f"WAITING ON {next_actor}"
        elif state == "ready":
            label = "READY FOR FINAL CLOSE"
        else:
            label = "NEXT MOVE"
        if lead.due_at and lead.due_at <= now:
            label = "OVERDUE " + label
        return f"{entity} | {label}"

    def _title_from_obligation(
        self,
        obligation: Obligation,
        gates: Sequence[Gate],
        state: str,
        now: datetime,
    ) -> str:
        entity = obligation.project or obligation.counterparty or obligation.title.split(" | ", 1)[0]
        missing = sorted(
            (gate for gate in gates if gate.required and not gate.complete),
            key=lambda gate: (GATE_ORDER.get(gate.name, 999), gate.name),
        )
        blocked = [gate for gate in missing if gate.state == "blocked"]
        profile = clean(obligation.metadata.get("profile")).casefold()
        if profile == "meeting" and any(gate.name == "next_move" for gate in missing):
            label = "ASSIGN NEXT MOVE"
        elif profile == "meeting" and any(gate.name == "decision" for gate in missing):
            label = "NEED DECISION"
        elif blocked:
            gate = blocked[0]
            if gate.name == "evidence":
                detail = gate.detail.removeprefix("Missing evidence: ")
                count = len([value for value in detail.split(",") if value.strip()])
                label = f"{max(1, count)} PROOFS MISSING"
            else:
                label = GATE_LABELS.get(gate.name, "BLOCKED")
        elif missing:
            label = GATE_LABELS.get(missing[0].name, "NEXT STEP")
        elif state == "waiting":
            label = f"WAITING ON {obligation.next_actor}"
        elif state == "ready":
            label = "READY FOR FINAL CLOSE"
        else:
            label = "NEXT MOVE"
        if obligation.due_at and obligation.due_at <= now:
            label = "OVERDUE " + label
        return f"{entity} | {label}"

    def _to_event(self, obligation: Obligation, now: datetime) -> Event:
        missing = [gate.to_dict() for gate in obligation.missing_gates]
        return Event(
            id=f"closure:{obligation.id}",
            source="closure",
            title=obligation.title,
            body=self._body_for(obligation),
            priority=obligation.priority,
            action_required=True,
            kind="blocker" if obligation.state == "blocked" else "waiting" if obligation.state == "waiting" else "meeting" if obligation.metadata.get("profile") == "meeting" else "obligation",
            urgency="critical" if obligation.state == "blocked" or (obligation.due_at and obligation.due_at <= now) else "high" if obligation.state in {"waiting", "ready"} else "normal",
            impact="high" if obligation.counterparty or obligation.project else "medium",
            occurred_at=obligation.updated_at,
            due_at=obligation.due_at,
            expires_at=now + self.event_lease,
            dedupe_key=obligation.id,
            url=obligation.url,
            confidence=obligation.confidence,
            metadata={
                "obligation_id": obligation.id,
                "obligation_state": obligation.state,
                "owner": obligation.owner,
                "counterparty": obligation.counterparty,
                "next_actor": obligation.next_actor,
                "project": obligation.project,
                "relationship_key": obligation.relationship_key,
                "followup_count": obligation.followup_count,
                "burst_count": obligation.burst_count,
                "priority_diluted": bool(obligation.metadata.get("priority_diluted")),
                "missing_gates": missing,
                "evidence_count": len([item for item in obligation.evidence if item.is_valid(now)]),
                "source_event_ids": list(obligation.source_event_ids),
                "profile": obligation.metadata.get("profile"),
                "visual_state": self._visual_state(obligation),
                "status_label": self._status_label(obligation),
            },
        )

    @staticmethod
    def _visual_state(obligation: Obligation) -> str:
        profile = clean(obligation.metadata.get("profile")).casefold()
        if profile == "meeting":
            return "meeting"
        if obligation.state == "ready":
            return "success"
        missing = obligation.missing_gates
        if any(gate.name in {"evidence", "validation", "acceptance"} for gate in missing):
            return "validation"
        if any(gate.name in {"decision", "ownership", "next_move"} for gate in missing):
            return "decision"
        if obligation.state == "blocked":
            return "blocked"
        return "waiting"

    @staticmethod
    def _status_label(obligation: Obligation) -> str:
        if obligation.state == "ready":
            return "CLOSE"
        if obligation.state == "waiting":
            return "WAIT"
        missing = obligation.missing_gates
        if missing:
            return {
                "deployment": "DEPLOY",
                "evidence": "PROOF",
                "validation": "VALID",
                "decision": "DECIDE",
                "capacity": "LOAD",
                "handoff": "BACKUP",
            }.get(missing[0].name, "ACT")
        return "NEXT"

    @staticmethod
    def _body_for(obligation: Obligation) -> str:
        parts = [
            f"owner {obligation.owner}",
            f"next {obligation.next_actor}",
            f"{len(obligation.missing_gates)} gates open",
        ]
        if obligation.followup_count:
            parts.append(f"{obligation.followup_count} follow-ups")
        return " · ".join(parts)

    def _state_for(self, gates: Sequence[Gate], next_actor: str) -> str:
        required = [gate for gate in gates if gate.required]
        if any(gate.state == "blocked" for gate in required):
            return "blocked"
        if fold(next_actor) not in self.self_aliases:
            return "waiting"
        if required and all(gate.complete for gate in required):
            return "ready"
        return "open"

    def _same_actor(self, first: str, second: str) -> bool:
        first_folded, second_folded = fold(first), fold(second)
        if first_folded in self.self_aliases and second_folded in self.self_aliases:
            return True
        return bool(first_folded and first_folded == second_folded)

    def _should_reopen(self, existing: Obligation, events: Sequence[Event]) -> bool:
        if existing.closed_at is None:
            return True
        for event in events:
            observation = self.ledger.observation(event.id)
            if observation:
                if observation.get("fingerprint") != _event_fingerprint(event):
                    return True
            elif event.occurred_at > existing.closed_at:
                return True
        return False

    def _export_if_due(self, obligations: Sequence[Obligation], now: datetime) -> None:
        if not self.export_path:
            return
        digest_payload = [
            (item.id, item.state, item.priority, item.updated_at.isoformat())
            for item in sorted(obligations, key=lambda obligation: obligation.id)
        ]
        digest = sha256(json.dumps(digest_payload, separators=(",", ":")).encode("utf-8")).hexdigest()
        monotonic = time.monotonic()
        if digest == self._last_export_digest and monotonic - self._last_export_monotonic < self.export_seconds:
            return
        self.ledger.export_snapshot(self.export_path, now=now, obligations=obligations)
        self._last_export_digest = digest
        self._last_export_monotonic = monotonic


def _event_fingerprint(event: Event) -> str:
    semantic = event.to_dict()
    # Poll-time leases and observation timestamps are volatile. They must not
    # reopen a manually closed obligation when source meaning is unchanged.
    semantic.pop("occurred_at", None)
    semantic.pop("expires_at", None)
    payload = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _same_gate_content(first: Gate, second: Gate) -> bool:
    return (
        first.name,
        first.state,
        first.owner,
        first.detail,
        first.required,
        first.evidence_ids,
    ) == (
        second.name,
        second.state,
        second.owner,
        second.detail,
        second.required,
        second.evidence_ids,
    )


def _stable_gate(previous: Gate | None, candidate: Gate) -> Gate:
    if previous is not None and _same_gate_content(previous, candidate):
        return replace(candidate, updated_at=previous.updated_at)
    return candidate
