"""Input adapters with explicit trust and event binding."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import socket
import socketserver
import stat
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final


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
PresenceCallback = Callable[[Mapping[str, Any]], Mapping[str, Any]]
BRIDGE_VERSION = 2
BRIDGE_CAPABILITIES: Final[dict[str, str]] = {
    "open": "event.open",
    "snooze": "event.snooze",
    "acknowledge": "event.acknowledge",
    "allow": "permission.allow",
    "deny": "permission.deny",
    "presence_acquire": "presence.acquire",
    "presence_renew": "presence.renew",
    "presence_release": "presence.release",
    "presence_release_all": "presence.release_all",
}


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
        presence_callback: PresenceCallback | None = None,
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
        self.presence_callback = presence_callback
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
                self._send(
                    HTTPStatus.OK,
                    {"bridge_version": BRIDGE_VERSION, "context": context},
                )

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {"/input", "/presence/lease"}:
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
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_input", "detail": str(exc)})
                    return
                if self.path == "/presence/lease":
                    if listener.presence_callback is None:
                        self._send(HTTPStatus.CONFLICT, {"error": "presence_unavailable"})
                        return
                    try:
                        command = listener._validate_presence_payload(payload)
                        result = dict(listener.presence_callback(command))
                    except (TypeError, ValueError):
                        self._send(HTTPStatus.CONFLICT, {"error": "lease_rejected"})
                        return
                    except Exception:
                        listener.log.exception("signed presence callback failed")
                        self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "presence_failed"})
                        return
                    self._send(HTTPStatus.OK, {"result": "OK", "presence": result})
                    return
                try:
                    event = listener._validate_payload(payload)
                except (TypeError, ValueError) as exc:
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
        if set(payload) != {"key", "event_id", "request_id", "issued_at", "nonce"}:
            raise ValueError("input payload fields do not match the bridge contract")
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
        self._consume_nonce(issued_at, nonce)
        return InputEvent(
            key=key,
            event_id=event_id,
            request_id=request_id,
            trusted=True,
            transport="signed_http",
            issued_at=issued_at,
            nonce=nonce,
        )

    def _validate_presence_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("presence payload must be an object")
        action = str(payload.get("action", "")).strip().lower()
        lease_id = str(payload.get("lease_id", "")).strip()
        state = str(payload.get("state", "")).strip().lower()
        nonce = str(payload.get("nonce", "")).strip()
        issued_at_value = payload.get("issued_at")
        if action not in {"acquire", "renew", "release", "release_all"}:
            raise ValueError("presence action must be acquire, renew, release, or release_all")
        required_fields = {
            "acquire": {"action", "lease_id", "state", "ttl_seconds", "issued_at", "nonce"},
            "renew": {"action", "lease_id", "ttl_seconds", "issued_at", "nonce"},
            "release": {"action", "lease_id", "issued_at", "nonce"},
            "release_all": {"action", "issued_at", "nonce"},
        }[action]
        if set(payload) != required_fields:
            raise ValueError("presence payload fields do not match the requested action")
        if action != "release_all" and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", lease_id
        ):
            raise ValueError("lease_id is invalid")
        if not isinstance(issued_at_value, int) or isinstance(issued_at_value, bool):
            raise ValueError("issued_at must be an integer Unix timestamp")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
            raise ValueError("nonce must contain 16 to 128 URL-safe characters")
        command: dict[str, Any] = {
            "action": action,
            "issued_at": issued_at_value,
            "nonce": nonce,
        }
        if action != "release_all":
            command["lease_id"] = lease_id
        if action == "acquire":
            if state not in {"focus", "manual_call", "recording"}:
                raise ValueError("presence state is invalid")
            command["state"] = state
        elif state:
            raise ValueError("state is accepted only when acquiring a lease")
        if action in {"acquire", "renew"}:
            ttl = payload.get("ttl_seconds")
            if not isinstance(ttl, int) or isinstance(ttl, bool):
                raise ValueError("ttl_seconds must be an integer")
            command["ttl_seconds"] = ttl
        elif "ttl_seconds" in payload:
            raise ValueError("ttl_seconds is not accepted when releasing a lease")
        self._consume_nonce(issued_at_value, nonce)
        return command

    def _consume_nonce(self, issued_at: int, nonce: str) -> None:
        current_time = time.time()
        if abs(current_time - issued_at) > self.max_clock_skew_seconds:
            raise ValueError("input timestamp is outside the accepted clock window")
        with self._nonce_lock:
            cutoff = current_time - self.max_clock_skew_seconds * 2
            self._seen_nonces = {value: seen for value, seen in self._seen_nonces.items() if seen >= cutoff}
            if nonce in self._seen_nonces:
                raise ValueError("input nonce has already been used")
            self._seen_nonces[nonce] = current_time


class LocalInputListener:
    """Private Unix-domain bridge for same-account desktop integrations.

    The socket lives inside the private FounderOS state directory. Its parent
    must be owned by the current account with mode 0700, and the socket itself
    is created with mode 0600. The protocol stays request-bound and reuses the
    signed bridge payload validation, but no secret leaves the runtime process.
    """

    _MAX_REQUEST_BYTES = 4096
    _MAX_RESPONSE_BYTES = 65536

    def __init__(
        self,
        socket_path: str | Path,
        callback: InputCallback,
        context_provider: ContextProvider,
        *,
        presence_callback: PresenceCallback | None = None,
        allowed_keys: Iterable[str] = (),
        max_clock_skew_seconds: float = 30,
        logger: logging.Logger | None = None,
    ) -> None:
        self.socket_path = Path(socket_path).expanduser()
        if not self.socket_path.is_absolute():
            raise ValueError("local input socket path must be absolute")
        self.callback = callback
        self.context_provider = context_provider
        self.presence_callback = presence_callback
        self.allowed_keys = {
            str(value).strip().lower() for value in allowed_keys if str(value).strip()
        }
        if not self.allowed_keys:
            raise ValueError("local input listener requires an explicit key allowlist")
        self.log = logger or logging.getLogger("founderos.input")
        self._validator = SignedInputListener(
            "127.0.0.1",
            0,
            "local-socket-payload-validator-0001",
            callback,
            context_provider,
            presence_callback=presence_callback,
            max_clock_skew_seconds=max_clock_skew_seconds,
            logger=self.log,
        )
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._thread: threading.Thread | None = None
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._prepare_socket_path()
        listener = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                raw = self.rfile.readline(listener._MAX_REQUEST_BYTES + 1)
                if not raw or len(raw) > listener._MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                    self._send({"error": "invalid_length"})
                    return
                try:
                    request = json.loads(raw.decode("utf-8"))
                    response = listener._handle_request(request)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    response = {"error": "invalid_request"}
                except Exception:
                    listener.log.exception("local input request failed")
                    response = {"error": "request_failed"}
                self._send(response)

            def _send(self, payload: Mapping[str, Any]) -> None:
                data = (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                if len(data) > listener._MAX_RESPONSE_BYTES:
                    data = b'{"error":"response_too_large"}\n'
                self.wfile.write(data)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        try:
            self._server = Server(str(self.socket_path), Handler)
            created = self.socket_path.lstat()
            self._socket_identity = (created.st_dev, created.st_ino)
            os.chmod(self.socket_path, 0o600)
            facts = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(facts.st_mode)
                or facts.st_uid != os.getuid()
                or stat.S_IMODE(facts.st_mode) != 0o600
            ):
                raise OSError("local input socket permissions are invalid")
        except BaseException:
            self._close_server()
            self._unlink_owned_socket()
            raise
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="founderos-local-input",
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
        self._unlink_owned_socket()

    def _handle_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("version") != 1:
            raise ValueError("local input request version is invalid")
        operation = request.get("operation")
        if operation == "context":
            if set(request) != {"version", "operation"}:
                raise ValueError("context request fields are invalid")
            return {
                "bridge_version": BRIDGE_VERSION,
                "context": dict(self.context_provider()),
            }
        if operation == "input":
            if set(request) != {"version", "operation", "payload"}:
                raise ValueError("input request fields are invalid")
            validated = self._validator._validate_payload(request["payload"])
            if validated.key not in self.allowed_keys:
                raise ValueError("input key is not allowlisted")
            event = InputEvent(
                key=validated.key,
                event_id=validated.event_id,
                request_id=validated.request_id,
                trusted=True,
                transport="local_socket",
                issued_at=validated.issued_at,
                nonce=validated.nonce,
            )
            result = self.callback(event)
            if result is None:
                return {"error": "stale_or_inapplicable_context"}
            return {"result": "OK", "action": str(result)}
        if operation == "presence":
            if set(request) != {"version", "operation", "payload"}:
                raise ValueError("presence request fields are invalid")
            if self.presence_callback is None:
                return {"error": "presence_unavailable"}
            command = self._validator._validate_presence_payload(request["payload"])
            return {"result": "OK", "presence": dict(self.presence_callback(command))}
        raise ValueError("local input operation is invalid")

    def _prepare_socket_path(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        facts = parent.lstat()
        if (
            not stat.S_ISDIR(facts.st_mode)
            or facts.st_uid != os.getuid()
            or stat.S_IMODE(facts.st_mode) != 0o700
        ):
            raise OSError("local input socket parent must be private and account-owned")
        if not self.socket_path.exists() and not self.socket_path.is_symlink():
            return
        socket_facts = self.socket_path.lstat()
        if not stat.S_ISSOCK(socket_facts.st_mode) or socket_facts.st_uid != os.getuid():
            raise OSError("local input socket path is not an account-owned socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            self.socket_path.unlink(missing_ok=True)
        else:
            raise OSError("local input socket is already active")
        finally:
            probe.close()

    def _close_server(self) -> None:
        server, self._server = self._server, None
        if server:
            server.server_close()

    def _unlink_owned_socket(self) -> None:
        expected = self._socket_identity
        if expected is None:
            return
        try:
            facts = self.socket_path.lstat()
        except FileNotFoundError:
            self._socket_identity = None
            return
        actual = (facts.st_dev, facts.st_ino)
        if (
            actual == expected
            and stat.S_ISSOCK(facts.st_mode)
            and facts.st_uid == os.getuid()
        ):
            self.socket_path.unlink()
        self._socket_identity = None


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
