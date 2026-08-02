"""Deterministic multilingual signals used by the obligation engine."""

from __future__ import annotations

import re
from datetime import datetime, time as wall_time, timedelta
from hashlib import sha256
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from founder_os.closure.entities import clean, fold, relationship_domain
from founder_os.closure.models import Evidence, Gate
from founder_os.models import Event, parse_datetime


PROMISE_PHRASES = (
    "i will", "i’ll", "i'll", "we will", "we’ll", "we'll", "will send", "will share",
    "je vais", "je vous envoie", "je t’envoie", "nous allons", "on va", "je reviens vers",
)
WAITING_PHRASES = (
    "waiting for", "awaiting", "pending response", "please confirm", "need access", "needs access",
    "en attente", "dans l’attente", "merci de confirmer", "besoin d’accès", "attend la réponse",
)
FOLLOWUP_PHRASES = (
    "follow up", "following up", "gentle reminder", "reminder", "checking in",
    "relance", "je me permets de revenir", "rappel", "suite à mon message",
)
DECISION_PHRASES = (
    "need decision", "decision required", "please decide", "approval required", "needs approval",
    "décision requise", "besoin d’une décision", "merci d’arbitrer", "validation requise",
)
FEEDBACK_PHRASES = (
    "customer feedback", "client feedback", "design partner", "feature request", "user request",
    "retour client", "retour partenaire", "demande client", "suggestion produit",
)
RELEASE_PHRASES = (
    "go live", "go-live", "launch", "release", "deployment", "deploy", "production", "rollout",
    "mise en production", "déploiement", "lancement", "livraison",
)
READY_PHRASES = ("ready", "code complete", "code done", "prêt", "prête", "terminé côté code")
RESOLVED_PHRASES = (
    "resolved", "completed", "done", "closed", "approved", "validated", "delivered",
    "résolu", "résolue", "terminé", "terminée", "fermé", "fermée", "approuvé", "validé", "livré",
)
BLOCKED_PHRASES = (
    "blocked", "blocking", "failed", "missing", "need access", "cannot", "can’t", "can't",
    "not ready", "not complete", "not done",
    "bloqué", "bloquée", "échec", "manquant", "manquante", "besoin d’accès", "impossible",
    "pas prêt", "pas prête", "non prêt", "non prête", "pas terminé", "pas terminée",
)
ACCESS_SUCCESS_PHRASES = ("access granted", "permission granted", "accès obtenu", "accès accordé")
DEPLOYMENT_SUCCESS_PHRASES = (
    "deployed", "deployment succeeded", "live in production", "released",
    "déployé", "déploiement réussi", "en production", "mis en ligne",
)
VALIDATION_SUCCESS_PHRASES = (
    "validated", "approved", "qa passed", "accepted", "sign-off",
    "validé", "approuvé", "recette réussie", "accepté", "bon pour accord",
)
READY_NEGATIONS = (
    "not ready", "isn't ready", "is not ready", "not complete", "not completed", "not done",
    "pas prêt", "pas prête", "non prêt", "non prête", "pas terminé", "pas terminée", "non terminé",
)
DEPLOYMENT_NEGATIONS = (
    "not deployed", "deployment failed", "deployment did not succeed", "release failed",
    "pas déployé", "pas déployée", "non déployé", "non déployée", "échec du déploiement",
)
ACCESS_NEGATIONS = (
    "access not granted", "no access", "without access", "permission denied",
    "accès non obtenu", "accès refusé", "sans accès", "pas accès",
)
VALIDATION_NEGATIONS = (
    "not validated", "not approved", "not accepted", "validation failed", "rejected",
    "non validé", "non validée", "pas validé", "pas validée", "non approuvé", "refusé", "rejeté",
)
RESOLUTION_NEGATIONS = READY_NEGATIONS + VALIDATION_NEGATIONS + (
    "not resolved", "not closed", "not delivered", "non résolu", "non résolue", "pas livré", "pas livrée",
)
OUT_OF_OFFICE_PHRASES = (
    "out of office", "ooo", "vacation", "annual leave", "travel", "travelling", "flight",
    "absence", "congé", "congés", "vacances", "voyage", "déplacement", "vol ",
)

