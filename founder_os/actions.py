"""Private, atomic outbox for actions delegated by trusted device input."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from founder_os.models import Event, utc_now
from founder_os.paths import ensure_private_directory


class ActionOutbox:
    def __init__(self, root: str | Path, *, max_pending: int = 1000) -> None:
        self.root = Path(root).expanduser()
        self.max_pending = max(1, int(max_pending))

    def publish(self, event: Event, action: str, *, now: datetime | None = None) -> Path | None:
        action = action.strip().lower()
        if action != "open" or not _is_safe_open_url(event.url):
            return None
        now = now or utc_now()
        record = {
            "schema_version": 1,
            "action": action,
            "event_id": event.id,
            "source": event.source,
            "url": event.url,
            "created_at": now.isoformat(),
        }
        directory = ensure_private_directory(self.root / "pending")
        if sum(1 for _ in directory.glob("*.json")) >= self.max_pending:
            return None
        filename = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(8)}.json"
        destination = directory / filename
        _atomic_create_json(destination, record)
        return destination


@dataclass(frozen=True, slots=True)
class ActionResult:
    record_name: str
    outcome: str
    event_id: str = ""
    source: str = ""


class ActionOutboxConsumer:
    """Open validated outbox URLs once, with private crash-aware auditing."""

    def __init__(
        self,
        root: str | Path,
        *,
        poll_seconds: float = 0.5,
        max_history: int = 1000,
        max_age_seconds: float = 300.0,
        browser_opener: Callable[[str], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.max_history = max(1, int(max_history))
        self.max_age_seconds = max(10.0, float(max_age_seconds))
        self.browser_opener = browser_opener or _open_with_macos
        self.log = logger or logging.getLogger("founderos.actions")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consume_lock = threading.Lock()
        self._recovered = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        with self._consume_lock:
            self._prepare()
            self._recover_inflight()
            self._recovered = True
        self._thread = threading.Thread(
            target=self._run,
            name="founderos-action-consumer",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def consume_pending(self, *, limit: int = 16) -> list[ActionResult]:
        results: list[ActionResult] = []
        with self._consume_lock:
            self._prepare()
            if not self._recovered:
                self._recover_inflight()
                self._recovered = True
            pending = self.root / "pending"
            for source_path in sorted(pending.glob("*.json"))[: max(1, int(limit))]:
                claimed = self.root / "processing" / source_path.name
                try:
                    os.replace(source_path, claimed)
                except FileNotFoundError:
                    continue
                results.append(self._consume_claimed(claimed))
            self._prune_history()
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                results = self.consume_pending()
                if results:
                    self.log.info("processed %d trusted open action(s)", len(results))
            except Exception:
                self.log.exception("trusted action consumer failed")
            self._stop.wait(self.poll_seconds)

    def _prepare(self) -> None:
        for name in ("pending", "processing", "processed", "rejected", "audit"):
            ensure_private_directory(self.root / name)

    def _recover_inflight(self) -> None:
        for path in sorted((self.root / "processing").glob("*.json")):
            audit = _read_json_if_safe(self.root / "audit" / path.name)
            outcome = str(audit.get("outcome", "")) if isinstance(audit, Mapping) else ""
            if outcome == "opened":
                destination = self.root / "processed" / path.name
            elif outcome.startswith(("failed", "rejected")):
                destination = self.root / "rejected" / path.name
            else:
                destination = self.root / "rejected" / path.name
                self._write_audit(
                    path.name,
                    {
                        "schema_version": 1,
                        "record_name": path.name,
                        "outcome": "indeterminate_after_restart",
                        "processed_at": utc_now().isoformat(),
                    },
                )
            os.replace(path, destination)

    def _consume_claimed(self, path: Path) -> ActionResult:
        try:
            record = _read_action_record(path, max_age_seconds=self.max_age_seconds)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            result = ActionResult(path.name, "rejected_invalid")
            self._write_audit(
                path.name,
                {
                    "schema_version": 1,
                    "record_name": path.name,
                    "outcome": result.outcome,
                    "processed_at": utc_now().isoformat(),
                },
            )
            os.replace(path, self.root / "rejected" / path.name)
            return result

        audit = {
            "schema_version": 1,
            "record_name": path.name,
            "action": "open",
            "event_id": record["event_id"],
            "source": record["source"],
            "url_sha256": hashlib.sha256(record["url"].encode("utf-8")).hexdigest(),
            "created_at": record["created_at"],
            "claimed_at": utc_now().isoformat(),
            "outcome": "claimed",
        }
        self._write_audit(path.name, audit)
        try:
            opened = bool(self.browser_opener(record["url"]))
            outcome = "opened" if opened else "failed_to_open"
        except Exception as exc:
            outcome = "failed_to_open"
            audit["failure_type"] = type(exc).__name__
        audit["outcome"] = outcome
        audit["processed_at"] = utc_now().isoformat()
        self._write_audit(path.name, audit)
        destination_name = "processed" if outcome == "opened" else "rejected"
        os.replace(path, self.root / destination_name / path.name)
        return ActionResult(
            path.name,
            outcome,
            event_id=record["event_id"],
            source=record["source"],
        )

    def _write_audit(self, name: str, payload: Mapping[str, Any]) -> None:
        _atomic_replace_json(self.root / "audit" / name, payload)

    def _prune_history(self) -> None:
        for name in ("processed", "rejected", "audit"):
            directory = self.root / name
            paths = sorted(
                directory.glob("*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for path in paths[self.max_history :]:
                path.unlink(missing_ok=True)


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def is_safe_open_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname == "localhost"


def _is_safe_open_url(value: str) -> bool:
    """Compatibility alias for the original private validator."""
    return is_safe_open_url(value)


def _read_action_record(path: Path, *, max_age_seconds: float) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= 8192:
            raise ValueError("action record must be a bounded regular file")
        if metadata.st_uid != os.getuid():
            raise ValueError("action record owner is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise ValueError("action record permissions are invalid")
        data = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(data) != metadata.st_size:
        raise ValueError("action record changed during validation")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported action record")
    action = payload.get("action")
    event_id = payload.get("event_id")
    source = payload.get("source")
    url = payload.get("url")
    created_at = payload.get("created_at")
    if action != "open":
        raise ValueError("unsupported action")
    if not all(isinstance(value, str) for value in (event_id, source, url, created_at)):
        raise ValueError("action record fields are invalid")
    if not event_id or len(event_id) > 256 or not source or len(source) > 96:
        raise ValueError("action record identifiers are invalid")
    if not is_safe_open_url(url):
        raise ValueError("action URL is unsafe")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("action timestamp is invalid") from exc
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("action timestamp must include a timezone")
    age_seconds = (utc_now() - created).total_seconds()
    if age_seconds < -30 or age_seconds > max_age_seconds:
        raise ValueError("action record is outside its accepted age window")
    return {
        "action": action,
        "event_id": event_id,
        "source": source,
        "url": url,
        "created_at": created_at,
    }


def _read_json_if_safe(path: Path) -> Mapping[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not 1 <= metadata.st_size <= 8192
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                return None
            data = os.read(descriptor, metadata.st_size + 1)
        finally:
            os.close(descriptor)
        if len(data) != metadata.st_size:
            return None
        payload = json.loads(data.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    directory = ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _open_with_macos(url: str) -> bool:
    if not is_safe_open_url(url):
        return False
    completed = subprocess.run(
        ["/usr/bin/open", url],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return completed.returncode == 0
