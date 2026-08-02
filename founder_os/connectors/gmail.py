"""Gmail connector for actionable inbox items and outgoing commitments."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError
from founder_os.connectors.google_oauth import GoogleAccessTokenProvider
from founder_os.connectors.http_client import ConnectorHTTPError, request_json
from founder_os.models import Event


DEFAULT_ACTION_KEYWORDS = (
    "action required", "action requise", "approval", "approbation",
    "please confirm", "merci de confirmer", "confirmation requise",
    "decision required", "décision requise", "à valider", "please review",
    "signature required", "signature requise", "question",
)
DEFAULT_FYI_KEYWORDS = (
    "receipt", "reçu", "invoice", "facture", "refund", "remboursement",
    "payment received", "paiement reçu", "statement", "relevé",
    "newsletter", "digest", "order confirmation", "confirmation de commande",
)
DEFAULT_URGENT_KEYWORDS = ("urgent", "deadline", "échéance", "today", "aujourd’hui")
DEFAULT_NON_ACTION_KEYWORDS = (
    "no action required", "no action needed", "aucune action requise",
    "aucune action nécessaire", "for your information", "pour information",
)
DEFAULT_PROMISE_KEYWORDS = (
    "i will", "i’ll", "i'll", "we will", "we’ll", "we'll", "will send", "will share",
    "je vais", "je vous envoie", "je t’envoie", "nous allons", "on va", "je reviens vers",
)


class GmailConnector(Connector):
    name = "gmail"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token_provider = GoogleAccessTokenProvider(config, secrets=self.secrets)
        self.query = str(config.get("query", "is:unread newer_than:2d"))
        raw_queries = config.get("queries")
        if raw_queries is not None and not isinstance(raw_queries, list):
            raise ConnectorConfigurationError("gmail.queries must be a list")
        self.queries = self._query_specs(raw_queries) if raw_queries else ((self.query, "incoming"),)
        self.vip_senders = {str(value).casefold() for value in config.get("vip_senders", [])}
        self.action_keywords = self._keywords(config.get("action_keywords", DEFAULT_ACTION_KEYWORDS))
        self.fyi_keywords = self._keywords(config.get("fyi_keywords", DEFAULT_FYI_KEYWORDS))
        self.urgent_keywords = self._keywords(config.get("urgent_keywords", DEFAULT_URGENT_KEYWORDS))
        self.non_action_keywords = self._keywords(
            config.get("non_action_keywords", DEFAULT_NON_ACTION_KEYWORDS)
        )
        self.promise_keywords = self._keywords(config.get("promise_keywords", DEFAULT_PROMISE_KEYWORDS))
        self.max_results = min(25, max(1, int(config.get("max_results", 10))))
        self.detail_workers = min(8, max(1, int(config.get("detail_workers", 5))))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 4)))
        self.endpoint = str(config.get("endpoint", "https://gmail.googleapis.com/gmail/v1"))

    @staticmethod
    def _keywords(values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise ConnectorConfigurationError("Gmail keyword settings must be lists")
        return tuple(str(value).strip().casefold() for value in values if str(value).strip())

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        message_directions: dict[str, str] = {}
        for query_value, direction in self.queries:
            listing = self._request(
                f"{self.endpoint.rstrip('/')}/users/me/messages",
                now,
                query={"q": query_value, "maxResults": self.max_results},
                deadline=deadline,
            )
            rows = listing.get("messages") or []
            if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
                raise ConnectorError("Gmail response did not contain a message list")
            for item in rows:
                message_id = str(item.get("id") or "")
                if message_id:
                    message_directions.setdefault(message_id, direction)
        message_ids = list(message_directions)
        with ThreadPoolExecutor(max_workers=self.detail_workers, thread_name_prefix="founderos-gmail") as executor:
            messages = list(
                executor.map(lambda message_id: self._fetch_message(message_id, now, deadline), message_ids)
            )
        events: list[Event] = []
        for message_id, message in zip(message_ids, messages):
            if message is None:
                continue
            message["_founderos_direction"] = message_directions[message_id]
            event = self._normalize(message, now)
            if event.action_required or message_directions[message_id] == "outgoing":
                events.append(event)
        return events

    def _fetch_message(self, message_id: str, now: datetime, deadline: float) -> dict[str, Any] | None:
        try:
            return self._request(
                f"{self.endpoint.rstrip('/')}/users/me/messages/{message_id}",
                now,
                query={"format": "metadata", "metadataHeaders": ["Subject", "From", "To", "Cc"]},
                deadline=deadline,
            )
        except ConnectorHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise

    def _request(
        self,
        url: str,
        now: datetime,
        *,
        query: Mapping[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        timeout = self._remaining_timeout(deadline)
        try:
            return request_json(
                url,
                query=query,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                retries=0,
                deadline_monotonic=deadline,
            )
        except ConnectorHTTPError as exc:
            if exc.status_code != 401 or not self.token_provider.refreshable:
                raise
        self.token_provider.invalidate()
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        timeout = self._remaining_timeout(deadline)
        return request_json(
            url,
            query=query,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            retries=0,
            deadline_monotonic=deadline,
        )

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Gmail poll exceeded {self.poll_timeout_seconds:.0f} seconds")
        return min(self.request_timeout, remaining)

    def _normalize(self, message: Mapping[str, Any], now: datetime) -> Event:
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            raise ConnectorError("Gmail message is missing its id")
        headers = {
            str(item.get("name", "")).casefold(): str(item.get("value", ""))
            for item in ((message.get("payload") or {}).get("headers") or [])
        }
        subject = " ".join((headers.get("subject") or "No subject").split())
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        recipient_name, recipient_email = parseaddr(headers.get("to", ""))
        direction = str(message.get("_founderos_direction") or ("outgoing" if "SENT" in set(message.get("labelIds") or []) else "incoming"))
        sender_label = sender_name or sender_email or "Unknown sender"
        recipient_label = recipient_name or recipient_email or "Unknown recipient"
        sender_folded = sender_email.casefold()
        vip = self._is_vip_sender(sender_folded)
        labels = set(message.get("labelIds") or [])
        important = "IMPORTANT" in labels
        snippet = " ".join(str(message.get("snippet") or "").split())
        classification_text = f"{subject} {snippet}".casefold()
        action_keywords = getattr(self, "action_keywords", DEFAULT_ACTION_KEYWORDS)
        fyi_keywords = getattr(self, "fyi_keywords", DEFAULT_FYI_KEYWORDS)
        urgent_keywords = getattr(self, "urgent_keywords", DEFAULT_URGENT_KEYWORDS)
        non_action_keywords = getattr(self, "non_action_keywords", DEFAULT_NON_ACTION_KEYWORDS)
        non_action = any(keyword in classification_text for keyword in non_action_keywords)
        explicit_action = not non_action and any(
            keyword in classification_text for keyword in action_keywords
        )
        fyi = any(keyword in classification_text for keyword in fyi_keywords)
        fyi = fyi or non_action
        fyi = fyi or "noreply" in sender_folded or "no-reply" in sender_folded
        urgent = any(keyword in classification_text for keyword in urgent_keywords)
        promise_keywords = getattr(self, "promise_keywords", DEFAULT_PROMISE_KEYWORDS)
        promise = direction == "outgoing" and any(keyword in classification_text for keyword in promise_keywords)
        attachment = self._has_attachment(message.get("payload") or {})
        action_required = explicit_action or vip or (important and not fyi) or promise
        internal_ms = message.get("internalDate")
        try:
            occurred_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            occurred_at = now
        title = f"{recipient_label if direction == 'outgoing' else sender_label}: {subject}"
        counterparty_name = recipient_label if direction == "outgoing" else sender_label
        counterparty_email = recipient_email if direction == "outgoing" else sender_email
        counterparty_domain = counterparty_email.rsplit("@", 1)[1].casefold() if "@" in counterparty_email else ""
        return Event(
            id=f"gmail:{message_id}",
            source="gmail",
            kind="deadline" if urgent and action_required else "email",
            title=title,
            body=snippet,
            priority=self._priority(vip, explicit_action, important, fyi, attachment),
            action_required=action_required,
            urgency="high" if vip or (urgent and action_required) else "normal",
            impact="high" if vip else "medium" if action_required else "low",
            occurred_at=occurred_at,
            expires_at=occurred_at + timedelta(days=30 if direction == "outgoing" else 2),
            dedupe_key=f"gmail-thread:{message.get('threadId') or message_id}",
            url=f"https://mail.google.com/mail/u/0/#{'sent' if direction == 'outgoing' else 'inbox'}/{message_id}",
            metadata={
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "cc": headers.get("cc", ""),
                "subject": subject,
                "labels": sorted(labels),
                "classification": "outgoing_promise" if promise else self._classification(vip, explicit_action, important, fyi, attachment),
                "has_attachment": attachment,
                "direction": direction,
                "thread_id": str(message.get("threadId") or message_id),
                "sender_name": sender_name,
                "sender_email": sender_email,
                "sender_domain": sender_email.rsplit("@", 1)[1].casefold() if "@" in sender_email else "",
                "counterparty": counterparty_name,
                "relationship_key": counterparty_domain,
                "obligation": promise,
            },
        )

    @staticmethod
    def _query_specs(values: list[Any]) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for value in values:
            if isinstance(value, str):
                query, direction = value.strip(), "incoming"
            elif isinstance(value, Mapping):
                query = str(value.get("query") or "").strip()
                direction = str(value.get("direction") or "incoming").strip().lower()
            else:
                raise ConnectorConfigurationError("gmail.queries entries must be strings or objects")
            if not query or direction not in {"incoming", "outgoing"}:
                raise ConnectorConfigurationError("gmail.queries entries require a query and a valid direction")
            result.append((query, direction))
        if not result:
            raise ConnectorConfigurationError("gmail.queries cannot be empty")
        return tuple(result)

    @staticmethod
    def _priority(vip: bool, explicit: bool, important: bool, fyi: bool, attachment: bool) -> int:
        if vip:
            return 82
        if explicit:
            return 76
        if important and not fyi:
            return 68
        if attachment:
            return 48
        return 34 if fyi else 46

    @staticmethod
    def _classification(vip: bool, explicit: bool, important: bool, fyi: bool, attachment: bool) -> str:
        if vip:
            return "vip_action"
        if explicit:
            return "explicit_action"
        if important and not fyi:
            return "important_action"
        if attachment:
            return "artifact_received"
        return "fyi" if fyi else "unread"

    def _is_vip_sender(self, sender_email: str) -> bool:
        sender = sender_email.strip().casefold()
        if not sender or "@" not in sender:
            return False
        domain = sender.rsplit("@", 1)[1]
        return any(
            sender == value
            or (value.startswith("@") and domain == value[1:])
            or ("@" not in value and domain == value)
            for value in self.vip_senders
        )

    @staticmethod
    def _has_attachment(payload: Any) -> bool:
        stack = [payload]
        visited = 0
        while stack and visited < 1000:
            part = stack.pop()
            visited += 1
            if not isinstance(part, Mapping):
                continue
            if str(part.get("filename") or "").strip():
                return True
            children = part.get("parts") or []
            if isinstance(children, list):
                stack.extend(children)
        return False