EVIDENCE_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "market": ("market", "country", "france", "spain", "italy", "germany", "marché", "pays"),
    "language": ("language", "locale", "translation", "french", "spanish", "langue", "traduction"),
    "pricing": ("price", "pricing", "discount", "currency", "prix", "tarif", "devise"),
    "analytics": ("analytics", "tracking", "posthog", "event count", "conversion", "mesure"),
    "device": ("device", "mobile", "desktop", "tablet", "browser", "appareil", "navigateur"),
    "deployment": DEPLOYMENT_SUCCESS_PHRASES,
    "access": ACCESS_SUCCESS_PHRASES,
    "validation": VALIDATION_SUCCESS_PHRASES,
    "document": ("document", "proposal", "contract", "sheet", "notion", "drive", "document", "contrat"),
    "monitoring": ("sentry", "error free", "no regression", "monitoring", "sans erreur", "régression"),
    "merchant": ("shopify", "catalog", "merchant", "catalogue", "marchand"),
    "finance": ("invoice", "payment", "refund", "stripe", "facture", "paiement", "remboursement"),
}

PROFILE_GATES: Mapping[str, tuple[str, ...]] = {
    "release": ("code", "deployment", "access", "evidence", "validation"),
    "feedback": ("ownership", "decision"),
    "meeting": ("decision", "next_move"),
    "commitment": ("delivery", "acceptance"),
    "decision": ("decision",),
    "capacity": ("capacity", "handoff"),
}
EXPLICIT_GATE_DETAIL = "Explicit source gate state"


def event_text(event: Event) -> str:
    metadata_values = [
        event.metadata.get("state"), event.metadata.get("status"), event.metadata.get("classification"),
        event.metadata.get("signal"), event.metadata.get("project"), event.metadata.get("customer"),
        event.metadata.get("description"), event.metadata.get("decision"), event.metadata.get("action_item"),
        event.metadata.get("next_move"),
    ]
    return " ".join(clean(value) for value in (event.title, event.body, *metadata_values) if clean(value)).casefold()


def infer_profile(event: Event) -> str:
    explicit = clean(event.metadata.get("gate_profile") or event.metadata.get("obligation_type")).casefold()
    if explicit in PROFILE_GATES:
        return explicit
    text = event_text(event)
    if event.metadata.get("meeting_phase") or event.kind == "meeting":
        return "meeting"
    if event.metadata.get("feedback") or contains_any(text, FEEDBACK_PHRASES):
        return "feedback"
    if contains_any(text, RELEASE_PHRASES):
        return "release"
    if event.kind in {"blocker", "waiting"} or contains_any(text, WAITING_PHRASES):
        return "commitment"
    if contains_any(text, DECISION_PHRASES) or event.metadata.get("signal") == "decision":
        return "decision"
    return "commitment"


def is_obligation_candidate(event: Event) -> bool:
    if event.kind in {"permission_request", "connector_health", "agent_usage"}:
        return False
    text = event_text(event)
    if event.kind == "meeting":
        phase = clean(event.metadata.get("meeting_phase")).casefold()
        return bool(
            phase in {"before", "after"}
            or event.metadata.get("rsvp_required")
            or event.metadata.get("obligation")
            or event.metadata.get("decision")
            or event.metadata.get("action_item")
        )
    return bool(
        event.action_required
        or event.kind in {"blocker", "waiting", "deadline", "feedback", "deployment", "incident"}
        or event.metadata.get("feedback")
        or (event.kind == "meeting" and bool(event.action_required or event.metadata.get("meeting_phase") in {"before", "after"}))
        or contains_any(text, PROMISE_PHRASES + WAITING_PHRASES + DECISION_PHRASES + FEEDBACK_PHRASES)
        or event.metadata.get("obligation")
    )


