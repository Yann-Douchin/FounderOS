"""Minimal JSON HTTP client for read-only connector polling."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from founder_os.connectors.base import ConnectorError


def request_json(
    url: str,
    *,
    method: str = "GET",
    query: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    if query:
        encoded = urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
        url += ("&" if "?" in url else "?") + encoded
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ConnectorError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConnectorError(f"{method} {url} failed: {exc.reason}") from exc
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ConnectorError(f"{method} {url} returned a non-object JSON response")
    return result
