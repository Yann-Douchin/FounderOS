"""Optional emulator input adapter for approval buttons."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any


InputCallback = Callable[[str], Any]


class EmulatorInputListener:
    """Listen to the emulator SSE stream without changing the hardware API path."""

    def __init__(
        self,
        url: str,
        callback: InputCallback,
        *,
        reconnect_seconds: float = 1.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.url = url
        self.callback = callback
        self.reconnect_seconds = max(0.1, float(reconnect_seconds))
        self.log = logger or logging.getLogger("founderos.input")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._response: Any = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="founderos-emulator-input", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        response, self._response = self._response, None
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            request = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(request, timeout=5.0) as response:
                    self._response = response
                    for event_name, payload in parse_sse(response):
                        if self._stop.is_set():
                            break
                        if event_name != "input" or not isinstance(payload, dict):
                            continue
                        key = str(payload.get("key", "")).strip().lower()
                        if key:
                            self.callback(key)
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                if not self._stop.is_set():
                    self.log.debug("emulator input stream unavailable: %s", exc)
            finally:
                self._response = None
            self._stop.wait(self.reconnect_seconds)


def parse_sse(lines: Iterable[bytes]) -> Iterator[tuple[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = text
                yield event_name, payload
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
