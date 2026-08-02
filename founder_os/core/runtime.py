"""FounderOS runtime: poll, rank, select one event, and render one frame."""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from founder_os.actions import ActionOutbox
from founder_os.automation import BusyBarMatterTarget, CalendarBusyAutomation
from founder_os.closure import ClosureEngine, ObligationLedger
from founder_os.config import load_config
from founder_os.connectors.registry import build_connectors
from founder_os.core.event_bus import EventBus
from founder_os.core.priority_engine import PriorityEngine
from founder_os.core.scheduler import Scheduler
from founder_os.display.busybar import BusyBarDisplay, Display, DisplayConflict, DisplayError
from founder_os.display.layouts import event_layout, idle_layout
from founder_os.health import HealthReporter
from founder_os.interaction import EmulatorInputListener, InputEvent, SignedInputListener
from founder_os.models import RankedEvent, utc_now
from founder_os.paths import state_root
from founder_os.ranking.deterministic import DeterministicRanker
from founder_os.ranking.llm import NoLLMFallback, OpenAIResponsesTieBreaker, TieBreaker
from founder_os.ranking.memory import RankingMemory
from founder_os.secrets import build_secret_resolver


@dataclass(slots=True)
class RuntimeState:
    selected: RankedEvent | None
    event_count: int
    connector_counts: Mapping[str, int]
    connector_health: Mapping[str, Mapping[str, Any]]
    automation_health: Mapping[str, Mapping[str, Any]]
    displayed: bool
    display_error: str = ""


