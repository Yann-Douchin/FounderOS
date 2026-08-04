"""Governed side effects driven by normalized FounderOS state."""

from .calendar_busy import BusyBarMatterTarget, CalendarBusyAutomation, calendar_busy_events
from .occupancy import (
    EXTERNAL_PRESENCE_STATES,
    MATTER_BUSY_STATES,
    PRESENCE_PRIORITY,
    OccupancyLease,
    OccupancyLeaseCoordinator,
    OccupancySnapshot,
)

__all__ = [
    "BusyBarMatterTarget",
    "CalendarBusyAutomation",
    "EXTERNAL_PRESENCE_STATES",
    "MATTER_BUSY_STATES",
    "PRESENCE_PRIORITY",
    "OccupancyLease",
    "OccupancyLeaseCoordinator",
    "OccupancySnapshot",
    "calendar_busy_events",
]
