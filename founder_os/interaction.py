"""Input adapters with explicit trust and event binding."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One input plus the exact selected event it is authorized to mutate."""

    key: str
    event_id: str = ""
    request_id: str = ""
    trusted: bool = False
    transport: str = "direct"
    issued_at: int | None = None
    nonce: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", self.key.strip().lower())
        object.__setattr__(self, "event_id", self.event_id.strip())
        object.__setattr__(self, "request_id", self.request_id.strip().lower())
        object.__setattr__(self, "transport", self.transport.strip().lower())


InputCallback = Callable[[InputEvent], Any]
ContextProvider = Callable[[], Mapping[str, Any]]


class EmulatorInputListener:
    """Observe emulator SSE input as untrusted development telemetry.

    The emulator exposes ``/api/input`` without authentication. Events from this
    adapter are therefore never marked trusted and cannot approve, acknowledge,
    snooze, or open anything in the production runtime.
    """

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
                            self.callback(InputEvent(key=key, trusted=False, transport="emulator_sse"))
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                if not self._stop.is_set():
                    self.log.debug("emulator input stream unavailable: %s", exc)
            finally:
                self._response = None
            self._stop.wait(self.reconnect_seconds)


class SignedInputListener:
    """Loopback HTTP bridge requiring HMAC and exact event context."""

    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        callback: InputCallback,
        context_provider: ContextProvider,
        *,
        max_clock_skew_seconds: float = 30,
        logger: logging.Logger | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("signed input listener must bind to loopback")
        secret_bytes = secret.encode("utf-8")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError("signed input secret must contain 32 to 256 URL-safe ASCII characters")
        self.host = host
        self.port = int(port)
        self.secret = secret_bytes
        self.callback = callback
        self.context_provider = context_provider
        self.max_clock_skew_seconds = max(1.0, float(max_clock_skew_seconds))
        self.log = logger or logging.getLogger("founderos.input")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._seen_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        if self._server:
            host, port = self._server.server_address[:2]
            return str(host), int(port)
        return self.host, self.port

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/context":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                authorization = self.headers.get("Authorization", "")
                expected = "Bearer " + listener.secret.decode("utf-8")
                if not hmac.compare_digest(authorization, expected):
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                try:
                    context = dict(listener.context_provider())
                except Exception:
                    listener.log.exception("signed input context provider failed")
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "context_unavailable"})
                    return
                self._send(HTTPStatus.OK, {"context": context})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/input":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 1 <= length <= 4096:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_length"})
                    return
                body = self.rfile.read(length)
                signature = self.headers.get("X-FounderOS-Signature", "")
                if not verify_signature(listener.secret, body, signature):
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid_signature"})
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                    event = listener._validate_payload(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_input", "detail": str(exc)})
                    return
                try:
                    result = listener.callback(event)
                except Exception:
                    listener.log.exception("signed input callback failed")
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "input_failed"})
                    return
                if result is None:
                    self._send(HTTPStatus.CONFLICT, {"error": "stale_or_inapplicable_context"})
                    return
                self._send(HTTPStatus.OK, {"result": "OK", "action": str(result)})

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _send(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="founderos-signed-input",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _validate_payload(self, payload: Any) -> InputEvent:
        if not isinstance(payload, dict):
            raise ValueError("input payload must be an object")
        key = str(payload.get("key", "")).strip().lower()
        event_id = str(payload.get("event_id", "")).strip()
        request_id = str(payload.get("request_id", "")).strip().lower()
        nonce = str(payload.get("nonce", "")).strip()
        issued_at_value = payload.get("issued_at")
        if not isinstance(issued_at_value, int) or isinstance(issued_at_value, bool):
            raise ValueError("issued_at must be an integer Unix timestamp")
        issued_at = issued_at_value
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", key):
            raise ValueError("key must contain 1 to 32 URL-safe lowercase characters")
        if not event_id or len(event_id) > 256:
            raise ValueError("event_id must contain 1 to 256 characters")
        if request_id and not re.fullmatch(r"[a-f0-9]{12,64}", request_id):
            raise ValueError("request_id is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
            raise ValueError("nonce must contain 16 to 128 URL-safe characters")
        current_time = time.time()
        if abs(current_time - issued_at) > self.max_clock_skew_seconds:
            raise ValueError("input timestamp is outside the accepted clock window")
        with self._nonce_lock:
            cutoff = current_time - self.max_clock_skew_seconds * 2
            self._seen_nonces = {value: seen for value, seen in self._seen_nonces.items() if seen >= cutoff}
            if nonce in self._seen_nonces:
                raise ValueError("input nonce has already been used")
            self._seen_nonces[nonce] = current_time
        return InputEvent(
            key=key,
            event_id=event_id,
            request_id=request_id,
            trusted=True,
            transport="signed_http",
            issued_at=issued_at,
            nonce=nonce,
        )


def encode_signed_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def signature_for(secret: str | bytes, body: bytes) -> str:
    key = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | bytes, body: bytes, signature: str) -> bool:
    expected = signature_for(secret, body)
    return hmac.compare_digest(signature.strip().lower(), expected)


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