class FounderOSRuntime:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        display: Display | None = None,
        busy_indicator: CalendarBusyAutomation | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.log = logger or logging.getLogger("founderos")
        self._state_lock = RLock()
        self.secrets = build_secret_resolver(config.get("secrets"))
        runtime_config = config["runtime"]
        operations_config = config["operations"]
        display_config = config["display"]
        ranking_config = config["ranking"]
        self.bus = EventBus(default_ttl_minutes=float(runtime_config["event_ttl_minutes"]))
        self.memory = RankingMemory(
            Path(str(config["memory"]["path"])),
            retention_days=float(config["memory"]["retention_days"]),
            max_entries=int(config["memory"]["max_entries"]),
        )
        closure_config = dict(config["closure"])
        memory_parent = Path(str(config["memory"]["path"])).expanduser().resolve().parent
        ledger_path = Path(str(closure_config["ledger_path"])).expanduser().resolve()
        default_ledger_path = (state_root() / "obligations.sqlite3").resolve()
        if memory_parent != ledger_path.parent and ledger_path == default_ledger_path:
            closure_config["ledger_path"] = str(memory_parent / "obligations.sqlite3")
            closure_config["snapshot_path"] = str(memory_parent / "obligations.json")
        closure_config["timezone"] = str(closure_config.get("timezone") or runtime_config["timezone"])
        self.closure_engine = (
            ClosureEngine(
                closure_config,
                ObligationLedger(
                    closure_config["ledger_path"],
                    audit_max_entries=closure_config.get("audit_max_entries", 100_000),
                ),
            )
            if bool(closure_config["enabled"])
            else None
        )
        self.rank_raw_events = bool(closure_config.get("rank_raw_events", False))
        self.connectors = build_connectors(config["connectors"], secrets=self.secrets)
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
            api_token = self.secrets.get(api_token_env) if api_token_env else ""
            display = BusyBarDisplay(
                str(display_config["host"]),
                application_name=str(display_config["application_name"]),
                priority=int(display_config["device_priority"]),
                timeout=float(display_config["request_timeout_seconds"]),
                api_token=api_token,
                api_semver=str(display_config["api_semver"]),
                text_rendering=str(display_config["text_rendering"]),
                font_atlas_path=str(Path(__file__).resolve().parents[2] / "public" / "fonts" / "font-atlas.json"),
            )
        self.display = display
        indicator_config = config["automations"]["calendar_busy_indicator"]
        if busy_indicator is not None:
            self.calendar_busy_automation = busy_indicator
        elif bool(indicator_config["enabled"]):
            indicator_host = str(indicator_config.get("host") or display_config["host"])
            indicator_token_env = str(
                indicator_config.get("api_token_env") or display_config.get("api_token_env") or ""
            ).strip()
            indicator_token = self.secrets.get(indicator_token_env) if indicator_token_env else ""
            indicator_client = BusyBarDisplay(
                indicator_host,
                application_name="founderos-calendar-busy",
                priority=1,
                timeout=float(indicator_config["request_timeout_seconds"]),
                api_token=indicator_token,
                api_semver=str(indicator_config.get("api_semver") or display_config["api_semver"]),
                text_rendering="native",
            )
            self.calendar_busy_automation = CalendarBusyAutomation(
                BusyBarMatterTarget(
                    indicator_client,
                    require_pairing=bool(indicator_config["require_pairing"]),
                ),
                include_all_day=bool(indicator_config["include_all_day"]),
                include_tentative=bool(indicator_config["include_tentative"]),
                off_delay_seconds=float(indicator_config["off_delay_seconds"]),
                verify_interval_seconds=float(indicator_config["verify_interval_seconds"]),
                retry_seconds=float(indicator_config["retry_seconds"]),
                retry_max_seconds=float(indicator_config["retry_max_seconds"]),
                force_wait_seconds=float(indicator_config["force_wait_seconds"]),
            )
        else:
            self.calendar_busy_automation = None
        self.validate_display_on_start = bool(display_config["validate_on_start"])
        self.expected_api_semver = str(display_config["api_semver"])
        self.tick_seconds = float(runtime_config["tick_seconds"])
        self.refresh_seconds = float(runtime_config["refresh_seconds"])
        self.min_hold_seconds = float(display_config["min_hold_seconds"])
        self.show_idle = bool(display_config["show_idle"])
        self.display_lease_seconds = float(display_config["lease_seconds"])
        self.display_lease_refresh_ratio = float(display_config["lease_refresh_ratio"])
        self.display_retry_seconds = float(display_config["conflict_retry_seconds"])
        self.display_retry_max_seconds = float(display_config["conflict_retry_max_seconds"])
        self.clear_on_shutdown = bool(display_config["clear_on_shutdown"])
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
                secret = self.secrets.get(secret_env)
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
        self._last_frame_elements: dict[str, dict[str, Any]] = {}
        self._display_lease_until = 0.0
        self._display_lease_renew_at = 0.0
        self._display_retry_at = 0.0
        self._display_retry_count = 0
        self._display_retry_event_id: str | None = None
        self._last_display_error = ""
        self._needs_full_redraw = False
        self._display_initialized = not isinstance(self.display, BusyBarDisplay)
        self._displayed_event_id: str | None = None
        self._stop = False
        self.health_reporter = (
            HealthReporter(
                str(operations_config["health_path"]),
                heartbeat_seconds=float(operations_config["heartbeat_seconds"]),
            )
            if bool(operations_config["health_enabled"])
            else None
        )

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
        source_events = self.bus.active(now)
        connector_health = self.scheduler.health_snapshot()
        automation_health: dict[str, Mapping[str, Any]] = {}
        if self.calendar_busy_automation is not None:
            automation_health[self.calendar_busy_automation.name] = self.calendar_busy_automation.reconcile(
                source_events,
                connector_health.get("calendar"),
                now,
                wait=force_poll,
            )
        if self.closure_engine:
            closure_events = self.closure_engine.reconcile(source_events, now)
            system_events = self.closure_engine.system_events(source_events)
            rankable_events = closure_events + system_events
            if self.rank_raw_events:
                rankable_events.extend(
                    event for event in source_events
                    if event.kind not in {"permission_request", "connector_health", "agent_usage"}
                )
        else:
            rankable_events = source_events
        with self._state_lock:
            candidate = self.rank_engine.select(rankable_events, now)
            candidate = self._respect_hold(
                candidate,
                now,
                active_event_ids={event.id for event in rankable_events},
            )
            displayed, error = self._render(candidate, now)
        state = RuntimeState(
            selected=candidate,
            event_count=len(rankable_events),
            connector_counts=connector_counts,
            connector_health=connector_health,
            automation_health=automation_health,
            displayed=displayed,
            display_error=error,
        )
        if self.health_reporter:
            self.health_reporter.publish(
                selected_source=candidate.event.source if candidate else "",
                event_count=state.event_count,
                connector_health=state.connector_health,
                automation_health=state.automation_health,
                displayed=state.displayed,
                display_error=state.display_error,
                now=now,
                force=force_poll,
            )
        return state

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
        rendering = self.display.resolve_text_rendering()
        self.log.info("BUSY Bar Unicode rendering resolved to %s", rendering)

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        if self.input_listener:
            self.input_listener.close()
        if self.calendar_busy_automation:
            self.calendar_busy_automation.close()
        self.scheduler.close()
        if self.closure_engine:
            self.closure_engine.close()
        if self.health_reporter:
            self.health_reporter.close()
        if self.clear_on_shutdown and isinstance(self.display, BusyBarDisplay):
            try:
                self.display.clear()
            except DisplayError as exc:
                self.log.debug("could not clear BUSY Bar during shutdown: %s", exc)

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
                obligation_id = str(event.metadata.get("obligation_id", ""))
                if (
                    obligation_id
                    and str(event.metadata.get("obligation_state", "")) == "ready"
                    and self.closure_engine is not None
                ):
                    self.closure_engine.ledger.transition(
                        obligation_id,
                        "closed",
                        reason="final close from trusted BUSY Bar input",
                    )
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

    def _respect_hold(
        self,
        candidate: RankedEvent | None,
        now: datetime,
        *,
        active_event_ids: set[str] | None = None,
    ) -> RankedEvent | None:
        monotonic = time.monotonic()
        if candidate and candidate.event.kind == "permission_request":
            return candidate
        active_ids = active_event_ids
        if active_ids is None:
            active_ids = {event.id for event in self.bus.active(now)}
        if (
            self._selected
            and candidate
            and candidate.event.id != self._selected.event.id
            and monotonic - self._selected_since < self.min_hold_seconds
            and self._selected.event.id in active_ids
        ):
            return self._selected
        return candidate

    def _render(self, selected: RankedEvent | None, now: datetime) -> tuple[bool, str]:
        monotonic = time.monotonic()
        event_id = selected.event.id if selected else None
        changed = event_id != (self._selected.event.id if self._selected else None)
        content_changed = bool(
            selected is not None
            and self._selected is not None
            and selected.event != self._selected.event
        )
        refresh_due = monotonic - self._last_draw_at >= self.refresh_seconds
        lease_refresh_due = bool(
            self._display_lease_renew_at
            and monotonic >= self._display_lease_renew_at
        )
        icon_frame = (
            int(monotonic / self.icon_frame_seconds)
            if selected is not None and self.content_icon_enabled
            else None
        )
        icon_changed = icon_frame != self._last_icon_frame
        if (
            not changed
            and not content_changed
            and not refresh_due
            and not icon_changed
            and not lease_refresh_due
        ):
            return False, ""
        if event_id != self._display_retry_event_id:
            self._reset_display_retry()
        if monotonic < self._display_retry_at:
            self._displayed_event_id = None
            return False, self._last_display_error
        self._displayed_event_id = None
        try:
            if not self._display_initialized:
                self.display.clear()
                self._display_initialized = True
            if selected:
                frame = event_layout(selected, now, icon_frame=icon_frame)
                signature = _frame_signature(frame)
                if self._last_frame_signature is not None and signature != self._last_frame_signature:
                    self.display.clear()
                    self._last_frame_signature = None
                    self._last_frame_elements = {}
                full_draw = changed or lease_refresh_due or self._needs_full_redraw or not self._last_frame_elements
                if full_draw:
                    lease_seconds = self.display_lease_seconds
                    if selected.event.expires_at is not None:
                        lease_seconds = min(
                            lease_seconds,
                            max(1.0, (selected.event.expires_at - now).total_seconds()),
                        )
                    self._display_lease_until = now.timestamp() + lease_seconds
                    self._display_lease_renew_at = (
                        monotonic + lease_seconds * self.display_lease_refresh_ratio
                    )
                    payload = _with_display_until(frame, self._display_lease_until)
                elif content_changed or icon_changed:
                    payload = _changed_elements(frame, self._last_frame_elements)
                    payload = _with_display_until(payload, self._display_lease_until)
                else:
                    payload = _refresh_probe(frame)
                    payload = _with_display_until(payload, self._display_lease_until)
                if payload:
                    self.display.draw(payload)
                self._last_frame_signature = signature
                self._last_frame_elements = _elements_by_id(frame)
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
                    self._last_frame_elements = {}
                self._display_lease_until = now.timestamp() + self.display_lease_seconds
                self._display_lease_renew_at = (
                    monotonic + self.display_lease_seconds * self.display_lease_refresh_ratio
                )
                self.display.draw(_with_display_until(frame, self._display_lease_until))
                self._last_frame_signature = signature
                self._last_frame_elements = _elements_by_id(frame)
                self._displayed_event_id = None
                self.memory.clear_current()
                self._idle_drawn = True
            else:
                self.display.clear()
                self._last_frame_signature = None
                self._last_frame_elements = {}
                self._display_lease_until = 0.0
                self._display_lease_renew_at = 0.0
                self._displayed_event_id = None
                self.memory.clear_current()
                self._idle_drawn = False
            self._selected = selected
            self._last_draw_at = monotonic
            self._last_icon_frame = icon_frame
            self._needs_full_redraw = False
            self._reset_display_retry()
            return True, ""
        except DisplayConflict as exc:
            self.log.info("BUSY Bar is owned by a higher-priority app: %s", exc)
            self._schedule_display_retry(event_id, monotonic, str(exc))
            return False, str(exc)
        except DisplayError as exc:
            self.log.warning("display unavailable: %s", exc)
            self._schedule_display_retry(event_id, monotonic, str(exc))
            return False, str(exc)

    def _schedule_display_retry(self, event_id: str | None, monotonic: float, error: str) -> None:
        delay = min(
            self.display_retry_max_seconds,
            self.display_retry_seconds * (2 ** min(self._display_retry_count, 8)),
        )
        self._display_retry_count += 1
        self._display_retry_at = monotonic + delay
        self._display_retry_event_id = event_id
        self._last_display_error = error
        self._needs_full_redraw = True
        self._displayed_event_id = None

    def _reset_display_retry(self) -> None:
        self._display_retry_at = 0.0
        self._display_retry_count = 0
        self._display_retry_event_id = None
        self._last_display_error = ""

    def _tie_breaker(self, config: Mapping[str, Any]) -> TieBreaker:
        if not config.get("enabled", False):
            return NoLLMFallback()
        if config.get("provider") != "openai":
            self.log.warning("unsupported LLM provider %s, fallback disabled", config.get("provider"))
            return NoLLMFallback()
        key = self.secrets.get(str(config.get("api_key_env", "OPENAI_API_KEY")))
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


def _elements_by_id(elements: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(element.get("id", index)): dict(element) for index, element in enumerate(elements)}


def _changed_elements(
    elements: list[Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        key = str(element.get("id", index))
        if previous.get(key) != element:
            changed.append(dict(element))
    return changed


def _refresh_probe(elements: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    for element in elements:
        if not element.get("scroll_rate"):
            return [dict(element)]
    return [dict(elements[0])] if elements else []


def _with_display_until(
    elements: list[Mapping[str, Any]],
    display_until: float,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for element in elements:
        leased = dict(element)
        leased["display_until"] = int(display_until)
        result.append(leased)
    return result
