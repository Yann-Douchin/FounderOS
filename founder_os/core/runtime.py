"""FounderOS runtime: poll, rank, select one event, and render one frame."""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from founder_os.actions import ActionOutbox
from founder_os.config import load_config
from founder_os.connectors.registry import build_connectors
from founder_os.core.event_bus import EventBus
from founder_os.core.priority_engine import PriorityEngine
from founder_os.core.scheduler import Scheduler
from founder_os.display.busybar import BusyBarDisplay, Display, DisplayConflict, DisplayError
from founder_os.display.layouts import event_layout, idle_layout
from founder_os.interaction import EmulatorInputListener, InputEvent, SignedInputListener
from founder_os.models import RankedEvent, utc_now
from founder_os.ranking.deterministic import DeterministicRanker
from founder_os.ranking.llm import NoLLMFallback, OpenAIResponsesTieBreaker, TieBreaker
from founder_os.ranking.memory import RankingMemory


@dataclass(slots=True)
class RuntimeState:
    selected: RankedEvent | None
    event_count: int
    connector_counts: Mapping[str, int]
    connector_health: Mapping[str, Mapping[str, Any]]
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
        self._state_lock = RLock()
        runtime_config = config["runtime"]
        display_config = config["display"]
        ranking_config = config["ranking"]
        self.bus = EventBus(default_ttl_minutes=float(runtime_config["event_ttl_minutes"]))
        self.memory = RankingMemory(
            Path(str(config["memory"]["path"])),
            retention_days=float(config["memory"]["retention_days"]),
            max_entries=int(config["memory"]["max_entries"]),
        )
        self.connectors = build_connectors(config["connectors"])
        self.scheduler = Scheduler(
            self.connectors,
            self.bus,
            self.log,
            max_workers=int(runtime_config["connector_workers"]),
            force_wait_seconds=float(runtime_config["force_poll_timeout_seconds"]),
        )
        self.rank_engine = PriorityEngine(
            DeterministicRanker(ranking_config, self.memory),
            self._tie_breaker(config["llm"]),
            tie_threshold=float(ranking_config["tie_threshold"]),
        )
        if display is None:
            api_token_env = str(display_config.get("api_token_env", "")).strip()
            api_token = os.environ.get(api_token_env, "").strip() if api_token_env else ""
            display = BusyBarDisplay(
                str(display_config["host"]),
                application_name=str(display_config["application_name"]),
                priority=int(display_config["device_priority"]),
                timeout=float(display_config["request_timeout_seconds"]),
                api_token=api_token,
                api_semver=str(display_config["api_semver"]),
            )
        self.display = display
        self.validate_display_on_start = bool(display_config["validate_on_start"])
        self.expected_api_semver = str(display_config["api_semver"])
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
        self.acknowledge_key = str(interaction_config["acknowledge_key"]).strip().lower()
        self.snooze_key = str(interaction_config["snooze_key"]).strip().lower()
        self.open_key = str(interaction_config["open_key"]).strip().lower()
        self.snooze_minutes = float(interaction_config["snooze_minutes"])
        self.action_outbox = ActionOutbox(
            str(interaction_config["action_outbox_path"]),
            max_pending=int(interaction_config["action_outbox_max_pending"]),
        )
        self.input_listener: EmulatorInputListener | SignedInputListener | None = None
        if bool(interaction_config["enabled"]):
            mode = str(interaction_config["mode"]).strip().lower()
            if mode == "emulator_sse":
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
            else:
                secret_env = str(interaction_config["secret_env"]).strip()
                secret = os.environ.get(secret_env, "").strip()
                if not secret:
                    raise ValueError(f"signed input is enabled but {secret_env} is missing")
                self.input_listener = SignedInputListener(
                    str(interaction_config["listen_host"]),
                    int(interaction_config["listen_port"]),
                    secret,
                    self.handle_input,
                    self._input_context,
                    max_clock_skew_seconds=float(interaction_config["max_clock_skew_seconds"]),
                    logger=self.log,
                )
        self._selected: RankedEvent | None = None
        self._selected_since = 0.0
        self._last_draw_at = 0.0
        self._idle_drawn = False
        self._last_icon_frame: int | None = None
        self._last_frame_signature: tuple[tuple[str, str], ...] | None = None
        self._displayed_event_id: str | None = None
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
        with self._state_lock:
            candidate = self.rank_engine.select(self.bus.active(now), now)
            candidate = self._respect_hold(candidate, now)
            displayed, error = self._render(candidate, now)
        return RuntimeState(
            selected=candidate,
            event_count=len(self.bus.active(now)),
            connector_counts=connector_counts,
            connector_health=self.scheduler.health_snapshot(),
            displayed=displayed,
            display_error=error,
        )

    def run(self) -> None:
        self._install_signal_handlers()
        self.log.info("FounderOS started with %d connector(s)", len(self.connectors))
        try:
            self.validate_display()
            if self.input_listener:
                self.input_listener.start()
                self.log.info("input adapter enabled: %s", type(self.input_listener).__name__)
            while not self._stop:
                self.tick()
                time.sleep(self.tick_seconds)
        finally:
            self.close()

    def validate_display(self) -> None:
        if not self.validate_display_on_start or not isinstance(self.display, BusyBarDisplay):
            return
        actual = self.display.version()
        expected_major = self.expected_api_semver.split(".", 1)[0]
        if actual.split(".", 1)[0] != expected_major:
            raise DisplayError(
                f"BUSY Bar API major {actual!r} is incompatible with expected {self.expected_api_semver!r}"
            )

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        if self.input_listener:
            self.input_listener.close()
        self.scheduler.close()

    def handle_input(self, value: str | InputEvent) -> str | None:
        input_event = value if isinstance(value, InputEvent) else InputEvent(key=str(value))
        if not input_event.trusted:
            self.log.warning("ignored untrusted input from %s", input_event.transport)
            return None
        with self._state_lock:
            selected = self._selected
            if (
                selected is None
                or self._displayed_event_id != selected.event.id
                or input_event.event_id != selected.event.id
            ):
                return None
            event = selected.event
            if event.kind == "connector_health":
                return None
            if event.kind == "permission_request":
                decision = (
                    "allow"
                    if input_event.key == self.allow_key
                    else "deny"
                    if input_event.key == self.deny_key
                    else None
                )
                request_id = str(event.metadata.get("request_id", ""))
                if decision is None or not request_id or input_event.request_id != request_id:
                    return None
                for connector in self.connectors:
                    decide = getattr(connector, "decide", None)
                    if connector.name != event.source or not callable(decide):
                        continue
                    if decide(
                        request_id,
                        decision,
                        input_key=input_event.key,
                        input_transport=input_event.transport,
                        input_nonce=input_event.nonce,
                    ):
                        self.bus.remove(event.id)
                        self.memory.clear_current()
                        self._selected = None
                        self._displayed_event_id = None
                        self.log.info("%s permission %s from trusted input", event.source, decision)
                        return decision
                return None
            if input_event.key == self.acknowledge_key:
                self.memory.acknowledge(event)
                self.bus.remove(event.id)
                self._selected = None
                self._displayed_event_id = None
                return "acknowledge"
            if input_event.key == self.snooze_key:
                self.memory.snooze(event, self.snooze_minutes)
                self._selected = None
                self._displayed_event_id = None
                return "snooze"
            if input_event.key == self.open_key and self.action_outbox.publish(event, "open"):
                self._displayed_event_id = None
                return "open"
            return None

    def _input_context(self) -> Mapping[str, Any]:
        with self._state_lock:
            if self._selected is None or self._displayed_event_id != self._selected.event.id:
                return {"event_id": "", "request_id": "", "kind": ""}
            event = self._selected.event
            return {
                "event_id": event.id,
                "request_id": str(event.metadata.get("request_id", "")),
                "kind": event.kind,
            }

    def _respect_hold(self, candidate: RankedEvent | None, now: datetime) -> RankedEvent | None:
        monotonic = time.monotonic()
        if candidate and candidate.event.kind == "permission_request":
            return candidate
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
        self._displayed_event_id = None
        try:
            if selected:
                frame = event_layout(selected, now, icon_frame=icon_frame)
                signature = _frame_signature(frame)
                if self._last_frame_signature is not None and signature != self._last_frame_signature:
                    self.display.clear()
                    self._last_frame_signature = None
                self.display.draw(frame)
                self._last_frame_signature = signature
                self._displayed_event_id = selected.event.id
                if changed:
                    self.memory.mark_displayed(selected.event, now)
                    self._selected_since = monotonic
                self._idle_drawn = False
            elif self.show_idle:
                frame = idle_layout()
                signature = _frame_signature(frame)
                if self._last_frame_signature is not None and signature != self._last_frame_signature:
                    self.display.clear()
                    self._last_frame_signature = None
                self.display.draw(frame)
                self._last_frame_signature = signature
                self._displayed_event_id = None
                self.memory.clear_current()
                self._idle_drawn = True
            else:
                self.display.clear()
                self._last_frame_signature = None
                self._displayed_event_id = None
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


def _frame_signature(elements: list[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((str(element.get("id", "")), str(element.get("type", ""))) for element in elements)
