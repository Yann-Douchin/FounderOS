"""Bounded, retrying JSON HTTP transport for connector workers."""

from __future__ import annotations

import json
import ipaddress
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from founder_os.connectors.base import ConnectorError


DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class ConnectorHTTPError(ConnectorError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def request_json(
    url: str,
    *,
    method: str = "GET",
    query: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
    form: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
    retries: int = 2,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    deadline_monotonic: float | None = None,
    root: str = "object",
) -> Any:
    if body is not None and form is not None:
        raise ValueError("request body and form are mutually exclusive")
    _validate_url(url)
    if query:
        encoded = urllib.parse.urlencode(
            {key: value for key, value in query.items() if value is not None},
            doseq=True,
        )
        url += ("&" if "?" in url else "?") + encoded
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    elif form is not None:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        payload = None
        content_type = ""
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "FounderOS/1.0",
        **dict(headers or {}),
    }
    if content_type:
        request_headers["Content-Type"] = content_type
    parsed = urllib.parse.urlsplit(url)
    safe_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    attempts = max(1, int(retries) + 1)
    raw = b""
    response_limit = max(1024, int(max_response_bytes))
    for attempt in range(attempts):
        effective_timeout = max(0.1, float(timeout))
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} {safe_url} exceeded its deadline")
            effective_timeout = min(effective_timeout, remaining)
        request = urllib.request.Request(url, data=payload, method=method, headers=request_headers)
        try:
            with _OPENER.open(request, timeout=effective_timeout) as response:
                raw = response.read(response_limit + 1)
            if len(raw) > response_limit:
                raise ConnectorError(
                    f"{method} {safe_url} exceeded the {response_limit}-byte response limit"
                )
            break
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                _sleep_before_retry(
                    _retry_delay(exc.headers.get("Retry-After"), attempt),
                    deadline_monotonic,
                    method,
                    safe_url,
                )
                continue
            raise ConnectorHTTPError(
                f"{method} {safe_url} returned HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 < attempts:
                _sleep_before_retry(
                    _retry_delay(None, attempt),
                    deadline_monotonic,
                    method,
                    safe_url,
                )
                continue
            reason = getattr(exc, "reason", exc)
            raise ConnectorError(f"{method} {safe_url} failed: {reason}") from exc
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"{method} {safe_url} returned invalid JSON") from exc
    if root == "object" and not isinstance(result, dict):
        raise ConnectorError(f"{method} {safe_url} returned a non-object JSON response")
    if root == "array" and not isinstance(result, list):
        raise ConnectorError(f"{method} {safe_url} returned a non-array JSON response")
    if root not in {"object", "array", "any"}:
        raise ValueError("root must be object, array, or any")
    return result


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after:
        try:
            return max(0.0, min(2.0, float(retry_after)))
        except ValueError:
            pass
    return min(1.0, 0.2 * (2**attempt))


def _sleep_before_retry(
    delay: float,
    deadline_monotonic: float | None,
    method: str,
    safe_url: str,
) -> None:
    if deadline_monotonic is None:
        time.sleep(delay)
        return
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{method} {safe_url} exceeded its deadline")
    time.sleep(min(delay, remaining))


def _validate_url(value: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise ConnectorError("connector URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ConnectorError("connector URL must use HTTP or HTTPS and include a host")
    if parsed.username or parsed.password:
        raise ConnectorError("connector URL must not contain credentials")
    if parsed.scheme == "https":
        return
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback:
        raise ConnectorError("plain HTTP connector URLs are allowed only on loopback")
