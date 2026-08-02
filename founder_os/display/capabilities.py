"""Versioned BUSY Bar firmware behavior used by the display adapter."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FirmwareCapabilities:
    firmware_version: str
    api_semver: str
    profile: str
    canvas_merges_elements_by_id: bool = True
    same_app_equal_priority_allowed: bool = True
    other_app_requires_strictly_higher_priority: bool = True
    timer_blocks_canvas: bool = True
    physical_busy_hidden_from_snapshot: bool = True
    menu_blocks_canvas: bool = True
    smart_home_timer_blocks_canvas: bool = True
    scrolling_text_restarts_when_redrawn: bool = True
    front_screen_width: int = 72
    front_screen_height: int = 16
    front_screen_encoding: str = "bgr888"
    back_screen_width: int = 80
    back_screen_height: int = 80
    back_screen_encoding: str = "gray8"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_KNOWN_PROFILES = {
    "1.1.1": "busybar-1.1.1-api25",
    "emulator-1.2.0": "founderos-emulator-1.2-api25",
}


def capabilities_for(firmware_version: str, api_semver: str) -> FirmwareCapabilities:
    """Return the safest known API 25 behavior for a firmware version."""

    version = str(firmware_version or "unknown").strip()
    semver = str(api_semver or "unknown").strip()
    profile = _KNOWN_PROFILES.get(version)
    if profile is None and re.search(r"(?:^|\D)1\.1\.1(?:\D|$)", version):
        profile = _KNOWN_PROFILES["1.1.1"]
    if profile is None:
        profile = "conservative-api25" if semver.split(".", 1)[0] == "25" else "unknown"
    return FirmwareCapabilities(
        firmware_version=version,
        api_semver=semver,
        profile=profile,
    )
