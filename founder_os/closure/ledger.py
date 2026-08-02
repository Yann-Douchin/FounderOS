"""Private SQLite obligation ledger with an append-only transition audit."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Mapping, Sequence

from founder_os.closure.models import ACTIVE_OBLIGATION_STATES, Obligation, Relationship
from founder_os.models import parse_datetime, utc_now
from founder_os.paths import ensure_private_directory


SCHEMA_VERSION = 2


class LedgerError(RuntimeError):
    pass


class ObligationLedger:
    def __init__(self, path: str | Path, *, audit_max_entries: int = 100_000) -> None:
        self.path = Path(path).expanduser().resolve()
        self.audit_max_entries = max(1_000, min(10_000_000, int(audit_max_entries)))
        ensure_private_directory(self.path.parent)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        self._configure()
        self._migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def get(self, obligation_id: str) -> Obligation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM obligations WHERE id = ?",
                (str(obligation_id),),
            ).fetchone()
        return self._decode_obligation(row["payload"]) if row else None

    def get_by_key(self, key: str) -> Obligation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM obligations WHERE obligation_key = ?",
                (str(key),),
            ).fetchone()
        return self._decode_obligation(row["payload"]) if row else None

    def for_event(self, event_id: str) -> Obligation | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT o.payload
                FROM observations AS s
                JOIN obligations AS o ON o.id = s.obligation_id
                WHERE s.event_id = ?
                """,
                (str(event_id),),
            ).fetchone()
        return self._decode_obligation(row["payload"]) if row else None

    def list(self, *, active_only: bool = False, limit: int = 5000) -> list[Obligation]:
        bounded = max(1, min(50_000, int(limit)))
        parameters: tuple[Any, ...] = ()
        query = "SELECT payload FROM obligations"
        if active_only:
            placeholders = ",".join("?" for _ in ACTIVE_OBLIGATION_STATES)
            query += f" WHERE state IN ({placeholders})"
            parameters = tuple(sorted(ACTIVE_OBLIGATION_STATES))
        query += " ORDER BY priority DESC, updated_at DESC, id ASC LIMIT ?"
        parameters += (bounded,)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._decode_obligation(row["payload"]) for row in rows]

    def upsert(self, obligation: Obligation, *, reason: str = "reconcile") -> bool:
        payload = self._encode(obligation.to_dict())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, payload FROM obligations WHERE id = ?",
                (obligation.id,),
            ).fetchone()
            if row and row["payload"] == payload:
                return False
            previous_state = str(row["state"]) if row else ""
            connection.execute(
                """
                INSERT INTO obligations(id, obligation_key, state, priority, updated_at, payload)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  obligation_key = excluded.obligation_key,
                  state = excluded.state,
                  priority = excluded.priority,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (
                    obligation.id,
                    obligation.key,
                    obligation.state,
                    obligation.priority,
                    obligation.updated_at.isoformat(),
                    payload,
                ),
            )
            if previous_state != obligation.state:
                self._record_transition(
                    connection,
                    obligation.id,
                    previous_state,
                    obligation.state,
                    reason,
                    obligation.updated_at,
                )
            self._record_audit(
                connection,
                "obligation",
                obligation.id,
                "created" if row is None else "state_changed" if previous_state != obligation.state else "updated",
                reason,
                obligation.updated_at,
                payload,
            )
        return True

    def transition(
        self,
        obligation_id: str,
        state: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> Obligation:
        current = self.get(obligation_id)
        if current is None:
            raise LedgerError(f"unknown obligation: {obligation_id}")
        changed = current.with_state(state, now=now or utc_now(), reason=reason)
        self.upsert(changed, reason=reason)
        return changed

    def correct(
        self,
        obligation_id: str,
        changes: Mapping[str, Any],
        *,
        actor: str = "operator",
        now: datetime | None = None,
    ) -> Obligation:
        current = self.get(obligation_id)
        if current is None:
            raise LedgerError(f"unknown obligation: {obligation_id}")
        allowed = {
            "title", "state", "owner", "counterparty", "next_actor", "project",
            "relationship_key", "due_at", "resume_after", "priority", "url",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise LedgerError("unsupported correction fields: " + ", ".join(unknown))
        payload = current.to_dict()
        for key, value in changes.items():
            payload[key] = value
        timestamp = now or utc_now()
        payload["updated_at"] = timestamp.isoformat()
        metadata = dict(payload.get("metadata") or {})
        previous_manual = metadata.get("manual_correction")
        previous_fields = (
            previous_manual.get("fields", [])
            if isinstance(previous_manual, Mapping)
            else []
        )
        metadata["manual_correction"] = {
            "actor": str(actor)[:160],
            "at": timestamp.isoformat(),
            "fields": sorted({*(str(value) for value in previous_fields), *changes}),
        }
        if "state" in changes and str(changes["state"]) in {"closed", "cancelled"}:
            payload["closed_at"] = timestamp.isoformat()
        elif "state" in changes:
            payload["closed_at"] = None
        payload["metadata"] = metadata
        corrected = Obligation.from_mapping(payload)
        self.upsert(corrected, reason=f"manual correction by {actor}")
        return corrected

    def correct_metadata(
        self,
        obligation_id: str,
        changes: Mapping[str, Any],
        *,
        actor: str = "operator",
        now: datetime | None = None,
    ) -> Obligation:
        current = self.get(obligation_id)
        if current is None:
            raise LedgerError(f"unknown obligation: {obligation_id}")
        allowed = {"delegate", "next_action", "relationship_stage"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise LedgerError("unsupported metadata correction fields: " + ", ".join(unknown))
        timestamp = now or utc_now()
        metadata = dict(current.metadata)
        for key, value in changes.items():
            normalized = " ".join(str(value or "").split())[:512]
            if normalized:
                metadata[key] = normalized
            else:
                metadata.pop(key, None)
        audit = dict(metadata.get("manual_metadata") or {})
        for key in changes:
            audit[key] = {"actor": str(actor)[:160], "at": timestamp.isoformat()}
        metadata["manual_metadata"] = audit
        corrected = Obligation.from_mapping({
            **current.to_dict(),
            "updated_at": timestamp.isoformat(),
            "metadata": metadata,
        })
        self.upsert(corrected, reason=f"manual metadata correction by {actor}")
        return corrected

    def bind_observation(
        self,
        event_id: str,
        obligation_id: str,
        *,
        source: str,
        fingerprint: str,
        observed_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO observations(event_id, obligation_id, source, fingerprint, last_seen_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                  obligation_id = excluded.obligation_id,
                  source = excluded.source,
                  fingerprint = excluded.fingerprint,
                  last_seen_at = excluded.last_seen_at
                """,
                (event_id, obligation_id, source, fingerprint, observed_at.isoformat()),
            )

    def observation(self, event_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT event_id, obligation_id, source, fingerprint, last_seen_at FROM observations WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        return dict(row) if row else None

    def upsert_relationship(self, relationship: Relationship, *, reason: str = "relationship reconciliation") -> bool:
        payload = self._encode(relationship.to_dict())
        updated = relationship.last_interaction_at or utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM relationships WHERE relationship_key = ?",
                (relationship.key,),
            ).fetchone()
            if row and row["payload"] == payload:
                return False
            connection.execute(
                """
                INSERT INTO relationships(relationship_key, updated_at, payload)
                VALUES(?, ?, ?)
                ON CONFLICT(relationship_key) DO UPDATE SET
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (relationship.key, updated.isoformat(), payload),
            )
            self._record_audit(
                connection,
                "relationship",
                relationship.key,
                "created" if row is None else "updated",
                reason,
                utc_now(),
                payload,
            )
        return True

    def relationship(self, key: str) -> Relationship | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM relationships WHERE relationship_key = ?",
                (str(key).casefold(),),
            ).fetchone()
        return Relationship.from_mapping(self._decode(row["payload"])) if row else None

    def relationships(self, *, limit: int = 5000) -> list[Relationship]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM relationships ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(50_000, int(limit))),),
            ).fetchall()
        return [Relationship.from_mapping(self._decode(row["payload"])) for row in rows]

    def transitions(self, obligation_id: str, *, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT occurred_at, from_state, to_state, reason
                FROM transitions
                WHERE obligation_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (str(obligation_id), max(1, min(1000, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit(self, subject_key: str, *, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT occurred_at, subject_type, action, reason, payload_hash
                FROM audit_entries
                WHERE subject_key = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (str(subject_key), max(1, min(1000, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_snapshot(
        self,
        path: str | Path,
        *,
        now: datetime | None = None,
        obligations: Sequence[Obligation] | None = None,
    ) -> None:
        destination = Path(path).expanduser().resolve()
        ensure_private_directory(destination.parent)
        timestamp = now or utc_now()
        payload = {
            "schema_version": 1,
            "generated_at": timestamp.isoformat(),
            "obligations": [
                item.to_dict()
                for item in (self.list(active_only=True) if obligations is None else obligations)
            ],
            "relationships": [item.to_dict() for item in self.relationships()],
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA busy_timeout = 5000")

    def _migrate(self) -> None:
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise LedgerError(
                    f"obligation ledger schema {version} is newer than supported schema {SCHEMA_VERSION}"
                )
            if version == 0:
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE obligations(
                      id TEXT PRIMARY KEY,
                      obligation_key TEXT NOT NULL UNIQUE,
                      state TEXT NOT NULL,
                      priority INTEGER NOT NULL,
                      updated_at TEXT NOT NULL,
                      payload TEXT NOT NULL
                    );
                    CREATE INDEX obligations_state_priority
                      ON obligations(state, priority DESC, updated_at DESC);
                    CREATE TABLE observations(
                      event_id TEXT PRIMARY KEY,
                      obligation_id TEXT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
                      source TEXT NOT NULL,
                      fingerprint TEXT NOT NULL,
                      last_seen_at TEXT NOT NULL
                    );
                    CREATE INDEX observations_obligation
                      ON observations(obligation_id, last_seen_at DESC);
                    CREATE TABLE transitions(
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      obligation_id TEXT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
                      occurred_at TEXT NOT NULL,
                      from_state TEXT NOT NULL,
                      to_state TEXT NOT NULL,
                      reason TEXT NOT NULL
                    );
                    CREATE TABLE relationships(
                      relationship_key TEXT PRIMARY KEY,
                      updated_at TEXT NOT NULL,
                      payload TEXT NOT NULL
                    );
                    CREATE TABLE audit_entries(
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      subject_type TEXT NOT NULL,
                      subject_key TEXT NOT NULL,
                      occurred_at TEXT NOT NULL,
                      action TEXT NOT NULL,
                      reason TEXT NOT NULL,
                      payload_hash TEXT NOT NULL
                    );
                    CREATE INDEX audit_entries_subject
                      ON audit_entries(subject_key, sequence DESC);
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )
            elif version == 1:
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE audit_entries(
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      subject_type TEXT NOT NULL,
                      subject_key TEXT NOT NULL,
                      occurred_at TEXT NOT NULL,
                      action TEXT NOT NULL,
                      reason TEXT NOT NULL,
                      payload_hash TEXT NOT NULL
                    );
                    CREATE INDEX audit_entries_subject
                      ON audit_entries(subject_key, sequence DESC);
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )

    @staticmethod
    def _record_transition(
        connection: sqlite3.Connection,
        obligation_id: str,
        from_state: str,
        to_state: str,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO transitions(obligation_id, occurred_at, from_state, to_state, reason)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                obligation_id,
                occurred_at.isoformat(),
                from_state,
                to_state,
                " ".join(str(reason).split())[:512],
            ),
        )

    def _record_audit(
        self,
        connection: sqlite3.Connection,
        subject_type: str,
        subject_key: str,
        action: str,
        reason: str,
        occurred_at: datetime,
        payload: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_entries(subject_type, subject_key, occurred_at, action, reason, payload_hash)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                subject_type,
                subject_key,
                occurred_at.isoformat(),
                action,
                " ".join(str(reason).split())[:512],
                sha256(payload.encode("utf-8")).hexdigest(),
            ),
        )
        connection.execute(
            """
            DELETE FROM audit_entries
            WHERE sequence <= COALESCE((SELECT MAX(sequence) - ? FROM audit_entries), 0)
            """,
            (self.audit_max_entries,),
        )

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(value: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise LedgerError("obligation ledger contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LedgerError("obligation ledger payload must be an object")
        return payload

    @classmethod
    def _decode_obligation(cls, value: str) -> Obligation:
        return Obligation.from_mapping(cls._decode(value))
