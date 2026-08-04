from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from founder_os.automation.calendar_busy import (
    BusyBarMatterTarget,
    CalendarBusyAutomation,
    calendar_busy_events,
)
from founder_os.connectors.calendar import GoogleCalendarConnector
from founder_os.display.busybar import DisplayError
from founder_os.models import Event


UTC = timezone.utc
NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


def calendar_event(
    *,
    start: datetime = NOW - timedelta(minutes=10),
    end: datetime = NOW + timedelta(minutes=20),
    busy: bool = True,
    all_day: bool = False,
    response: str = "accepted",
) -> Event:
    return Event(
        id="calendar:meeting",
        source="calendar",
        kind="meeting",
        title="Customer call",
        occurred_at=NOW,
        expires_at=end,
        metadata={
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "calendar_busy": busy,
            "all_day": all_day,
            "response_status": response,
        },
    )


class RecordingTarget:
    def __init__(self, state: bool = False, error: Exception | None = None) -> None:
        self.state = state
        self.error = error
        self.calls: list[bool] = []

    def synchronize(self, busy: bool) -> bool:
        self.calls.append(busy)
        if self.error:
            raise self.error
        self.state = busy
        return self.state


class FakeBusyBar:
    def __init__(self, *, fabric_count: int = 1, state: bool = False) -> None:
        self.fabric_count = fabric_count
        self.state = state
        self.writes: list[tuple[bool, str]] = []

    def smart_home_pairing(self):
        return {"fabric_count": self.fabric_count}

    def smart_home_switch(self):
        return {"state": self.state}

    def set_smart_home_switch(self, state: bool, *, startup: str = "off") -> None:
        self.writes.append((state, startup))
        self.state = state


