"""Compact, hardware-safe layouts for exactly 72 by 16 pixels."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from founder_os.display.content_icons import content_icon_frame
from founder_os.models import Event, RankedEvent, utc_now


SOURCE_COLORS = {
    "linear": "0x7C5CFCFF",
    "slack": "0x36C5F0FF",
    "gmail": "0xEA4335FF",
    "calendar": "0x4285F4FF",
    "linkedin": "0x0A66C2FF",
    "claude": "0xD97745FF",
    "chatgpt_codex": "0x10A37FFF",
    "demo": "0x7C5CFCFF",
}
CRITICAL = "0xFF3C3CFF"
ACTION = "0xFFB020FF"
IDLE = "0x28DC6EFF"
WHITE = "0xFFFFFFFF"
MUTED = "0x8B95A7FF"
BACKGROUND = "0x080B10FF"


def rectangle(element_id: str, x: int, y: int, width: int, height: int, color: str) -> dict[str, Any]:
    return {
        "id": element_id,
        "type": "rectangle",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "border_width": 0,
        "fill": "solid",
        "fill_colors": [color],
    }


def text(
    element_id: str,
    value: str,
    *,
    x: int,
    y: int,
    font: str,
    color: str,
    width: int | None = None,
    align: str | None = None,
    scroll: bool = False,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "id": element_id,
        "type": "text",
        "text": value,
        "x": x,
        "y": y,
        "font": font,
        "color": color,
    }
    if width is not None:
        element["width"] = width
    if align:
        element["align"] = align
    if scroll:
        element.update({"scroll_rate": 420, "scroll_start_delay": 1400, "scroll_repeat_delay": 900})
    return element


def event_layout(
    selection: RankedEvent,
    now: datetime | None = None,
    *,
    icon_frame: int | None = 0,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    event = selection.event
    if event.kind == "permission_request":
        return permission_request_layout(event, now, frame_index=icon_frame or 0)
    if event.kind == "agent_usage":
        return agent_usage_layout(event)
    critical = event.urgency == "critical" or event.kind in {"blocker", "incident"}
    accent = CRITICAL if critical else ACTION if event.action_required else SOURCE_COLORS.get(event.source, WHITE)
    source_color = SOURCE_COLORS.get(event.source, accent)
    source_label = event.source.replace("chatgpt_codex", "CODEX").replace("calendar", "CAL").upper()[:8]
    status = _due_label(event.due_at, now) if event.due_at else "ACT" if event.action_required else "NOW"
    has_icon = icon_frame is not None
    title_x = 16 if has_icon else 6
    title_width = 72 - title_x
    max_characters = 14 if has_icon else 18
    title_lines = _wrap_title(event.title, max_characters=max_characters)
    score_x = title_x - 1
    score_width = 72 - score_x
    progress = max(
        4,
        min(score_width, round(max(0.0, min(120.0, selection.score)) / 120.0 * score_width)),
    )
    elements = [
        rectangle("bg", 0, 0, 72, 16, BACKGROUND),
        rectangle("accent", 0, 0, 3, 16, accent),
        text("source", source_label, x=6, y=0, font="tiny", color=source_color),
        text("status", status, x=70, y=0, font="tiny", color=accent, align="top_right"),
    ]
    if has_icon:
        elements.extend(_content_icon_elements(event, icon_frame, x=5, y=7, accent=accent))
    if not event.title.isascii():
        elements.append(
            text(
                "title",
                event.title,
                x=title_x,
                y=5,
                font="global",
                color=WHITE,
                width=title_width,
                scroll=True,
            )
        )
        return elements
    if len(title_lines) == 1:
        elements.extend(
            [
                text(
                    "title",
                    title_lines[0],
                    x=title_x,
                    y=7,
                    font="bold",
                    color=WHITE,
                    width=title_width,
                ),
                rectangle("score", score_x, 15, progress, 1, accent),
            ]
        )
    else:
        elements.extend(
            [
                text(
                    "title-1",
                    title_lines[0],
                    x=title_x,
                    y=6,
                    font="small",
                    color=WHITE,
                    width=title_width,
                ),
                text(
                    "title-2",
                    title_lines[1],
                    x=title_x,
                    y=11,
                    font="small",
                    color=accent,
                    width=title_width,
                    scroll=len(title_lines[1]) > max_characters,
                ),
            ]
        )
    return elements


def permission_request_layout(
    event: Event,
    now: datetime | None = None,
    *,
    frame_index: int = 0,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    source_color = SOURCE_COLORS.get(event.source, ACTION)
    source_label = "CLAUDE" if event.source == "claude" else "CODEX"
    seconds = max(0, int(((event.expires_at or now) - now).total_seconds() + 0.999))
    pulse = WHITE if frame_index % 2 else ACTION
    title_font = "global" if not event.title.isascii() else "small"
    elements = [
        rectangle("bg", 0, 0, 72, 16, BACKGROUND),
        rectangle("accent", 0, 0, 2, 16, pulse),
        text("source", source_label, x=4, y=0, font="tiny", color=source_color),
        text("status", f"? {seconds}S", x=70, y=0, font="tiny", color=pulse, align="top_right"),
        text(
            "title",
            event.title,
            x=4,
            y=5,
            font=title_font,
            color=WHITE,
            width=66,
            scroll=True,
        ),
        rectangle("deny-line", 3, 15, 32, 1, CRITICAL),
        rectangle("allow-line", 38, 15, 34, 1, IDLE),
    ]
    if event.title.isascii():
        elements.extend(
            [
                text("deny", "NON", x=4, y=11, font="tiny", color=CRITICAL),
                text("allow", "OUI", x=69, y=11, font="tiny", color=IDLE, align="top_right"),
            ]
        )
    return elements


def agent_usage_layout(event: Event) -> list[dict[str, Any]]:
    source_color = SOURCE_COLORS.get(event.source, WHITE)
    source_label = "CLAUDE" if event.source == "claude" else "CODEX"
    windows = event.metadata.get("windows")
    rows = [item for item in windows if isinstance(item, Mapping)][:2] if isinstance(windows, list) else []
    elements = [
        rectangle("bg", 0, 0, 72, 16, BACKGROUND),
        rectangle("accent", 0, 0, 2, 16, source_color),
        text("source", source_label, x=4, y=0, font="tiny", color=source_color),
        text("status", "USAGE", x=70, y=0, font="tiny", color=MUTED, align="top_right"),
    ]
    y_positions = (8,) if len(rows) == 1 else (6, 12)
    for index, (window, y) in enumerate(zip(rows, y_positions)):
        try:
            used_percent = max(0.0, min(100.0, float(window.get("used_percent", 0))))
        except (TypeError, ValueError):
            used_percent = 0.0
        color = CRITICAL if used_percent >= 90 else ACTION if used_percent >= 70 else source_color
        width = round(used_percent / 100 * 50)
        elements.extend(
            [
                text(
                    f"window-{index}",
                    str(window.get("label") or "?")[:8],
                    x=4,
                    y=y - 1,
                    font="tiny",
                    color=WHITE,
                ),
                rectangle(f"track-{index}", 18, y, 50, 3, "0x202734FF"),
            ]
        )
        if width:
            elements.append(rectangle(f"used-{index}", 18, y, width, 3, color))
    return elements


def _content_icon_elements(
    event: Event,
    frame_index: int,
    *,
    x: int,
    y: int,
    accent: str,
) -> list[dict[str, Any]]:
    _, pattern = content_icon_frame(event, frame_index)
    colors = {".": BACKGROUND, "#": accent, "+": WHITE}
    return [
        rectangle(f"icon-{row}-{column}", x + column, y + row, 1, 1, colors[pixel])
        for row, line in enumerate(pattern)
        for column, pixel in enumerate(line)
    ]


def idle_layout() -> list[dict[str, Any]]:
    return [
        rectangle("bg", 0, 0, 72, 16, BACKGROUND),
        rectangle("accent", 0, 0, 3, 16, IDLE),
        text("source", "FOUNDEROS", x=6, y=0, font="tiny", color=MUTED),
        text("idle", "ALL CLEAR", x=6, y=7, font="bold", color=IDLE),
        rectangle("line", 5, 15, 67, 1, IDLE),
    ]


def startup_layout() -> list[dict[str, Any]]:
    return [
        rectangle("bg", 0, 0, 72, 16, BACKGROUND),
        rectangle("accent", 0, 0, 3, 16, "0x7C5CFCFF"),
        text("brand", "FOUNDER", x=6, y=0, font="tiny", color=MUTED),
        text("os", "OS READY", x=6, y=7, font="bold", color=WHITE),
        rectangle("line", 5, 15, 67, 1, "0x7C5CFCFF"),
    ]


def _due_label(due_at: datetime | None, now: datetime) -> str:
    if not due_at:
        return "NOW"
    minutes = round((due_at - now).total_seconds() / 60)
    if minutes <= 0:
        return "DUE"
    if minutes < 60:
        return f"{minutes}M"
    return f"{round(minutes / 60)}H"


def _wrap_title(value: str, *, max_characters: int) -> list[str]:
    words = value.split()
    if not words:
        return [""]
    if len(words) == 1 and len(words[0]) > max_characters:
        return [words[0][:max_characters], words[0][max_characters:]]
    first: list[str] = []
    while words:
        candidate = " ".join(first + [words[0]])
        if first and len(candidate) > max_characters:
            break
        first.append(words.pop(0))
    if not words:
        return [" ".join(first)]
    return [" ".join(first), " ".join(words)]
