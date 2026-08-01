"""FounderOS runtime: poll, rank, select one event, and render one frame."""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from founder_os.config import load_config
from founder_os.connectors.registry import build_connectors
from founder_os.core.event_bus import EventBus
from founder_os.core.priority_engine import PriorityEngine
from founder_os.core.scheduler import Scheduler
from founder_os.display.busybar import BusyBarDisplay, Display, DisplayConflict, DisplayError
from founder_os.display.layouts import event_layout, idle_layout
from founder_os.interaction import EmulatorInputListener
from founder_os.models import RankedEvent, utc_now
from founder_os.ranking.deterministic import DeterministicRanker
from founder_os.ranking.llm import NoLLMFallback, OpenAIResponsesTieBreaker, TieBreaker
from founder_os.ranking.memory import RankingMemory


@dataclass(slots=True)
class RuntimeState:
    selected: RankedEvent | None
    event_count: int
    connector_counts: Mapping[str, int]
    displayed: bool
    display_error: str = ""


class FounderOSRuntime:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        display: Display | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.log = logger or logging.getLogger("founderos")
        runtime_config = config["runtime"]
        display_config = config["display"]
        ranking_config = config["ranking"]
        self.bus = EventBus(default_ttl_minutes=float(runtime_config["event_ttl_minutes"]))
        self.memory = RankingMemory(Path(str(config["memory"]["path"])))
        self.connectors = build_connectors(config["connectors"])
        self.scheduler = Scheduler(self.connectors, self.bus, self.log)
        self.rank_engine = PriorityEngine(
            DeterministicRanker(ranking_config, self.memory),
            self._tie_breaker(config["llm"]),
            tie_threshold=float(ranking_config["tie_threshold"]),
        )
        self.display = display or BusyBarDisplay(
            str(display_config["host"]),
            application_name=str(display_config["application_name"]),
            priority=int(display_config["device_priority"]),
            timeout=float(display_config["request_timeout_seconds"]),
        )
        self.tick_seconds = float(runtime_config["tick_seconds"])
        self.refresh_seconds = float(runtime_config["refresh_seconds"])
        self.min_hold_seconds = float(display_config["min_hold_seconds"])
        self.show_idle = bool(display_config["show_idle"])
        icon_config = display_config["content_icon"]
        self.content_icon_enabled = bool(icon_config["enabled"])
        self.icon_frame_seconds = float(icon_config["frame_seconds"])
        interaction_config = config["interaction"]
        self.allow_key = str(interaction_config["allow_key"]).strip().lower()
        self.deny_key = str(interaction_config["deny_key"]).strip().lower()
        self.input_listener: EmulatorInputListener | None = None
        if bool(interaction_config["enabled"]):
            event_url = str(interaction_config.get("event_url", "")).strip()
            if not event_url:
                host = str(display_config["host"])
                event_url = (host if host.startswith(("http://", "https://")) else "http://" + host)
                event_url = event_url.rstrip("/") + "/events"
            self.input_listener = EmulatorInputListener(
                event_url,
                self.handle_input,
                reconnect_seconds=float(interaction_config["reconnect_seconds"]),
                logger=self.log,
            )
        self._selected: RankedEvent | None = None
        self._selected_since = 0.0
        self._last_draw_at = 0.0
        self._idle_drawn = False
        self._last_icon_frame: int | None = None
        self._stop = False

    @classmethod
    def from_path(
        cls,
        path: str | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
        display: Display | None = None,
    ) -> "FounderOSRuntime":
        return cls(load_config(path, overrides=overrides), display=display)

    def tick(self, now: datetime | None = None, *, force_poll: bool = False) -> RuntimeState:
        now = now or utc_now()
        connector_counts = self.scheduler.poll_due(now, force=force_poll)
        self.bus.prune(now)
        candidate = self.rank_engine.select(self.bus.active(now), now)
        candidate = self._respect_hold(candidate, now)
        displayed, error = self._render(candidate, now)
        return RuntimeState(
            selected=candidate,
            event_count=len(self.bus.active(now)),
            connector_counts=connector_counts,
            displayed=displayed,
            display_error=error,
        )

    def run(self) -> None:
        self._install_signal_handlers()
        self.log.info("FounderOS started with %d connector(s)", len(self.connectors))
        if self.input_listener:
            self.input_listener.start()
            self.log.info("emulator approval input enabled: %s=allow, %s=deny", self.allow_key, self.deny_key)
        try:
            while not self._stop:
                self.tick()
                time.sleep(self.tick_seconds)
        finally:
            if self.input_listener:
                self.input_listener.close()
            self.scheduler.close()

    def stop(self) -> None:
        self._stop = True

    def handle_input(self, key: str) -> str | None:
        key = key.strip().lower()
        decision = "allow" if key == self.allow_key else "deny" if key == self.deny_key else None
        selected = self._selected
        if decision is None or selected is None or selected.event.kind != "permission_request":
            return None
        request_id = str(selected.event.metadata.get("request_id", ""))
        for connector in self.connectors:
            decide = getattr(connector, "decide", None)
            if connector.name != selected.event.source or not callable(decide):
                continue
            if decide(request_id, decision, input_key=key):
                self.bus.remove(selected.event.id)
                self.log.info("%s permission %s from BUSY Bar input", selected.event.source, decision)
                return decision
        return None

    def _respect_hold(self, candidate: RankedEvent | None, now: datetime) -> RankedEvent | None:
        monotonic = time.monotonic()
        if (
            self._selected
            and candidate
            and candidate.event.id != self._selected.event.id
            and monotonic - self._selected_since < self.min_hold_seconds
            and any(event.id == self._selected.event.id for event in self.bus.active(now))
        ):
            return self._selected
        return candidate

    def _render(self, selected: RankedEvent | None, now: datetime) -> tuple[bool, str]:
        monotonic = time.monotonic()
        changed = (selected.event.id if selected else None) != (self._selected.event.id if self._selected else None)
        refresh_due = monotonic - self._last_draw_at >= self.refresh_seconds
        icon_frame = (
            int(monotonic / self.icon_frame_seconds)
            if selected is not None and self.content_icon_enabled
            else None
        )
        icon_changed = icon_frame != self._last_icon_frame
        if not changed and not refresh_due and not icon_changed:
            return False, ""
        try:
            if selected:
                self.display.draw(event_layout(selected, now, icon_frame=icon_frame))
                if changed:
                    self.memory.mark_displayed(selected.event, now)
                    self._selected_since = monotonic
                self._idle_drawn = False
            elif self.show_idle:
                self.display.draw(idle_layout())
                self.memory.clear_current()
                self._idle_drawn = True
            else:
                self.display.clear()
                self.memory.clear_current()
                self._idle_drawn = False
            self._selected = selected
            self._last_draw_at = monotonic
            self._last_icon_frame = icon_frame
            return True, ""
        except DisplayConflict as exc:
            self.log.info("BUSY Bar is owned by a higher-priority app: %s", exc)
            return False, str(exc)
        except DisplayError as exc:
            self.log.warning("display unavailable: %s", exc)
            return False, str(exc)

    def _tie_breaker(self, config: Mapping[str, Any]) -> TieBreaker:
        if not config.get("enabled", False):
            return NoLLMFallback()
        if config.get("provider") != "openai":
            self.log.warning("unsupported LLM provider %s, fallback disabled", config.get("provider"))
            return NoLLMFallback()
        key = os.environ.get(str(config.get("api_key_env", "OPENAI_API_KEY")), "").strip()
        if not key:
            self.log.warning("LLM fallback requested but API key is missing, fallback disabled")
            return NoLLMFallback()
        return OpenAIResponsesTieBreaker(config, key)

    def _install_signal_handlers(self) -> None:
        def stop_handler(*_: object) -> None:
            self.stop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, stop_handler)
            except ValueError:
                pass