def infer_owner(event: Event, default_owner: str) -> str:
    metadata = event.metadata
    if metadata.get("assigned_to_viewer"):
        return default_owner
    owner = clean(metadata.get("action_owner") or metadata.get("assignee") or metadata.get("responsible"))
    if owner and owner.casefold() not in {"unassigned", "none", "nobody"}:
        return owner
    direction = clean(metadata.get("direction")).casefold()
    if direction == "incoming":
        return default_owner
    owner = clean(metadata.get("owner"))
    if owner and owner.casefold() not in {"unassigned", "none", "nobody"}:
        return owner
    if direction == "outgoing" or contains_any(event_text(event), PROMISE_PHRASES):
        return default_owner
    return default_owner


def infer_counterparty(event: Event) -> str:
    metadata = event.metadata
    direction = clean(metadata.get("direction")).casefold()
    value = clean(
        metadata.get("counterparty") or metadata.get("customer") or metadata.get("account")
        or metadata.get("organization")
        or (metadata.get("sender_name") if direction != "outgoing" else "")
        or (metadata.get("sender") if direction != "outgoing" else "")
    )
    return value or relationship_domain(event)


def infer_next_actor(event: Event, owner: str, default_owner: str) -> str:
    metadata = event.metadata
    explicit = clean(metadata.get("next_actor") or metadata.get("dependency_owner"))
    if explicit:
        return explicit
    text = event_text(event)
    dependency = _dependency_actor(" ".join((event.title, event.body)))
    if dependency:
        if fold(dependency) in {"you", "vous", "toi", "your_team", "votre_equipe"}:
            return default_owner
        return dependency
    direction = clean(metadata.get("direction")).casefold()
    if direction == "incoming":
        return default_owner
    if contains_any(text, WAITING_PHRASES) or event.kind == "waiting":
        return infer_counterparty(event) or clean(metadata.get("assignee")) or "external"
    return owner or default_owner


def infer_due_at(event: Event, timezone: ZoneInfo) -> datetime | None:
    if event.due_at:
        return event.due_at
    text = " ".join((event.title, event.body))
    folded = text.casefold()
    local = event.occurred_at.astimezone(timezone)
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", folded)
    if iso_match:
        try:
            value = datetime(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)), 18, tzinfo=timezone)
            return value.astimezone(event.occurred_at.tzinfo)
        except ValueError:
            pass
    local_match = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", folded)
    if local_match:
        try:
            value = datetime(int(local_match.group(3)), int(local_match.group(2)), int(local_match.group(1)), 18, tzinfo=timezone)
            return value.astimezone(event.occurred_at.tzinfo)
        except ValueError:
            pass
    if any(value in folded for value in ("tomorrow", "demain")):
        return datetime.combine(local.date() + timedelta(days=1), wall_time(18), timezone).astimezone(event.occurred_at.tzinfo)
    if any(value in folded for value in ("today", "aujourd’hui", "aujourd'hui")):
        return datetime.combine(local.date(), wall_time(18), timezone).astimezone(event.occurred_at.tzinfo)
    if any(value in folded for value in ("end of week", "by friday", "fin de semaine", "d’ici vendredi", "d'ici vendredi")):
        days = (4 - local.weekday()) % 7
        return datetime.combine(local.date() + timedelta(days=days), wall_time(18), timezone).astimezone(event.occurred_at.tzinfo)
    return None


def infer_project(event: Event) -> str:
    return clean(event.metadata.get("project") or event.metadata.get("initiative"))


def infer_relationship_key(event: Event) -> str:
    explicit = clean(event.metadata.get("relationship_key"))
    if explicit:
        return explicit.casefold()
    domain = relationship_domain(event)
    return domain or clean(event.metadata.get("customer") or event.metadata.get("account")).casefold()


