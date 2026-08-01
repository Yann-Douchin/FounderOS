"""Short deterministic frame sequences for startup and state changes."""

from __future__ import annotations

from typing import Any

from founder_os.display.layouts import BACKGROUND, rectangle, startup_layout


def startup_frames() -> list[list[dict[str, Any]]]:
    """Three frames that can be rendered without a stock emulator-only animation."""
    return [
        [rectangle("bg", 0, 0, 72, 16, BACKGROUND), rectangle("scan", 0, 7, 18, 2, "0x7C5CFCFF")],
        [rectangle("bg", 0, 0, 72, 16, BACKGROUND), rectangle("scan", 18, 7, 36, 2, "0x7C5CFCFF")],
        startup_layout(),
    ]
