"""Small in-memory event bus with deduplication and expiry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from threading import RLock

from founder_os.models import Event, utc_now


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, *, default_ttl_minutes: float = 120) -> None:
        self._events: dict[str, Event] = {}
        self._subscribers: list[Subscriber] = []
        self._lock = RLock()
        self.default_ttl = timedelta(minutes=default_ttl_minutes)

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: Event) -> bool:
        """Publish an event. Returns True when its normalized value changed."""
        with self._lock:
            previous = self._events.get(event.id)
            changed = previous != event
            self._events[event.id] = event
            subscribers = tuple(self._subscribers) if changed else ()
        for callback in subscribers:
            callback(event)
        return changed

    def publish_many(self, events: Iterable[Event]) -> int:
        return sum(1 for event in events if self.publish(event))

    def reconcile_source(self, source: str, events: Iterable[Event]) -> tuple[int, int]:
        """Replace a connector snapshot and remove events absent from its latest poll."""
        snapshot = list(events)
        incoming_ids = {event.id for event in snapshot}
        changed = self.publish_many(snapshot)
        with self._lock:
            removed_ids = [
                event_id
                for event_id, event in self._events.items()
                if event.source == source and event_id not in incoming_ids
            ]
            for event_id in removed_ids:
                del self._events[event_id]
        return changed, len(removed_ids)

    def remove(self, event_id: str) -> None:
        with self._lock:
            self._events.pop(event_id, None)

    def active(self, now: datetime | None = None) -> list[Event]:
        now = now or utc_now()
        with self._lock:
            values = tuple(self._events.values())
        return [event for event in values if self._is_active(event, now)]

    def prune(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        with self._lock:
            expired = [event_id for event_id, event in self._events.items() if not self._is_active(event, now)]
            for event_id in expired:
                del self._events[event_id]
        return len(expired)

    def _is_active(self, event: Event, now: datetime) -> bool:
        if event.is_expired(now):
            return False
        if event.expires_at is None and event.occurred_at + self.default_ttl <= now:
            return False
        return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