def infer_gates(event: Event, profile: str, now: datetime) -> tuple[Gate, ...]:
    text = event_text(event)
    explicit_status = event.metadata.get("gate_status") or {}
    status_map = explicit_status if isinstance(explicit_status, Mapping) else {}
    owner = infer_owner(event, "self")
    gates: list[Gate] = []
    for name in PROFILE_GATES.get(profile, PROFILE_GATES["commitment"]):
        explicit = clean(status_map.get(name)).casefold()
        if explicit in {"pending", "blocked", "satisfied", "waived"}:
            gates.append(Gate(
                name=name,
                state=explicit,
                owner=owner,
                detail=EXPLICIT_GATE_DETAIL,
                updated_at=now,
            ))
            continue
        state = "pending"
        detail = ""
        if name == "code" and _positive_signal(text, READY_PHRASES + RESOLVED_PHRASES, READY_NEGATIONS):
            state, detail = "satisfied", "Code is reported ready"
        elif name == "deployment":
            if event.metadata.get("deployment_status") == "success" or _positive_signal(
                text, DEPLOYMENT_SUCCESS_PHRASES, DEPLOYMENT_NEGATIONS
            ):
                state, detail = "satisfied", "Deployment evidence is present"
            elif event.metadata.get("deployment_status") in {"failed", "error"} or contains_any(text, DEPLOYMENT_NEGATIONS):
                state, detail = "blocked", "Deployment failed"
        elif name == "access":
            if event.metadata.get("access_status") == "granted" or _positive_signal(
                text, ACCESS_SUCCESS_PHRASES, ACCESS_NEGATIONS
            ):
                state, detail = "satisfied", "Required access is available"
            elif contains_any(text, ACCESS_NEGATIONS) or "access" in text or "accès" in text:
                state, detail = "blocked", "Required access is missing"
        elif name == "validation" and _positive_signal(text, VALIDATION_SUCCESS_PHRASES, VALIDATION_NEGATIONS):
            state, detail = "satisfied", "Final validation is present"
        elif name == "ownership":
            has_owner = bool(clean(event.metadata.get("owner") or event.metadata.get("assignee")))
            state, detail = ("satisfied", "Feedback has an owner") if has_owner else ("blocked", "Feedback has no owner")
        elif name == "decision":
            linked = bool(clean(event.metadata.get("decision_id") or event.metadata.get("decision")))
            if linked or _positive_signal(text, VALIDATION_SUCCESS_PHRASES, VALIDATION_NEGATIONS):
                state, detail = "satisfied", "Decision is recorded"
        elif name == "delivery" and _positive_signal(text, RESOLVED_PHRASES, RESOLUTION_NEGATIONS):
            state, detail = "satisfied", "Promised delivery is reported complete"
        elif name == "acceptance" and _positive_signal(text, VALIDATION_SUCCESS_PHRASES, VALIDATION_NEGATIONS):
            state, detail = "satisfied", "Counterparty acceptance is present"
        elif name == "next_move":
            next_move = clean(event.metadata.get("next_move") or event.metadata.get("action_item"))
            if next_move:
                state, detail = "satisfied", next_move
        if state == "pending" and (event.kind == "blocker" or contains_any(text, BLOCKED_PHRASES)):
            gate_specific_signal = any(value in text for value in ("access", "accès", "deploy", "déploi", "proof", "preuve"))
            if name == "delivery" or (name == "code" and not gate_specific_signal):
                state = "blocked"
                detail = detail or "The source reports a blocker"
        gates.append(Gate(name=name, state=state, owner=owner, detail=detail, updated_at=now))
    return tuple(gates)


