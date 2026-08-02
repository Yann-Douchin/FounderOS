"""Explainable ranking with no network or model calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from founder_os.models import Event, RankedEvent, utc_now
from founder_os.ranking.memory import RankingMemory


URGENCY_WEIGHTS = {"low": -3.0, "normal": 0.0, "high": 8.0, "critical": 16.0}
IMPACT_WEIGHTS = {"low": -2.0, "medium": 0.0, "high": 7.0, "critical": 13.0}
KIND_WEIGHTS = {
    "information": 0.0,
    "message": 1.0,
    "email": 1.0,
    "meeting": 4.0,
    "deadline": 8.0,
    "waiting": 6.0,
    "blocker": 14.0,
    "incident": 16.0,
    "permission_request": 30.0,
    "agent_usage": -4.0,
    "obligation": 12.0,
}


class DeterministicRanker:
    def __init__(self, config: Mapping[str, Any], memory: RankingMemory) -> None:
        self.config = config
        self.memory = memory

    def rank(self, events: Iterable[Event], now: datetime | None = None) -> list[RankedEvent]:
        now = now or utc_now()
        ranked = [self.score(event, now) for event in events if not self.memory.is_suppressed(event, now)]
        ranked.sort(
            key=lambda item: (
                -item.score,
                -int(item.event.action_required),
                -item.event.occurred_at.timestamp(),
                item.event.id,
            )
        )
        return ranked

    def score(self, event: Event, now: datetime | None = None) -> RankedEvent:
        now = now or utc_now()
        source_weights = self.config.get("source_weights", {})
        components: dict[str, float] = {
            "base": float(event.priority),
            "source": float(source_weights.get(event.source, 0)),
            "action": float(self.config.get("action_required_bonus", 12)) if event.action_required else 0.0,
            "urgency": URGENCY_WEIGHTS.get(event.urgency, 0.0),
            "impact": IMPACT_WEIGHTS.get(event.impact, 0.0),
            "kind": KIND_WEIGHTS.get(event.kind, 0.0),
            "due": self._due_adjustment(event, now),
            "age": self._age_adjustment(event, now),
            "confidence": (event.confidence - 1.0) * 10.0,
            "memory": self.memory.score_adjustment(
                event,
                now=now,
                repeat_penalty=float(self.config.get("repeat_penalty", 7)),
                repeat_window_minutes=float(self.config.get("repeat_window_minutes", 20)),
                current_selection_bonus=float(self.config.get("current_selection_bonus", 3)),
            ),
        }
        return RankedEvent(event=event, score=round(sum(components.values()), 3), components=components)

    def _due_adjustment(self, event: Event, now: datetime) -> float:
        if not event.due_at:
            return 0.0
        seconds = (event.due_at - now).total_seconds()
        if seconds <= 0:
            return 18.0
        if seconds <= 5 * 60:
            return 16.0
        if seconds <= 15 * 60:
            return 12.0
        if seconds <= 60 * 60:
            return 7.0
        if seconds <= 4 * 60 * 60:
            return 3.0
        return 0.0

    def _age_adjustment(self, event: Event, now: datetime) -> float:
        age_minutes = max(0.0, (now - event.occurred_at).total_seconds() / 60.0)
        fresh_minutes = float(self.config.get("fresh_minutes", 15))
        if age_minutes <= fresh_minutes:
            return 0.0
        hours = (age_minutes - fresh_minutes) / 60.0
        return -min(20.0, hours * float(self.config.get("age_penalty_per_hour", 1.5)))
