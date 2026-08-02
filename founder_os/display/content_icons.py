"""Deterministic content-aware pixel icons for the 72 by 16 front display."""

from __future__ import annotations

import unicodedata

from founder_os.models import Event


ICON_SIZE = 8

# `#` uses the event accent, `+` uses white, and `.` uses the background.
_PIXEL_DRAWINGS: dict[str, tuple[tuple[str, ...], ...]] = {
    "alert": (
        (
            "...##...",
            "..#++#..",
            ".#++++#.",
            "#..++..#",
            "#..++..#",
            ".#....#.",
            "..#++#..",
            "...##...",
        ),
        (
            "...##...",
            "..####..",
            ".##++##.",
            "##.++.##",
            "##.++.##",
            ".##..##.",
            "..#++#..",
            "...##...",
        ),
    ),
    "calendar": (
        (
            ".#....#.",
            "+#++++#+",
            "########",
            "#......#",
            "#.++...#",
            "#.++...#",
            "#......#",
            "########",
        ),
        (
            ".#....#.",
            "+#++++#+",
            "########",
            "#......#",
            "#...++.#",
            "#...++.#",
            "#......#",
            "########",
        ),
    ),
    "decision": (
        (
            "########",
            "#......#",
            "#.....+#",
            "#....+.#",
            "#+..+..#",
            "#.++...#",
            "#......#",
            "########",
        ),
        (
            "########",
            "#......#",
            "#....++#",
            "#...++.#",
            "#+.++..#",
            "#.++...#",
            "#......#",
            "########",
        ),
    ),
    "task": (
        (
            "########",
            "#......#",
            "#.+.###.",
            "#......#",
            "#...###.",
            "#.+....#",
            "#...###.",
            "########",
        ),
        (
            "########",
            "#......#",
            "#...###.",
            "#.+....#",
            "#...###.",
            "#......#",
            "#.+.###.",
            "########",
        ),
    ),
    "focus": (
        (
            "..####..",
            ".#....#.",
            "#..++..#",
            "#.+..+.#",
            "#.+..+.#",
            "#..++..#",
            ".#....#.",
            "..####..",
        ),
        (
            "........",
            "..####..",
            ".#.++.#.",
            ".#+..+#.",
            ".#+..+#.",
            ".#.++.#.",
            "..####..",
            "........",
        ),
    ),
}

# FounderOS intentionally exposes a six-state visual language. The private
# pixel drawings above are reused to keep the state vocabulary compact.
ICON_FRAMES = {
    "waiting": (
        (
            "########",
            ".#++++#.",
            "..#++#..",
            "...##...",
            "...##...",
            "..#..#..",
            ".#....#.",
            "########",
        ),
        (
            "########",
            ".#....#.",
            "..#..#..",
            "...##...",
            "...##...",
            "..#++#..",
            ".#++++#.",
            "########",
        ),
    ),
    "blocked": _PIXEL_DRAWINGS["alert"],
    "decision": _PIXEL_DRAWINGS["focus"],
    "meeting": _PIXEL_DRAWINGS["calendar"],
    "validation": _PIXEL_DRAWINGS["task"],
    "success": _PIXEL_DRAWINGS["decision"],
}


_ALERT_KEYWORDS = (
    "blocked", "blocker", "bloqu", "incident", "urgent", "escalad", "outage",
    "failure", "failed", "echec", "risk", "risque", "critical", "critique",
    "overdue", "retard",
)
_CALENDAR_KEYWORDS = (
    "meeting", "reunion", "rendez-vous", "calendar", "agenda", "appel", "call",
    "demo", "entretien",
)
_DECISION_KEYWORDS = (
    "decision", "decide", "choisir", "arbitr", "repond", "reply", "respond", "envoyer",
    "send", "confirmer", "confirm", "question", "permission",
)
_VALIDATION_KEYWORDS = (
    "approve", "approval", "approuv", "validate", "validation", "review", "revue",
    "signer", "signature", "accept", "qa", "test", "verification", "verifier",
)
_SUCCESS_KEYWORDS = (
    "success", "succeeded", "complete", "completed", "done", "resolved", "shipped",
    "deployed", "reussi", "termine", "finalise", "livre", "resolu", "valide",
)


def select_content_icon(event: Event) -> str:
    """Choose one of the six governed state icons from event semantics."""
    searchable = _searchable_text(" ".join((event.title, event.body, event.kind)))
    if event.urgency == "critical" or event.kind in {"blocker", "incident"}:
        return "blocked"
    if any(keyword in searchable for keyword in _ALERT_KEYWORDS):
        return "blocked"
    if any(keyword in searchable for keyword in _SUCCESS_KEYWORDS):
        return "success"
    if event.source == "calendar" or any(keyword in searchable for keyword in _CALENDAR_KEYWORDS):
        return "meeting"
    if any(keyword in searchable for keyword in _VALIDATION_KEYWORDS):
        return "validation"
    if event.kind == "permission_request" or event.action_required or any(
        keyword in searchable for keyword in _DECISION_KEYWORDS
    ):
        return "decision"
    return "waiting"


def content_icon_frame(event: Event, frame_index: int) -> tuple[str, tuple[str, ...]]:
    icon_name = select_content_icon(event)
    frames = ICON_FRAMES[icon_name]
    return icon_name, frames[frame_index % len(frames)]


def _searchable_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


for _icon_frames in ICON_FRAMES.values():
    for _frame in _icon_frames:
        assert len(_frame) == ICON_SIZE
        assert all(len(row) == ICON_SIZE and set(row) <= {".", "#", "+"} for row in _frame)
