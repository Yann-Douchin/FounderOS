from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from founder_os.connectors.base import Connector, ConnectorStaleError
from founder_os.core.event_bus import EventBus
from founder_os.core.scheduler import Scheduler
from founder_os.models import Event


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


class MutableConnector(Connector):
    name = "linear"

    def __init__(self) -> None:
        super().__init__({"poll_interval_seconds": 1, "poll_timeout_seconds": 2, "critical": True})
        self.events = [Event(source="linear", id="linear:1", title="Décision active", occurred_at=NOW)]
        self.error: Exception | None = None

    def poll(self, now: datetime) -> list[Event]:
        if self.error:
            raise self.error
        return list(self.events)


class BlockingConnector(Connector):
    name = "gmail"

    def __init__(self, release: threading.Event) -> None:
        super().__init__({"poll_interval_seconds": 30, "poll_timeout_seconds": 2})
        self.release = release

    def poll(self, now: datetime) -> list[Event]:
        self.release.wait(2)
        return []


class FastConnector(Connector):
    name = "calendar"

    def __init__(self) -> None:
        super().__init__({"poll_interval_seconds": 30, "poll_timeout_seconds": 2})

    def poll(self, now: datetime) -> list[Event]:
        return [Event(source="calendar", id="calendar:fast", title="Rapide", occurred_at=now)]


class SchedulerHealthTests(unittest.TestCase):
    def test_snapshot_ownership_does_not_depend_on_event_source(self) -> None:
        bus = EventBus(default_ttl_minutes=120)
        connector = MutableConnector()
        connector.events = [
            Event(source="slack", id="demo:foreign-source", title="Événement simulé", occurred_at=NOW)
        ]
        scheduler = Scheduler([connector], bus)
        try:
            scheduler.poll_due(NOW, force=True)
            self.assertTrue(any(event.id == "demo:foreign-source" for event in bus.active(NOW)))
            connector.events = []
            scheduler.poll_due(NOW + timedelta(seconds=2), force=True)
            self.assertFalse(
                any(event.id == "demo:foreign-source" for event in bus.active(NOW + timedelta(seconds=2)))
            )
        finally:
            scheduler.close()

    def test_stale_source_preserves_last_known_events_and_emits_health(self) -> None:
        bus = EventBus(default_ttl_minutes=120)
        connector = MutableConnector()
        scheduler = Scheduler([connector], bus)
        try:
            scheduler.poll_due(NOW, force=True)
            connector.error = ConnectorStaleError("snapshot périmé")
            scheduler.poll_due(NOW + timedelta(seconds=2), force=True)
            active = bus.active(NOW + timedelta(seconds=2))
            self.assertTrue(any(event.id == "linear:1" for event in active))
            health = next(event for event in active if event.kind == "connector_health")
            self.assertIn("périmées", health.title)
            self.assertEqual(scheduler.health_snapshot()["linear"]["status"], "stale")
        finally:
            scheduler.close()

    def test_slow_connector_does_not_block_fast_connector(self) -> None:
        release = threading.Event()
        bus = EventBus(default_ttl_minutes=120)
        scheduler = Scheduler([BlockingConnector(release), FastConnector()], bus, max_workers=2)
        try:
            started = time.monotonic()
            scheduler.poll_due(NOW)
            self.assertLess(time.monotonic() - started, 0.2)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                scheduler.poll_due(NOW)
                if any(event.id == "calendar:fast" for event in bus.active(NOW)):
                    break
                time.sleep(0.01)
            self.assertTrue(any(event.id == "calendar:fast" for event in bus.active(NOW)))
        finally:
            release.set()
            scheduler.close()


if __name__ == "__main__":
    unittest.main()
