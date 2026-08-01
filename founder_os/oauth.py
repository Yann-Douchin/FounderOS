"""Loopback OAuth authorization for Google and Linear."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets as secure_random
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from founder_os.connectors.http_client import request_json
from founder_os.secrets import SecretStore


GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
_MACHINE_ERROR_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class OAuthFlowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthResult:
    provider: str
    accounts: tuple[str, ...]
    scope: str = ""


def authorize_google(
    client_json: str | Path,
    store: SecretStore,
    *,
    timeout_seconds: float = 240,
    browser_opener: Callable[[str], Any] = webbrowser.open,
    token_request: Callable[..., dict[str, Any]] = request_json,
) -> OAuthResult:
    client = _load_google_client(client_json)
    client_id = str(client["client_id"])
    client_secret = str(client.get("client_secret") or "")
    _require_store_accounts(
        store,
        (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_REFRESH_TOKEN",
            *(("GOOGLE_CLIENT_SECRET",) if client_secret else ()),
        ),
    )
    verifier, challenge = _pkce_pair()
    with _CallbackServer(port=0) as callback:
        redirect_uri = callback.redirect_uri
        state = secure_random.token_urlsafe(32)
        authorization_url = _authorization_url(
            "https://accounts.google.com/o/oauth2/v2/auth",
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent select_account",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        _open_authorization_url(authorization_url, browser_opener)
        code = callback.wait_for_code(state, timeout_seconds=timeout_seconds)
    form = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    payload = token_request(
        "https://oauth2.googleapis.com/token",
        method="POST",
        form=form,
        timeout=15,
        retries=0,
    )
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OAuthFlowError(
            "Google did not return a refresh token; revoke the previous grant and authorize again"
        )
    granted_scope = _scope_text(payload.get("scope"))
    if granted_scope and not set(GOOGLE_SCOPES).issubset(set(granted_scope.split())):
        raise OAuthFlowError("Google did not grant every requested read-only scope")
    store.set("GOOGLE_CLIENT_ID", client_id)
    accounts = ["GOOGLE_CLIENT_ID"]
    if client_secret:
        store.set("GOOGLE_CLIENT_SECRET", client_secret)
        accounts.append("GOOGLE_CLIENT_SECRET")
    store.set("GOOGLE_REFRESH_TOKEN", refresh_token)
    accounts.append("GOOGLE_REFRESH_TOKEN")
    return OAuthResult(
        provider="google",
        accounts=tuple(accounts),
        scope=granted_scope or " ".join(GOOGLE_SCOPES),
    )


def authorize_linear(
    client_id: str,
    store: SecretStore,
    *,
    callback_port: int = 8766,
    timeout_seconds: float = 240,
    browser_opener: Callable[[str], Any] = webbrowser.open,
    token_request: Callable[..., dict[str, Any]] = request_json,
) -> OAuthResult:
    client_id = str(client_id).strip()
    if not client_id:
        raise OAuthFlowError("Linear OAuth client id is required")
    _require_store_accounts(store, ("LINEAR_CLIENT_ID", "LINEAR_REFRESH_TOKEN"))
    verifier, challenge = _pkce_pair()
    with _CallbackServer(port=callback_port) as callback:
        redirect_uri = callback.redirect_uri
        state = secure_random.token_urlsafe(32)
        authorization_url = _authorization_url(
            "https://linear.app/oauth/authorize",
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "read",
                "state": state,
                "actor": "user",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        _open_authorization_url(authorization_url, browser_opener)
        code = callback.wait_for_code(state, timeout_seconds=timeout_seconds)
    payload = token_request(
        "https://api.linear.app/oauth/token",
        method="POST",
        form={
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
        },
        timeout=15,
        retries=0,
    )
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OAuthFlowError("Linear did not return a refresh token")
    granted_scope = _scope_text(payload.get("scope"))
    if granted_scope and "read" not in set(granted_scope.split()):
        raise OAuthFlowError("Linear did not grant the requested read scope")
    store.set("LINEAR_CLIENT_ID", client_id)
    store.set("LINEAR_REFRESH_TOKEN", refresh_token)
    return OAuthResult(
        provider="linear",
        accounts=("LINEAR_CLIENT_ID", "LINEAR_REFRESH_TOKEN"),
        scope=granted_scope or "read",
    )


class _CallbackServer:
    def __init__(self, *, port: int) -> None:
        if not 0 <= int(port) <= 65535:
            raise OAuthFlowError("OAuth callback port is invalid")
        self.result: dict[str, str] = {}
        result = self.result

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if len(self.path) > 8192:
                    self.send_error(414)
                    return
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != "/oauth/callback":
                    self.send_error(404)
                    return
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                for key in ("code", "state", "error"):
                    values = query.get(key)
                    if values:
                        result[key] = str(values[0])[:4096]
                success = bool(result.get("code") and not result.get("error"))
                title = "FounderOS authorization complete" if success else "FounderOS authorization failed"
                body = (
                    "Authorization received. You can close this tab."
                    if success
                    else "Authorization was not completed. Return to the terminal for details."
                )
                encoded = (
                    "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
                    + title
                    + "</title></head><body><h1>"
                    + title
                    + "</h1><p>"
                    + body
                    + "</p></body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                return None

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
        except OSError as exc:
            raise OAuthFlowError(f"cannot bind OAuth callback on 127.0.0.1:{port}: {exc}") from exc
        self.server.daemon_threads = True

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/oauth/callback"

    def __enter__(self) -> "_CallbackServer":
        return self

    def __exit__(self, *_: object) -> None:
        self.server.server_close()

    def wait_for_code(self, expected_state: str, *, timeout_seconds: float) -> str:
        deadline = time.monotonic() + max(10.0, float(timeout_seconds))
        while time.monotonic() < deadline and not self.result:
            self.server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
            self.server.handle_request()
        if not self.result:
            raise OAuthFlowError("OAuth authorization timed out")
        if self.result.get("error"):
            error_code = self.result["error"]
            if not _MACHINE_ERROR_PATTERN.fullmatch(error_code):
                error_code = "authorization_error"
            raise OAuthFlowError(f"OAuth authorization was denied: {error_code}")
        received_state = self.result.get("state", "")
        if not received_state or not hmac.compare_digest(received_state, expected_state):
            raise OAuthFlowError("OAuth callback state did not match")
        code = self.result.get("code", "")
        if not code:
            raise OAuthFlowError("OAuth callback did not contain an authorization code")
        return code


def _load_google_client(path: str | Path) -> Mapping[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OAuthFlowError(f"Google OAuth client file was not found: {source}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthFlowError(f"Google OAuth client file is invalid: {source}") from exc
    if not isinstance(payload, Mapping):
        raise OAuthFlowError("Google OAuth client file must contain an object")
    client = payload.get("installed")
    if not isinstance(client, Mapping) or not str(client.get("client_id") or "").strip():
        raise OAuthFlowError("Google OAuth client file must contain an installed desktop client")
    return client


def _pkce_pair() -> tuple[str, str]:
    verifier = secure_random.token_urlsafe(64).rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _authorization_url(endpoint: str, parameters: Mapping[str, Any]) -> str:
    return endpoint + "?" + urllib.parse.urlencode(parameters)


def _open_authorization_url(url: str, opener: Callable[[str], Any]) -> None:
    try:
        opened = opener(url)
    except Exception as exc:
        raise OAuthFlowError(f"cannot open the authorization URL: {exc}") from exc
    if opened is False:
        raise OAuthFlowError(f"browser did not open; visit this URL manually: {url}")


def _scope_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _require_store_accounts(store: SecretStore, accounts: tuple[str, ...]) -> None:
    blocked = [account for account in accounts if not store.allows(account)]
    if blocked:
        raise OAuthFlowError("OAuth credential accounts are not allowlisted: " + ", ".join(blocked))
