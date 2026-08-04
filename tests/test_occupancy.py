from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from founder_os.automation.occupancy import OccupancyLeaseCoordinator


UTC = timezone.utc
NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


class OccupancyLeaseCoordinatorTests(unittest.TestCase):
    def test_priority_ttl_and_focus_matter_semantics(self) -> None:
        coordinator = OccupancyLeaseCoordinator(min_ttl_seconds=5, max_ttl_seconds=600)
        coordinator.acquire("focus", "focus", 30, source="stream_deck", now=NOW)
        focus = coordinator.snapshot(NOW)
        self.assertEqual(focus.state, "focus")
        self.assertFalse(focus.busy)

        coordinator.acquire("call", "manual_call", 30, source="stream_deck", now=NOW)
        coordinator.set_calendar_busy(True)
        meeting = coordinator.snapshot(NOW)
        self.assertEqual(meeting.state, "meeting")
        self.assertTrue(meeting.busy)

        coordinator.acquire("recording", "recording", 10, source="stream_deck", now=NOW)
        self.assertEqual(coordinator.snapshot(NOW).state, "recording")
        self.assertEqual(
            coordinator.snapshot(NOW + timedelta(seconds=11)).state,
            "meeting",
        )

    def test_acquire_is_idempotent_only_for_the_same_owner_and_state(self) -> None:
        coordinator = OccupancyLeaseCoordinator(min_ttl_seconds=5, max_ttl_seconds=600)
        first = coordinator.acquire("studio", "recording", 10, source="stream_deck", now=NOW)
        renewed = coordinator.acquire(
            "studio",
            "recording",
            20,
            source="stream_deck",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(first.issued_at, renewed.issued_at)
        self.assertEqual(renewed.expires_at, NOW + timedelta(seconds=22))
        with self.assertRaisesRegex(ValueError, "different owner or state"):
            coordinator.acquire(
                "studio",
                "focus",
                20,
                source="stream_deck",
                now=NOW + timedelta(seconds=3),
            )
        with self.assertRaisesRegex(ValueError, "not available"):
            coordinator.acquire("calendar", "meeting", 20, source="stream_deck", now=NOW)

    def test_renew_requires_a_live_owned_lease(self) -> None:
        coordinator = OccupancyLeaseCoordinator(min_ttl_seconds=5, max_ttl_seconds=600)
        coordinator.acquire("studio", "recording", 10, source="stream_deck", now=NOW)
        renewed = coordinator.renew(
            "studio",
            20,
            source="stream_deck",
            now=NOW + timedelta(seconds=5),
        )
        self.assertEqual(renewed.expires_at, NOW + timedelta(seconds=25))
        with self.assertRaisesRegex(ValueError, "absent, expired"):
            coordinator.renew(
                "studio",
                20,
                source="stream_deck",
                now=NOW + timedelta(seconds=26),
            )

    def test_release_all_is_source_scoped_and_never_clears_calendar(self) -> None:
        coordinator = OccupancyLeaseCoordinator(min_ttl_seconds=5, max_ttl_seconds=600)
        coordinator.set_calendar_busy(True)
        coordinator.acquire("deck.call", "manual_call", 30, source="stream_deck", now=NOW)
        coordinator.acquire("other.focus", "focus", 30, source="other", now=NOW)
        self.assertEqual(coordinator.release_all(source="stream_deck", now=NOW), 1)
        snapshot = coordinator.snapshot(NOW)
        self.assertEqual(snapshot.state, "meeting")
        self.assertTrue(snapshot.calendar_busy)
        self.assertEqual(snapshot.active_lease_count, 1)

    def test_ttl_bounds_are_enforced(self) -> None:
        coordinator = OccupancyLeaseCoordinator(min_ttl_seconds=5, max_ttl_seconds=60)
        with self.assertRaisesRegex(ValueError, "between 5 and 60"):
            coordinator.acquire("too-short", "focus", 4, source="stream_deck", now=NOW)
        with self.assertRaisesRegex(ValueError, "between 5 and 60"):
            coordinator.acquire("too-long", "focus", 61, source="stream_deck", now=NOW)


if __name__ == "__main__":
    unittest.main()
