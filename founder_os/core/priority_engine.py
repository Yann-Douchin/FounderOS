"""Select exactly one winning event from the current event set."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from founder_os.models import Event, RankedEvent, utc_now
from founder_os.ranking.deterministic import DeterministicRanker
from founder_os.ranking.llm import TieBreaker


class PriorityEngine:
    def __init__(self, ranker: DeterministicRanker, tie_breaker: TieBreaker, *, tie_threshold: float = 2.0) -> None:
        self.ranker = ranker
        self.tie_breaker = tie_breaker
        self.tie_threshold = tie_threshold
        self.llm_fallback_calls = 0

    def select(self, events: Iterable[Event], now: datetime | None = None) -> RankedEvent | None:
        ranked = self.ranker.rank(events, now or utc_now())
        if not ranked:
            return None
        contenders = [ranked[0]]
        for candidate in ranked[1:]:
            if ranked[0].score - candidate.score <= self.tie_threshold:
                contenders.append(candidate)
            else:
                break
        if len(contenders) > 1:
            selected_id = self.tie_breaker.choose(contenders)
            if selected_id:
                self.llm_fallback_calls += 1
                for contender in contenders:
                    if contender.event.id == selected_id:
                        return contender
        return ranked[0]
