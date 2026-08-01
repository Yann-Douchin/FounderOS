"""Exact HTTP transport used by FounderOS against emulator or real hardware."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol, Sequence


MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


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
        api_token: str = "",
        api_semver: str = "25.0.0",
    ) -> None:
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("BUSY Bar host must be an HTTP or HTTPS endpoint")
        if parsed.username or parsed.password:
            raise ValueError("BUSY Bar host must not contain credentials")
        self.base_url = host.rstrip("/")
        self.application_name = application_name
        self.priority = int(priority)
        self.timeout = timeout
        self.api_token = api_token.strip()
        self.api_semver = api_semver.strip()

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
        headers = {"X-API-Sem-Ver": self.api_semver}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.api_token:
            headers["X-API-Token"] = self.api_token
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DisplayError(f"{method} {path} exceeded the response limit")
        except urllib.error.HTTPError as exc:
            error_type = DisplayConflict if exc.code == 409 else DisplayError
            raise error_type(f"{method} {path} returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DisplayError(f"{method} {path} failed: {reason}") from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DisplayError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise DisplayError(f"{method} {path} returned a non-object JSON response")
        return result


class RecordingDisplay:
    """In-memory display used by tests and dry runs."""

    def __init__(self) -> None:
        self.frames: list[list[dict[str, Any]]] = []
        self.clear_count = 0
        self.operations: list[tuple[str, Any]] = []

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        frame = [dict(element) for element in elements]
        self.frames.append(frame)
        self.operations.append(("draw", frame))

    def clear(self) -> None:
        self.clear_count += 1
        self.operations.append(("clear", None))
