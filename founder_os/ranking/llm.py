"""Optional, tightly bounded LLM fallback for genuine deterministic ties."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Mapping, Protocol, Sequence

from founder_os.models import RankedEvent


MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class TieBreaker(Protocol):
    def choose(self, candidates: Sequence[RankedEvent]) -> str | None:
        """Return one candidate event id, or None to preserve deterministic order."""


class NoLLMFallback:
    def choose(self, candidates: Sequence[RankedEvent]) -> str | None:
        return None


class OpenAIResponsesTieBreaker:
    """Responses API fallback. It is never called unless the engine detects a close tie."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, config: Mapping[str, Any], api_key: str) -> None:
        self.model = str(config["model"])
        self.api_key = api_key
        self.timeout = float(config.get("timeout_seconds", 8))
        self.max_calls_per_hour = int(config.get("max_calls_per_hour", 6))
        self.max_candidates = max(2, int(config.get("max_candidates", 4)))
        self.max_title_chars = max(32, int(config.get("max_title_chars", 160)))
        self.max_output_tokens = max(16, int(config.get("max_output_tokens", 64)))
        self._calls: deque[float] = deque()

    def choose(self, candidates: Sequence[RankedEvent]) -> str | None:
        if len(candidates) < 2 or not self._within_budget():
            return None
        candidates = candidates[: self.max_candidates]
        allowed = [candidate.event.id for candidate in candidates]
        compact = [
            {
                "id": item.event.id,
                "source": item.event.source,
                "title": item.event.title[: self.max_title_chars],
                "kind": item.event.kind,
                "action_required": item.event.action_required,
                "due_at": item.event.due_at.isoformat() if item.event.due_at else None,
                "deterministic_score": item.score,
            }
            for item in candidates
        ]
        prompt = (
            "Goal: choose the single event a founder should see now on a 72x16 display. "
            "Use only the supplied facts. Prefer immediate blockers, time-sensitive commitments, "
            "and items requiring the founder's action. Return exactly one allowed id.\n\n"
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        )
        body = {
            "model": self.model,
            "store": False,
            "max_output_tokens": self.max_output_tokens,
            "input": prompt,
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "founderos_tie_break",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"event_id": {"type": "string", "enum": allowed}},
                        "required": ["event_id"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._calls.append(time.time())
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
            output_text = self._output_text(payload)
            selected = json.loads(output_text).get("event_id")
            return selected if selected in allowed else None
        except (OSError, ValueError, KeyError, urllib.error.HTTPError):
            return None

    def _within_budget(self) -> bool:
        cutoff = time.time() - 3600
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        return len(self._calls) < self.max_calls_per_hour

    @staticmethod
    def _output_text(payload: Mapping[str, Any]) -> str:
        for item in payload.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return str(content.get("text", ""))
        return ""
