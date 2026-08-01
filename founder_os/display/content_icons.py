"""Deterministic content-aware pixel icons for the 72 by 16 front display."""

from __future__ import annotations

import unicodedata

from founder_os.models import Event


ICON_SIZE = 8

# `#` uses the event accent, `+` uses white, and `.` uses the background.
ICON_FRAMES: dict[str, tuple[tuple[str, ...], ...]] = {
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
    "mail": (
        (
            "......+.",
            "#######.",
            "#.....#+",
            "##...##.",
            "#.#.#.#.",
            "#..#..#.",
            "#.....#.",
            "#######.",
        ),
        (
            ".......+",
            "#######+",
            "#.....#.",
            "##...##.",
            "#.#.#.#.",
            "#..#..#.",
            "#.....#.",
            "#######.",
        ),
    ),
    "chat": (
        (
            ".######.",
            "#......#",
            "#.+.+..#",
            "#......#",
            "#......#",
            ".#####..",
            "..#.....",
            ".#......",
        ),
        (
            ".######.",
            "#......#",
            "#..+.+.#",
            "#......#",
            "#......#",
            ".#####..",
            "..#.....",
            ".#......",
        ),
    ),
    "code": (
        (
            "########",
            "#......#",
            "#.##...#",
            "#...#..#",
            "#.##...#",
            "#....+.#",
            "#....+.#",
            "########",
        ),
        (
            "########",
            "#......#",
            "#.##...#",
            "#...#..#",
            "#.##...#",
            "#......#",
            "#....++#",
            "########",
        ),
    ),
    "trend": (
        (
            ".......+",
            ".....##+",
            "...###..",
            "..##....",
            "###.....",
            "#.......",
            "#.......",
            "########",
        ),
        (
            ".....+++",
            ".....#++",
            "...###.+",
            "..##....",
            "###.....",
            "#.......",
            "#.......",
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


_ALERT_KEYWORDS = (
    "blocked", "blocker", "bloqu", "incident", "urgent", "escalad", "outage",
    "failure", "failed", "echec", "risk", "risque", "critical", "critique",
    "overdue", "retard",
)
_CALENDAR_KEYWORDS = (
    "meeting", "reunion", "rendez-vous", "calendar", "agenda", "appel", "call",
    "demo", "entretien",
)
_MAIL_KEYWORDS = (
    "email", "e-mail", "mail", "repond", "inbox", "sender", "expediteur", "courriel",
)
_CHAT_KEYWORDS = ("slack", "mention", "message", "chat", "commentaire", "thread")
_CODE_KEYWORDS = (
    "bug", "fix", "deploy", "code", "engineering", "ingenierie", "pull request",
    "merge", "build", "release", "api", "technique", "develop",
)
_TREND_KEYWORDS = (
    "revenue", "revenu", "sales", "vente", "deal", "client", "customer", "investor",
    "investisseur", "stripe", "shopify", "conversion", "growth", "croissance", "linkedin",
)
_DECISION_KEYWORDS = (
    "decision", "approve", "approval", "approuv", "validate", "validation", "review",
    "revue", "choisir", "arbitr", "signer", "signature",
)


def select_content_icon(event: Event) -> str:
    """Choose one icon from event semantics, with source only as a fallback."""
    searchable = _searchable_text(" ".join((event.title, event.body, event.kind)))
    if event.urgency == "critical" or event.kind in {"blocker", "incident"}:
        return "alert"
    for icon_name, keywords in (
        ("alert", _ALERT_KEYWORDS),
        ("calendar", _CALENDAR_KEYWORDS),
        ("mail", _MAIL_KEYWORDS),
        ("chat", _CHAT_KEYWORDS),
        ("code", _CODE_KEYWORDS),
        ("trend", _TREND_KEYWORDS),
        ("decision", _DECISION_KEYWORDS),
    ):
        if any(keyword in searchable for keyword in keywords):
            return icon_name
    source_fallback = {
        "calendar": "calendar",
        "gmail": "mail",
        "slack": "chat",
        "linear": "task",
        "claude": "code",
        "chatgpt_codex": "code",
        "github": "code",
        "stripe": "trend",
        "shopify": "trend",
        "linkedin": "trend",
    }
    if event.source in source_fallback:
        return source_fallback[event.source]
    return "decision" if event.action_required else "focus"


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
