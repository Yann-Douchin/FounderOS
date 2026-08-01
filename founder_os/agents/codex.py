"""Official Codex app-server client for live ChatGPT rate limits."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from founder_os.agents.bridge import AgentBridgeError
from founder_os.models import parse_datetime, utc_now


class CodexAppServerError(AgentBridgeError):
    pass


class CodexAppServerClient:
    """Small synchronous JSONL client that reuses the user's Codex login."""

    def __init__(self, binary: str = "", *, timeout_seconds: float = 5.0) -> None:
        self.binary = resolve_codex_binary(binary)
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def read_rate_limits(self) -> dict[str, Any]:
        with self._lock:
            result = self._request("account/rateLimits/read")
        return result

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        try:
            self._process = subprocess.Popen(
                [self.binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexAppServerError(f"cannot start Codex app-server: {exc}") from exc
        self._send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "founderos_busybar",
                        "title": "FounderOS for BUSY Bar",
                        "version": "0.1.0",
                    }
                },
            }
        )
        self._wait_for(0)
        self._send({"method": "initialized", "params": {}})

    def _request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._start()
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        try:
            self._send(message)
            return self._wait_for(request_id)
        except CodexAppServerError:
            self.close()
            raise

    def _send(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("Codex app-server input closed") from exc

    def _wait_for(self, request_id: int) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise CodexAppServerError("Codex app-server output is unavailable")
        deadline = time.monotonic() + self.timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise CodexAppServerError(
                        f"Codex app-server exited with status {process.returncode}"
                    )
                ready = selector.select(max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                line = process.stdout.readline()
                if not line:
                    raise CodexAppServerError("Codex app-server closed its output")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexAppServerError("Codex app-server returned invalid JSON") from exc
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                error = message.get("error")
                if isinstance(error, Mapping):
                    raise CodexAppServerError(
                        str(error.get("message") or "Codex app-server request failed")
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexAppServerError("Codex app-server response has no result object")
                return result
        finally:
            selector.close()
        raise CodexAppServerError("Codex app-server request timed out")


def resolve_codex_binary(configured: str = "") -> str:
    candidates = (
        configured.strip(),
        os.environ.get("FOUNDEROS_CODEX_BINARY", "").strip(),
        shutil.which("codex") or "",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise CodexAppServerError(
        "Codex executable not found; set FOUNDEROS_CODEX_BINARY or usage.codex_binary"
    )


def normalize_rate_limits(
    result: Mapping[str, Any],
    *,
    now: datetime | None = None,
    ttl_seconds: float = 120,
) -> dict[str, Any] | None:
    """Convert account/rateLimits/read into the shared two-window snapshot."""
    now = now or utc_now()
    buckets = result.get("rateLimitsByLimitId")
    bucket: Mapping[str, Any] | None = None
    if isinstance(buckets, Mapping):
        preferred = buckets.get("codex")
        if isinstance(preferred, Mapping):
            bucket = preferred
        else:
            bucket = next((item for item in buckets.values() if isinstance(item, Mapping)), None)
    if bucket is None and isinstance(result.get("rateLimits"), Mapping):
        bucket = result["rateLimits"]
    if bucket is None:
        return None
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        item = bucket.get(key)
        if not isinstance(item, Mapping):
            continue
        try:
            used_percent = max(0.0, min(100.0, float(item.get("usedPercent"))))
            duration_minutes = max(1, int(item.get("windowDurationMins")))
        except (TypeError, ValueError):
            continue
        resets_at = _unix_datetime(item.get("resetsAt"))
        windows.append(
            {
                "label": _window_label(duration_minutes),
                "used_percent": round(used_percent, 1),
                "resets_at": resets_at.isoformat() if resets_at else None,
            }
        )
    if not windows:
        return None
    return {
        "schema_version": 1,
        "provider": "chatgpt_codex",
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=max(1.0, float(ttl_seconds)))).isoformat(),
        "plan_type": str(bucket.get("planType") or ""),
        "windows": windows[:2],
    }


def _window_label(minutes: int) -> str:
    if minutes >= 7 * 24 * 60 and minutes % (7 * 24 * 60) == 0:
        weeks = minutes // (7 * 24 * 60)
        return "SEM" if weeks == 1 else f"{weeks}SEM"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}H"
    return f"{minutes}M"


def _unix_datetime(value: Any) -> datetime | None:
    try:
        return parse_datetime(float(value))
    except (TypeError, ValueError):
        return None
