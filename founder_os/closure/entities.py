"""Deterministic cross-source entity correlation with conservative aliases."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from founder_os.models import Event


_EMAIL = re.compile(r"[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})", re.IGNORECASE)
_STOPWORDS = {
    "about", "access", "action", "approval", "blocked", "client", "customer", "decision",
    "follow", "from", "issue", "meeting", "need", "project", "ready", "reply", "request",
    "review", "send", "the", "this", "waiting", "with",
    "accès", "avec", "besoin", "bloqué", "client", "décision", "envoyer", "projet", "réunion",
    "réponse", "validation",
}


class EntityGraph:
    def __init__(self, events: Iterable[Event], config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.alias_to_key: dict[str, str] = {}
        self.key_to_name: dict[str, str] = {}
        configured = self.config.get("entity_aliases") or {}
        if isinstance(configured, Mapping):
            for canonical, aliases in configured.items():
                key = "entity:" + fold(canonical)
                self._register(key, str(canonical), [canonical, *(aliases if isinstance(aliases, list) else [])])
        candidates: dict[str, list[str]] = defaultdict(list)
        for event in events:
            metadata = event.metadata
            for field in ("project", "customer", "account", "company", "organization", "counterparty"):
                value = clean(metadata.get(field))
                if value:
                    candidates["entity:" + fold(value)].append(value)
            project_id = clean(metadata.get("project_id"))
            project = clean(metadata.get("project"))
            if project_id and project:
                candidates["project:" + fold(project_id)].append(project)
        for key, names in candidates.items():
            name = sorted(names, key=lambda value: (-len(value), value.casefold()))[0]
            self._register(key, name, names)

    def keys_for(self, event: Event) -> tuple[str, ...]:
        metadata = event.metadata
        keys: list[str] = []
        explicit = metadata.get("entity_keys")
        if isinstance(explicit, list):
            keys.extend("entity:" + fold(value) for value in explicit if clean(value))
        project_id = clean(metadata.get("project_id"))
        project = clean(metadata.get("project"))
        if project_id:
            keys.append("project:" + fold(project_id))
        if project:
            keys.append(self.alias_to_key.get(fold(project), "entity:" + fold(project)))
        for field in ("customer", "account", "company", "organization", "counterparty"):
            value = clean(metadata.get(field))
            if value:
                keys.append(self.alias_to_key.get(fold(value), "entity:" + fold(value)))
        relationship = clean(metadata.get("relationship_key"))
        if relationship:
            keys.append("relationship:" + fold(relationship))
        domain = relationship_domain(event)
        if domain:
            keys.append("domain:" + domain)
        thread_id = clean(metadata.get("thread_id") or metadata.get("threadId"))
        if thread_id:
            keys.append(f"thread:{event.source}:{fold(thread_id)}")
        title_folded = fold(event.title + " " + event.body)
        for alias, key in sorted(self.alias_to_key.items(), key=lambda item: -len(item[0])):
            if len(alias) >= 4 and _contains_phrase(title_folded, alias):
                keys.append(key)
        return tuple(dict.fromkeys(key for key in keys if key and not key.endswith(":")))

    def primary(self, event: Event) -> str:
        keys = self.keys_for(event)
        if event.source in {"gmail", "slack", "superhuman"}:
            project = next((key for key in keys if key.startswith("project:")), None)
            thread = next((key for key in keys if key.startswith("thread:")), None)
            if project or thread:
                return project or thread or ""
        for prefix in ("project:", "entity:", "relationship:", "domain:", "thread:"):
            match = next((key for key in keys if key.startswith(prefix)), None)
            if match:
                return match
        return "event:" + fold(event.dedupe_key or event.id)

    def display_name(self, key: str, fallback: str = "") -> str:
        if key in self.key_to_name:
            return self.key_to_name[key]
        suffix = key.split(":", 1)[-1].replace("_", " ").strip()
        return clean(fallback) or suffix.title() or "Obligation"

    def _register(self, key: str, name: str, aliases: Iterable[Any]) -> None:
        self.key_to_name[key] = clean(name)
        for value in aliases:
            alias = fold(value)
            if len(alias) >= 3 and alias not in _STOPWORDS:
                self.alias_to_key.setdefault(alias, key)


def relationship_domain(event: Event) -> str:
    metadata = event.metadata
    direct = clean(metadata.get("sender_domain") or metadata.get("domain"))
    if direct:
        return direct.casefold().strip(".")
    for value in (
        metadata.get("sender"), metadata.get("from"), metadata.get("email"),
        event.title, event.body,
    ):
        match = _EMAIL.search(str(value or ""))
        if match:
            return match.group(1).casefold().strip(".")
    try:
        host = urlsplit(event.url).hostname or ""
    except ValueError:
        host = ""
    ignored = {"linear.app", "slack.com", "google.com", "notion.so", "github.com"}
    return host.casefold() if host and host.casefold() not in ignored else ""


def significant_tokens(value: Any) -> tuple[str, ...]:
    tokens = re.findall(r"[^\W\d_]{3,}", fold(value), flags=re.UNICODE)
    return tuple(dict.fromkeys(token for token in tokens if token not in _STOPWORDS))


def fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_like = "".join(character for character in normalized if not unicodedata.combining(character))
    return "_".join(re.findall(r"[^\W_]+", ascii_like.casefold(), flags=re.UNICODE))


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def _contains_phrase(value: str, phrase: str) -> bool:
    return bool(re.search(rf"(?:^|_){re.escape(phrase)}(?:_|$)", value))
