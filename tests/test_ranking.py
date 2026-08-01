from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from founder_os.core.priority_engine import PriorityEngine
from founder_os.models import Event
from founder_os.ranking.deterministic import DeterministicRanker
from founder_os.ranking.memory import RankingMemory


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


RANKING_CONFIG = {
    "source_weights": {"linear": 8, "gmail": 2},
    "action_required_bonus": 12,
    "current_selection_bonus": 3,
    "fresh_minutes": 15,
    "age_penalty_per_hour": 1.5,
    "repeat_penalty": 7,
    "repeat_window_minutes": 20,
}


class FakeTieBreaker:
    def __init__(self, selection: str | None = None) -> None:
        self.selection = selection
        self.calls = 0

    def choose(self, candidates):
        self.calls += 1
        return self.selection


class RankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = RankingMemory(Path(self.temp.name) / "memory.json")
        self.ranker = DeterministicRanker(RANKING_CONFIG, self.memory)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_blocker_wins_without_llm_call(self) -> None:
        blocker = Event(
            id="linear:1",
            source="linear",
            title="Quantity Fix blocked",
            priority=90,
            action_required=True,
            kind="blocker",
            urgency="critical",
            impact="high",
            occurred_at=NOW,
        )
        email = Event(
            id="gmail:1",
            source="gmail",
            title="Investor question",
            priority=72,
            action_required=True,
            kind="email",
            occurred_at=NOW,
        )
        tie_breaker = FakeTieBreaker()
        engine = PriorityEngine(self.ranker, tie_breaker, tie_threshold=2)
        selected = engine.select([email, blocker], NOW)
        self.assertEqual(selected.event.id, blocker.id)
        self.assertEqual(tie_breaker.calls, 0)

    def test_close_tie_can_use_fallback(self) -> None:
        first = Event(id="gmail:1", source="gmail", title="One", priority=70, occurred_at=NOW)
        second = Event(id="gmail:2", source="gmail", title="Two", priority=69, occurred_at=NOW)
        tie_breaker = FakeTieBreaker(second.id)
        engine = PriorityEngine(self.ranker, tie_breaker, tie_threshold=2)
        selected = engine.select([first, second], NOW)
        self.assertEqual(selected.event.id, second.id)
        self.assertEqual(tie_breaker.calls, 1)
        self.assertEqual(engine.llm_fallback_calls, 1)

    def test_acknowledged_event_is_removed(self) -> None:
        event = Event(id="linear:1", source="linear", title="Decision", priority=90, occurred_at=NOW)
        self.memory.acknowledge(event)
        self.assertEqual(self.ranker.rank([event], NOW), [])

    def test_agent_permission_outranks_a_regular_blocker(self) -> None:
        permission = Event(
            source="chatgpt_codex",
            title="Autoriser Bash ?",
            priority=100,
            action_required=True,
            kind="permission_request",
            urgency="critical",
            impact="high",
        )
        blocker = Event(
            source="linear",
            title="Production bloquée",
            priority=95,
            action_required=True,
            kind="blocker",
            urgency="critical",
            impact="high",
        )
        ranked = self.ranker.rank([blocker, permission], NOW)
        self.assertEqual(ranked[0].event, permission)

    def test_old_event_receives_age_penalty(self) -> None:
        fresh = Event(id="gmail:fresh", source="gmail", title="Fresh", priority=50, occurred_at=NOW)
        old = Event(id="gmail:old", source="gmail", title="Old", priority=50, occurred_at=NOW - timedelta(hours=4))
        scores = {item.event.id: item for item in self.ranker.rank([old, fresh], NOW)}
        self.assertLess(scores[old.id].components["age"], scores[fresh.id].components["age"])


if __name__ == "__main__":
    unittest.main()
