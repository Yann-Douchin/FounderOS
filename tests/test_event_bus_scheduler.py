from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from founder_os.connectors.base import Connector
from founder_os.core.event_bus import EventBus
from founder_os.core.scheduler import Scheduler
from founder_os.models import Event


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


class SnapshotConnector(Connector):
    name = "test"

    def __init__(self) -> None:
        super().__init__({"poll_interval_seconds": 10})
        self.events = [Event(id="test:1", source="test", title="First", occurred_at=NOW)]

    def poll(self, now):
        return list(self.events)


class IncrementalConnector(SnapshotConnector):
    name = "incremental"
    emits_snapshot = False


class EventBusSchedulerTests(unittest.TestCase):
    def test_snapshot_removes_resolved_events(self) -> None:
        bus = EventBus(default_ttl_minutes=60)
        connector = SnapshotConnector()
        scheduler = Scheduler([connector], bus)
        scheduler.poll_due(NOW, force=True)
        self.assertEqual(len(bus.active(NOW)), 1)
        connector.events = []
        scheduler.poll_due(NOW + timedelta(seconds=11), force=True)
        self.assertEqual(bus.active(NOW + timedelta(seconds=11)), [])

    def test_incremental_empty_poll_keeps_previous_event(self) -> None:
        bus = EventBus(default_ttl_minutes=60)
        connector = IncrementalConnector()
        scheduler = Scheduler([connector], bus)
        scheduler.poll_due(NOW, force=True)
        connector.events = []
        scheduler.poll_due(NOW + timedelta(seconds=11), force=True)
        self.assertEqual(len(bus.active(NOW + timedelta(seconds=11))), 1)


if __name__ == "__main__":
    unittest.main()