class CalendarBusyAutomationTests(unittest.TestCase):
    def test_active_opaque_meeting_turns_the_matter_switch_on(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(target)
        try:
            health = automation.reconcile(
                [calendar_event()],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(target.calls, [True])
        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["applied_busy"])
        self.assertEqual(health["active_event_count"], 1)

    def test_free_declined_tentative_and_all_day_filters_are_deterministic(self) -> None:
        events = [
            calendar_event(busy=False),
            calendar_event(response="declined"),
            calendar_event(response="tentative"),
            calendar_event(all_day=True),
        ]
        self.assertEqual(
            calendar_busy_events(
                events,
                NOW,
                include_all_day=False,
                include_tentative=False,
            ),
            [],
        )
        retained = calendar_busy_events(
            events,
            NOW,
            include_all_day=True,
            include_tentative=True,
        )
        self.assertEqual(len(retained), 2)

    def test_off_delay_prevents_flicker_then_turns_the_switch_off(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(target, off_delay_seconds=15)
        try:
            automation.reconcile(
                [calendar_event()],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
            automation.reconcile(
                [],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW + timedelta(seconds=5),
                wait=True,
            )
            self.assertEqual(target.calls, [True])
            health = automation.reconcile(
                [],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW + timedelta(seconds=21),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(target.calls, [True, False])
        self.assertFalse(health["applied_busy"])

    def test_stale_calendar_holds_the_last_state(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(target)
        try:
            automation.reconcile(
                [calendar_event()],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
            health = automation.reconcile(
                [],
                {"status": "stale", "last_success_at": NOW.isoformat()},
                NOW + timedelta(minutes=5),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(target.calls, [True])
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["applied_busy"])

    def test_failed_calendar_retry_does_not_recompute_the_last_snapshot(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(target, off_delay_seconds=0)
        try:
            automation.reconcile(
                [calendar_event()],
                {
                    "status": "healthy",
                    "failures": 0,
                    "last_success_at": NOW.isoformat(),
                    "last_error": "",
                },
                NOW,
                wait=True,
            )
            retrying = automation.reconcile(
                [],
                {
                    "status": "polling",
                    "failures": 1,
                    "last_success_at": NOW.isoformat(),
                    "last_error": "calendar retry in progress",
                },
                NOW + timedelta(hours=1),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(retrying["status"], "degraded")
        self.assertEqual(retrying["presence_state"], "meeting")
        self.assertTrue(retrying["desired_busy"])
        self.assertEqual(target.calls, [True])

    def test_recording_release_cannot_override_a_stale_calendar_meeting(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(target, off_delay_seconds=0)
        try:
            automation.occupancy.acquire(
                "streamdeck.recording",
                "recording",
                60,
                source="stream_deck",
                now=NOW,
            )
            first = automation.reconcile(
                [calendar_event()],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
            automation.occupancy.release(
                "streamdeck.recording",
                source="stream_deck",
                now=NOW + timedelta(seconds=1),
            )
            stale = automation.reconcile(
                [],
                {"status": "stale", "last_success_at": NOW.isoformat()},
                NOW + timedelta(seconds=1),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(first["presence_state"], "recording")
        self.assertEqual(stale["presence_state"], "meeting")
        self.assertTrue(stale["desired_busy"])
        self.assertEqual(target.calls, [True])

    def test_manual_lease_can_expire_without_an_initial_calendar_snapshot(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(
            target,
            off_delay_seconds=0,
            lease_min_ttl_seconds=5,
        )
        try:
            automation.occupancy.acquire(
                "streamdeck.call",
                "manual_call",
                5,
                source="stream_deck",
                now=NOW,
            )
            automation.reconcile([], {"status": "starting"}, NOW, wait=True)
            health = automation.reconcile(
                [],
                {"status": "starting"},
                NOW + timedelta(seconds=6),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(target.calls, [True, False])
        self.assertEqual(health["presence_state"], "available")
        self.assertFalse(health["applied_busy"])

    def test_manual_lease_expiry_completes_delayed_off_without_calendar_snapshot(self) -> None:
        target = RecordingTarget()
        automation = CalendarBusyAutomation(
            target,
            off_delay_seconds=15,
            lease_min_ttl_seconds=5,
        )
        try:
            automation.occupancy.acquire(
                "streamdeck.call",
                "manual_call",
                5,
                source="stream_deck",
                now=NOW,
            )
            automation.reconcile([], {"status": "starting"}, NOW, wait=True)
            delayed = automation.reconcile(
                [],
                {"status": "starting"},
                NOW + timedelta(seconds=6),
                wait=True,
            )
            health = automation.reconcile(
                [],
                {"status": "starting"},
                NOW + timedelta(seconds=22),
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(target.calls, [True, False])
        self.assertTrue(delayed["applied_busy"])
        self.assertEqual(health["presence_state"], "available")
        self.assertFalse(health["applied_busy"])

    def test_focus_never_activates_the_matter_switch(self) -> None:
        target = RecordingTarget(state=False)
        automation = CalendarBusyAutomation(target, off_delay_seconds=0)
        try:
            automation.occupancy.acquire(
                "streamdeck.focus",
                "focus",
                60,
                source="stream_deck",
                now=NOW,
            )
            health = automation.reconcile(
                [],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(health["presence_state"], "focus")
        self.assertFalse(health["desired_busy"])
        self.assertEqual(target.calls, [False])

    def test_target_failure_is_bounded_and_visible_as_health_only(self) -> None:
        target = RecordingTarget(error=DisplayError("POST /api/smart_home/switch failed"))
        automation = CalendarBusyAutomation(target, force_wait_seconds=1)
        try:
            health = automation.reconcile(
                [calendar_event()],
                {"status": "healthy", "last_success_at": NOW.isoformat()},
                NOW,
                wait=True,
            )
        finally:
            automation.close()
        self.assertEqual(health["status"], "degraded")
        self.assertIn("smart_home", health["last_error"])

    def test_busybar_target_requires_pairing_and_confirms_state(self) -> None:
        unpaired = BusyBarMatterTarget(FakeBusyBar(fabric_count=0))
        with self.assertRaises(DisplayError):
            unpaired.synchronize(True)
        client = FakeBusyBar(fabric_count=1)
        target = BusyBarMatterTarget(client)
        self.assertTrue(target.synchronize(True))
        self.assertEqual(client.writes, [(True, "off")])

    def test_calendar_normalization_marks_transparent_events_free_and_drops_declined(self) -> None:
        connector = GoogleCalendarConnector.__new__(GoogleCalendarConnector)
        connector.timezone = "Europe/Madrid"
        transparent = connector._normalize(
            {
                "id": "transparent",
                "summary": "Optional office hours",
                "transparency": "transparent",
                "start": {"dateTime": (NOW - timedelta(minutes=5)).isoformat()},
                "end": {"dateTime": (NOW + timedelta(minutes=25)).isoformat()},
                "updated": NOW.isoformat(),
                "attendees": [{"self": True, "responseStatus": "accepted"}],
            },
            NOW,
        )
        declined = connector._normalize(
            {
                "id": "declined",
                "summary": "Declined call",
                "start": {"dateTime": (NOW - timedelta(minutes=5)).isoformat()},
                "end": {"dateTime": (NOW + timedelta(minutes=25)).isoformat()},
                "updated": NOW.isoformat(),
                "attendees": [{"self": True, "responseStatus": "declined"}],
            },
            NOW,
        )
        self.assertIsNotNone(transparent)
        self.assertFalse(transparent.metadata["calendar_busy"])
        self.assertFalse(transparent.action_required)
        self.assertIsNone(declined)


if __name__ == "__main__":
    unittest.main()
