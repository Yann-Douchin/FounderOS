"""Private file protocol shared by Claude and ChatGPT/Codex integrations."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from founder_os.models import parse_datetime, utc_now


PROVIDERS = frozenset({"claude", "chatgpt_codex"})
DECISIONS = frozenset({"allow", "deny"})
SCHEMA_VERSION = 1
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*"
        r"(?:bearer\s+)?[^\s,;]+"
    ),
)


class AgentBridgeError(RuntimeError):
    pass


def normalize_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider not in PROVIDERS:
        raise AgentBridgeError(f"unsupported agent provider: {provider or '<empty>'}")
    return provider


def summarize_permission(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return a short tool label and a redacted, display-safe summary."""
    tool_name = _compact_text(payload.get("tool_name"), fallback="Action", limit=32)
    tool_input = payload.get("tool_input")
    values = tool_input if isinstance(tool_input, Mapping) else {}
    candidates = (
        values.get("description"),
        values.get("command"),
        values.get("file_path"),
        values.get("path"),
        values.get("query"),
    )
    summary = next(
        (_compact_text(value, fallback="", limit=96) for value in candidates if _is_text(value)),
        "",
    )
    if not summary:
        summary = f"Autoriser {tool_name} ?"
    for pattern in _SECRET_PATTERNS:
        summary = pattern.sub("[secret masqué]", summary)
    return tool_name, summary


class BridgeStore:
    """Atomic request, decision, and usage snapshots under one private root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def create_permission_request(
        self,
        provider: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        provider = normalize_provider(provider)
        now = now or utc_now()
        timeout_seconds = max(1.0, float(timeout_seconds))
        tool_name, summary = summarize_permission(payload)
        identity = json.dumps(
            {
                "provider": provider,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": tool_name,
                "summary": summary,
                "nonce": time.time_ns(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_id = sha256(identity.encode("utf-8")).hexdigest()[:24]
        record = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "provider": provider,
            "tool_name": tool_name,
            "summary": summary,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=timeout_seconds)).isoformat(),
        }
        _atomic_write_json(self._request_path(provider, request_id), record)
        return record

    def pending_requests(
        self,
        provider: str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        provider = normalize_provider(provider)
        now = now or utc_now()
        request_dir = self._provider_dir(provider) / "requests"
        if not request_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(request_dir.glob("*.json")):
            record = _read_json(path)
            if not record or record.get("provider") != provider:
                continue
            request_id = str(record.get("request_id", ""))
            if not request_id or self._decision_path(provider, request_id).exists():
                continue
            expires_at = _safe_datetime(record.get("expires_at"))
            if expires_at is None or expires_at <= now:
                continue
            records.append(record)
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records

    def decide(
        self,
        provider: str,
        request_id: str,
        decision: str,
        *,
        input_key: str = "",
        now: datetime | None = None,
    ) -> bool:
        provider = normalize_provider(provider)
        decision = decision.strip().lower()
        if decision not in DECISIONS:
            raise AgentBridgeError(f"unsupported decision: {decision or '<empty>'}")
        request_id = _safe_request_id(request_id)
        now = now or utc_now()
        request = _read_json(self._request_path(provider, request_id))
        if not request or request.get("provider") != provider:
            return False
        expires_at = _safe_datetime(request.get("expires_at"))
        if expires_at is None or expires_at <= now:
            return False
        record = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "provider": provider,
            "decision": decision,
            "input_key": input_key.strip(),
            "decided_at": now.isoformat(),
        }
        _atomic_write_json(self._decision_path(provider, request_id), record)
        return True

    def wait_for_decision(
        self,
        provider: str,
        request_id: str,
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.1,
    ) -> str | None:
        provider = normalize_provider(provider)
        request_id = _safe_request_id(request_id)
        request_path = self._request_path(provider, request_id)
        decision_path = self._decision_path(provider, request_id)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        try:
            while time.monotonic() < deadline:
                record = _read_json(decision_path)
                if record and record.get("request_id") == request_id:
                    decision = str(record.get("decision", "")).strip().lower()
                    if decision in DECISIONS:
                        return decision
                time.sleep(max(0.01, min(float(poll_seconds), deadline - time.monotonic())))
            return None
        finally:
            decision_path.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)

    def publish_usage(
        self,
        provider: str,
        windows: Iterable[Mapping[str, Any]],
        *,
        ttl_seconds: float = 900,
        now: datetime | None = None,
        plan_type: str = "",
    ) -> dict[str, Any]:
        provider = normalize_provider(provider)
        now = now or utc_now()
        normalized = [_normalize_window(item) for item in windows]
        if not normalized:
            raise AgentBridgeError("at least one usage window is required")
        record = {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=max(1.0, float(ttl_seconds)))).isoformat(),
            "plan_type": plan_type.strip(),
            "windows": normalized[:2],
        }
        _atomic_write_json(self._usage_path(provider), record)
        return record

    def read_usage(
        self,
        provider: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        provider = normalize_provider(provider)
        now = now or utc_now()
        record = _read_json(self._usage_path(provider))
        if not record or record.get("provider") != provider:
            return None
        expires_at = _safe_datetime(record.get("expires_at"))
        windows = record.get("windows")
        if expires_at is None or expires_at <= now or not isinstance(windows, list):
            return None
        try:
            normalized = [_normalize_window(item) for item in windows if isinstance(item, Mapping)]
        except AgentBridgeError:
            return None
        if not normalized:
            return None
        return {**record, "windows": normalized[:2]}

    def latest_request_id(self, provider: str, *, now: datetime | None = None) -> str | None:
        requests = self.pending_requests(provider, now=now)
        return str(requests[0]["request_id"]) if requests else None

    def _provider_dir(self, provider: str) -> Path:
        return self.root / normalize_provider(provider)

    def _request_path(self, provider: str, request_id: str) -> Path:
        return self._provider_dir(provider) / "requests" / f"{_safe_request_id(request_id)}.json"

    def _decision_path(self, provider: str, request_id: str) -> Path:
        return self._provider_dir(provider) / "decisions" / f"{_safe_request_id(request_id)}.json"

    def _usage_path(self, provider: str) -> Path:
        return self._provider_dir(provider) / "usage.json"


def _normalize_window(value: Mapping[str, Any]) -> dict[str, Any]:
    label = _compact_text(value.get("label"), fallback="", limit=8).upper()
    if not label:
        raise AgentBridgeError("usage window label is required")
    try:
        used_percent = float(value.get("used_percent"))
    except (TypeError, ValueError) as exc:
        raise AgentBridgeError("usage window used_percent must be numeric") from exc
    if not 0 <= used_percent <= 100:
        raise AgentBridgeError("usage window used_percent must be between 0 and 100")
    resets_at = _safe_datetime(value.get("resets_at"))
    return {
        "label": label,
        "used_percent": round(used_percent, 1),
        "resets_at": resets_at.isoformat() if resets_at else None,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_request_id(value: str) -> str:
    request_id = value.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{12,64}", request_id):
        raise AgentBridgeError("invalid request id")
    return request_id


def _safe_datetime(value: Any) -> datetime | None:
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _compact_text(value: Any, *, fallback: str, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text:
        return fallback
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"
