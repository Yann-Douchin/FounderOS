"""Exact HTTP transport used by FounderOS against emulator or real hardware."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol, Sequence


class DisplayError(RuntimeError):
    pass


class DisplayConflict(DisplayError):
    pass


class Display(Protocol):
    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None: ...
    def clear(self) -> None: ...


class BusyBarDisplay:
    def __init__(
        self,
        host: str = "127.0.0.1:8080",
        *,
        application_name: str = "founderos",
        priority: int = 90,
        timeout: float = 3.0,
    ) -> None:
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.base_url = host.rstrip("/")
        self.application_name = application_name
        self.priority = int(priority)
        self.timeout = timeout

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        self._request(
            "POST",
            "/api/display/draw",
            body={
                "application_name": self.application_name,
                "priority": self.priority,
                "elements": list(elements),
            },
        )

    def clear(self) -> None:
        self._request(
            "DELETE",
            "/api/display/draw",
            query={"application_name": self.application_name},
        )

    def version(self) -> str:
        payload = self._request("GET", "/api/version")
        return str(payload.get("api_semver", "unknown"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            error_type = DisplayConflict if exc.code == 409 else DisplayError
            raise error_type(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise DisplayError(f"{method} {path} failed: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DisplayError(f"{method} {path} returned invalid JSON") from exc
        return result if isinstance(result, dict) else {}


class RecordingDisplay:
    """In-memory display used by tests and dry runs."""

    def __init__(self) -> None:
        self.frames: list[list[dict[str, Any]]] = []
        self.clear_count = 0

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        self.frames.append([dict(element) for element in elements])

    def clear(self) -> None:
        self.clear_count += 1