def infer_evidence(event: Event, now: datetime, ttl_hours: float) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = []
    raw = event.metadata.get("evidence")
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, Mapping):
                category = clean(item.get("category"))
                if not category:
                    continue
                observed_at = parse_datetime(
                    item.get("observed_at"),
                    default=event.occurred_at,
                ) or event.occurred_at
                evidence.append(Evidence(
                    id=clean(item.get("id")) or _evidence_id(event.id, category, str(index)),
                    category=category,
                    state=clean(item.get("state")) or "present",
                    scope=clean(item.get("scope")),
                    source=event.source,
                    source_event_id=event.id,
                    owner=clean(item.get("owner")),
                    detail=clean(item.get("detail")),
                    observed_at=observed_at,
                    expires_at=(
                        parse_datetime(item.get("expires_at"))
                        or observed_at + timedelta(hours=max(1.0, ttl_hours))
                    ),
                ))
    evidence_status = clean(event.metadata.get("evidence_status") or "present").casefold()
    categories = event.metadata.get("evidence_categories")
    if isinstance(categories, list):
        for category in categories:
            normalized = clean(category)
            if normalized and evidence_status == "present" and not any(item.category == normalized.casefold().replace(" ", "_") for item in evidence):
                evidence.append(_event_evidence(event, normalized, now, ttl_hours))
    text = event_text(event)
    source_category = {
        "github": "code", "deployment": "deployment", "sentry": "monitoring",
        "posthog": "analytics", "notion": "document", "drive": "document", "sheets": "document",
        "shopify": "merchant", "stripe": "finance",
    }.get(event.source)
    if source_category and evidence_status == "present" and not any(item.category == source_category for item in evidence):
        evidence.append(_event_evidence(event, source_category, now, ttl_hours))
    for category, phrases in EVIDENCE_KEYWORDS.items():
        if (
            contains_any(text, phrases)
            and _positive_signal(
                text,
                RESOLVED_PHRASES + VALIDATION_SUCCESS_PHRASES,
                RESOLUTION_NEGATIONS,
            )
            and not any(item.category == category for item in evidence)
        ):
            evidence.append(_event_evidence(event, category, now, ttl_hours))
    return tuple({item.id: item for item in evidence}.values())


def is_feedback(event: Event) -> bool:
    return bool(event.metadata.get("feedback") or contains_any(event_text(event), FEEDBACK_PHRASES))


def is_followup(event: Event) -> bool:
    return contains_any(event_text(event), FOLLOWUP_PHRASES)


def availability_signal(event: Event) -> Mapping[str, Any] | None:
    text = event_text(event)
    explicit = clean(event.metadata.get("availability")).casefold()
    if explicit not in {"unavailable", "ooo", "travel"} and not contains_any(text, OUT_OF_OFFICE_PHRASES):
        return None
    owner = clean(event.metadata.get("owner") or event.metadata.get("assignee") or event.metadata.get("person")) or "self"
    start = parse_datetime(event.metadata.get("start_at"), default=event.occurred_at) or event.occurred_at
    end = parse_datetime(event.metadata.get("end_at"), default=event.expires_at) or event.expires_at or start + timedelta(days=1)
    return {"owner": owner, "start": start, "end": end, "reason": event.title}


def contains_any(text: str, phrases: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(
        bool(re.search(r"(?<!\w)" + re.escape(phrase.casefold().strip()) + r"(?!\w)", folded))
        for phrase in phrases
        if phrase and phrase.strip()
    )


def _positive_signal(text: str, positive: Iterable[str], negative: Iterable[str]) -> bool:
    return contains_any(text, positive) and not contains_any(text, negative)


def _dependency_actor(value: str) -> str:
    normalized = " ".join(value.split())
    patterns = (
        r"\bwaiting for\s+([\wÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[\wÀ-ÖØ-öø-ÿ'’.-]+)?)",
        r"\bawaiting\s+([\wÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[\wÀ-ÖØ-öø-ÿ'’.-]+)?)",
        r"\ben attente d(?:e|’|')\s*([\wÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[\wÀ-ÖØ-öø-ÿ'’.-]+)?)",
        r"\bbloqu[ée]e? par\s+([\wÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[\wÀ-ÖØ-öø-ÿ'’.-]+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return clean(match.group(1)).rstrip(".,:;!?")[:80]
    return ""


def _event_evidence(event: Event, category: str, now: datetime, ttl_hours: float) -> Evidence:
    return Evidence(
        id=_evidence_id(event.id, category, "signal"),
        category=category,
        source=event.source,
        source_event_id=event.id,
        detail=event.title,
        observed_at=event.occurred_at,
        # Evidence freshness follows the source observation, not the time at
        # which FounderOS happened to poll the unchanged source again.
        expires_at=event.occurred_at + timedelta(hours=max(1.0, ttl_hours)),
    )


def _evidence_id(event_id: str, category: str, suffix: str) -> str:
    raw = "\x1f".join((event_id, category, suffix)).encode("utf-8")
    return "evidence:" + sha256(raw).hexdigest()[:20]
