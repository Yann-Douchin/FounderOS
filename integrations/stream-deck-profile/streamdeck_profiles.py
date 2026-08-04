#!/usr/bin/env python3
"""Build, validate, install, and roll back the FounderOS Stream Deck profiles.

The builder reads existing Hue and Camera Hub action settings in memory so their
technical identifiers never need to be committed to the repository. Generated
profiles and backups are private artifacts and should remain outside version
control.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import plistlib
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_STREAMDECK_ROOT = Path.home() / "Library/Application Support/com.elgato.StreamDeck"
DEFAULT_PROFILES_ROOT = DEFAULT_STREAMDECK_ROOT / "ProfilesV3"
DEFAULT_PLUGINS_ROOT = DEFAULT_STREAMDECK_ROOT / "Plugins"
DEFAULT_APP_PLUGINS_ROOT = Path(
    "/Applications/Elgato Stream Deck.app/Contents/Resources/Plugins"
)
DEFAULT_CONTRACT = REPO_ROOT / "integrations/stream-deck/action-contract.json"
DEFAULT_PLAN = HERE / "profile-plan.json"
DEFAULT_ICON_ASSETS_ROOT = HERE / "assets/icons"
DEFAULT_DIST = Path.home() / "Library/Application Support/FounderOS/stream-deck-profiles"
DEFAULT_BACKUPS = DEFAULT_STREAMDECK_ROOT / "FounderOSBackups"
PREFERENCES_DOMAIN = "com.elgato.StreamDeck"

PROFILE_NAMESPACE = uuid.UUID("3f298d30-09de-4e94-99b6-929b513b24f9")
PROFILE_KEYS = ("cockpit", "call", "studio", "pedal", "presentation")
PROFILE_IDS = {
    key: str(uuid.uuid5(PROFILE_NAMESPACE, f"founderos-stream-deck-profile:{key}")).upper()
    for key in PROFILE_KEYS
}
PAGE_IDS = {
    key: str(uuid.uuid5(PROFILE_NAMESPACE, f"founderos-stream-deck-page:{key}")).upper()
    for key in PROFILE_KEYS
}
DEFAULT_PAGE_IDS = {
    key: str(
        uuid.uuid5(
            PROFILE_NAMESPACE,
            f"founderos-stream-deck-default-page-placeholder:{key}",
        )
    ).upper()
    for key in PROFILE_KEYS
}

REQUIRED_PLUGIN_UUIDS = (
    "com.yanndouchin.founderos-actions",
    "com.elgato.volume-controller",
    "com.elgato.philips-hue",
    "com.elgato.camerahub",
    "com.elgato.obsstudio",
)
DISALLOWED_PLUGIN_UUIDS = (
    "com.lostdomain.zoom",
    "com.microsoft.teams",
)
SMART_PROFILE_APPS = {
    "call": "/Applications/zoom.us.app",
    "studio": "/Applications/OBS.app",
}
TARGET_STREAM_DECK_VERSION = "7.5.1"
ICON_SIZE = 144
ICON_MAX_BYTES = 1_000_000
VISIBLE_CONTAINER_UUIDS = {
    "com.elgato.streamdeck.keys.stack",
    "com.elgato.streamdeck.dial.stack",
}

PROFILE_ICON_FILES = {
    "cockpit": (
        "cockpit-key-0-0-priority-open.png",
        "cockpit-key-1-0-priority-snooze.png",
        "cockpit-key-2-0-priority-handle.png",
        "cockpit-key-3-0-focus-50.png",
        "cockpit-key-0-1-things-capture.png",
        "cockpit-key-1-1-prepare-call.png",
        "cockpit-key-2-1-open-studio.png",
        "cockpit-key-3-1-end-session.png",
        "cockpit-dial-0-output-volume.png",
        "cockpit-dial-1-yeti-input.png",
        "cockpit-dial-2-work-lights.png",
        "cockpit-dial-3-mode-wheel.png",
        "cockpit-wheel-0-call.png",
        "cockpit-wheel-1-studio.png",
        "cockpit-wheel-2-writing.png",
        "cockpit-wheel-3-presentation.png",
        "cockpit-wheel-4-home.png",
    ),
    "call": (
        "call-key-0-0-mic-mute.png",
        "call-key-1-0-face-tracking.png",
        "call-key-2-0-screenbrush.png",
        "call-key-3-0-notes.png",
        "call-key-0-1-lights.png",
        "call-key-1-1-prompter-inactive.png",
        "call-key-1-1-prompter-active.png",
        "call-key-2-1-priority-open.png",
        "call-key-3-1-end-call.png",
        "call-dial-0-yeti-gain.png",
        "call-dial-1-output-volume.png",
        "call-dial-2-video-lights.png",
        "call-dial-3-prompter-inactive.png",
        "call-dial-3-prompter-active.png",
    ),
    "studio": (
        "studio-key-0-0-prepare-lights.png",
        "studio-key-1-0-record-start.png",
        "studio-key-2-0-record-pause.png",
        "studio-key-2-0-record-resume.png",
        "studio-key-3-0-obs-screenshot.png",
        "studio-key-0-1-screenbrush.png",
        "studio-key-1-1-face-tracking.png",
        "studio-key-2-1-prompter-inactive.png",
        "studio-key-2-1-prompter-active.png",
        "studio-key-3-1-record-end.png",
        "studio-dial-0-yeti-gain.png",
        "studio-dial-1-prompter-inactive.png",
        "studio-dial-1-prompter-active.png",
        "studio-dial-2-video-lights.png",
        "studio-dial-3-prompter-stack.png",
        "studio-stack-0-display-brightness.png",
        "studio-stack-1-prompter-inactive.png",
        "studio-stack-1-prompter-active.png",
        "studio-stack-2-scroll-speed.png",
    ),
    "pedal": (
        "pedal-0-voice-ptt.png",
        "pedal-1-things-capture.png",
        "pedal-2-screenbrush.png",
    ),
    "presentation": (
        "presentation-key-0-0-slide-previous.png",
        "presentation-key-1-0-slide-next.png",
        "presentation-key-2-0-prompter-inactive.png",
        "presentation-key-2-0-prompter-active.png",
        "presentation-key-0-1-speed-down.png",
        "presentation-key-1-1-speed-up.png",
        "presentation-key-2-1-screenbrush.png",
    ),
}
REQUIRED_ICON_FILES = tuple(
    filename
    for profile_key in PROFILE_KEYS
    for filename in PROFILE_ICON_FILES[profile_key]
)


class ProfileError(RuntimeError):
    """Raised when a profile cannot be built or installed safely."""


@dataclass(frozen=True)
class Paths:
    profiles_root: Path
    plugins_root: Path
    app_plugins_root: Path
    contract: Path
    plan: Path
    icon_assets_root: Path = DEFAULT_ICON_ASSETS_ROOT


@dataclass(frozen=True)
class DeviceRecord:
    model: str
    uuid: str
    profile_count: int


@dataclass(frozen=True)
class PluginRecord:
    uuid: str
    name: str
    version: str
    manifest_path: Path


@dataclass(frozen=True)
class BuiltProfile:
    key: str
    name: str
    profile_id: str
    model: str
    live_path: Path
    export_path: Path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Any, *, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if private:
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _png_metadata(path: Path) -> tuple[int, int, int, int]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"Unreadable PNG icon: {path}") from exc
    if len(payload) > ICON_MAX_BYTES:
        raise ProfileError(f"PNG icon exceeds the size limit: {path}")
    if len(payload) < 33 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ProfileError(f"Invalid PNG signature: {path}")
    if payload[12:16] != b"IHDR" or struct.unpack(">I", payload[8:12])[0] != 13:
        raise ProfileError(f"Invalid PNG header: {path}")
    width, height = struct.unpack(">II", payload[16:24])
    bit_depth = payload[24]
    color_type = payload[25]
    offset = 8
    saw_iend = False
    while offset + 12 <= len(payload):
        chunk_length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(payload):
            raise ProfileError(f"Truncated PNG structure: {path}")
        chunk_type = payload[offset + 4 : offset + 8]
        if chunk_type == b"acTL":
            raise ProfileError(f"Animated PNG icon is forbidden: {path}")
        if chunk_type == b"IEND":
            saw_iend = True
            break
        offset = chunk_end
    if not saw_iend:
        raise ProfileError(f"Missing PNG end marker: {path}")
    return width, height, bit_depth, color_type


def _validate_svg_master(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ProfileError(f"Missing or irregular SVG master: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileError(f"Unreadable SVG master: {path}") from exc
    if len(text.encode("utf-8")) > ICON_MAX_BYTES:
        raise ProfileError(f"SVG master exceeds the size limit: {path}")
    if (
        not text.isascii()
        or "\ufffd" in text
        or "\u2014" in text
        or unicodedata.normalize("NFC", text) != text
    ):
        raise ProfileError(f"SVG master has invalid Unicode text: {path}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ProfileError(f"Invalid SVG master: {path}") from exc
    if not root.tag.endswith("svg"):
        raise ProfileError(f"Missing SVG root element: {path}")
    if any(element.tag.endswith("text") for element in root.iter()):
        raise ProfileError(f"Text elements are forbidden in SVG icons: {path}")
    view_box = " ".join(str(root.attrib.get("viewBox", "")).replace(",", " ").split())
    if view_box != f"0 0 {ICON_SIZE} {ICON_SIZE}":
        raise ProfileError(f"Unexpected SVG viewBox: {path}")


def _validate_icon_suite(root: Path) -> dict[str, Path]:
    expected = set(REQUIRED_ICON_FILES)
    if len(REQUIRED_ICON_FILES) != 60 or len(expected) != 60:
        raise ProfileError("Icon contract is incomplete or duplicated")
    if any(
        len(filename) > 80
        or not filename.isascii()
        or filename != filename.lower()
        or Path(filename).name != filename
        for filename in expected
    ):
        raise ProfileError("Icon filenames must be safe lowercase ASCII names of 80 characters or less")
    if root.is_symlink() or not root.is_dir():
        raise ProfileError(f"Missing or irregular icon directory: {root}")
    actual_png = {path.name for path in root.glob("*.png") if path.is_file()}
    actual_svg = {path.name for path in root.glob("*.svg") if path.is_file()}
    expected_svg = {Path(filename).with_suffix(".svg").name for filename in expected}
    missing_png = sorted(expected - actual_png)
    unexpected_png = sorted(actual_png - expected)
    missing_svg = sorted(expected_svg - actual_svg)
    unexpected_svg = sorted(actual_svg - expected_svg)
    if missing_png or unexpected_png or missing_svg or unexpected_svg:
        details = []
        if missing_png:
            details.append("missing PNG files: " + ", ".join(missing_png))
        if unexpected_png:
            details.append("unexpected PNG files: " + ", ".join(unexpected_png))
        if missing_svg:
            details.append("missing SVG files: " + ", ".join(missing_svg))
        if unexpected_svg:
            details.append("unexpected SVG files: " + ", ".join(unexpected_svg))
        raise ProfileError("Inconsistent icon suite: " + "; ".join(details))
    paths: dict[str, Path] = {}
    for filename in REQUIRED_ICON_FILES:
        png = root / filename
        svg = png.with_suffix(".svg")
        if png.is_symlink() or not png.is_file():
            raise ProfileError(f"Missing or irregular PNG icon: {png}")
        width, height, bit_depth, color_type = _png_metadata(png)
        if (width, height, bit_depth, color_type) != (ICON_SIZE, ICON_SIZE, 8, 6):
            raise ProfileError(
                f"Expected a {ICON_SIZE} x {ICON_SIZE}, 8-bit RGBA PNG icon: {png}"
            )
        _validate_svg_master(svg)
        paths[Path(filename).stem] = png
    return paths


def _safe_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _is_stream_deck_running() -> bool:
    completed = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Stream Deck"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _resume_stream_deck_if_stopped() -> None:
    completed = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Stream Deck"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return
    for raw_pid in completed.stdout.splitlines():
        try:
            pid = int(raw_pid.strip())
        except ValueError:
            continue
        state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        ).stdout.strip()
        if state.startswith("T"):
            os.kill(pid, signal.SIGCONT)


def _stop_stream_deck(timeout: float = 15.0) -> None:
    if not _is_stream_deck_running():
        return
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'tell application "Elgato Stream Deck" to quit',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.monotonic() + timeout
    while _is_stream_deck_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _is_stream_deck_running():
        raise ProfileError("Stream Deck did not stop cleanly")


def _start_stream_deck() -> None:
    subprocess.run(
        ["/usr/bin/open", "-a", "Elgato Stream Deck"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _wait_for_stream_deck(timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not _is_stream_deck_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if not _is_stream_deck_running():
        raise ProfileError("Stream Deck did not restart")
    _resume_stream_deck_if_stopped()


def _empty_action() -> dict[str, Any]:
    return {"Actions": []}


def _new_action_id() -> str:
    return str(uuid.uuid4())


def _plugin_metadata(plugin: PluginRecord | None, fallback_uuid: str, fallback_name: str) -> dict[str, str]:
    if plugin is None:
        return {"Name": fallback_name, "UUID": fallback_uuid, "Version": "1.0.0.0"}
    return {"Name": plugin.name, "UUID": plugin.uuid, "Version": plugin.version}


def _state(title: str, image: str | None = None, *, font_size: int = 10) -> dict[str, Any]:
    value: dict[str, Any] = {
        "FontFamily": "",
        "FontSize": font_size,
        "FontStyle": "",
        "FontUnderline": False,
        "OutlineThickness": 2,
        "ShowTitle": True,
        "Title": _normalized_text(title),
        "TitleAlignment": "bottom",
        "TitleColor": "#ffffff",
    }
    if image:
        value["Image"] = image
    return value


def _action(
    action_uuid: str,
    name: str,
    title: str,
    *,
    settings: Mapping[str, Any] | None = None,
    plugin: Mapping[str, str] | None = None,
    image: str | None = None,
    state_count: int = 1,
) -> dict[str, Any]:
    states = [_state(title, image)]
    for _ in range(1, max(1, state_count)):
        states.append(_state(title, image))
    value: dict[str, Any] = {
        "ActionID": _new_action_id(),
        "LinkedTitle": True,
        "Name": name,
        "Resources": None,
        "Settings": dict(settings or {}),
        "State": 0,
        "States": states,
        "UUID": action_uuid,
    }
    if plugin:
        value["Plugin"] = dict(plugin)
    return value


def _website(url: str, title: str, image: str | None = None) -> dict[str, Any]:
    return _action(
        "com.elgato.streamdeck.system.website",
        "Website",
        title,
        settings={"openInBrowser": True, "path": url},
        image=image,
    )


def _open_app(path: str, title: str, image: str | None = None) -> dict[str, Any]:
    return _action(
        "com.elgato.streamdeck.system.open",
        "Open",
        title,
        settings={"path": json.dumps(path, ensure_ascii=False)},
        image=image,
    )


def _null_hotkey() -> dict[str, Any]:
    return {
        "KeyCmd": False,
        "KeyCtrl": False,
        "KeyModifiers": 0,
        "KeyOption": False,
        "KeyShift": False,
        "NativeCode": -1,
        "QTKeyCode": 33554431,
        "VKeyCode": -1,
    }


def _arrow_hotkey(direction: str, title: str, image: str | None = None) -> dict[str, Any]:
    keycodes = {
        "left": (123, 16777234),
        "right": (124, 16777236),
    }
    if direction not in keycodes:
        raise ProfileError(f"Unknown arrow direction: {direction}")
    native, qt = keycodes[direction]
    first = _null_hotkey()
    first.update({"NativeCode": native, "QTKeyCode": qt, "VKeyCode": native})
    return _action(
        "com.elgato.streamdeck.system.hotkey",
        "Hotkey",
        title,
        settings={"Coalesce": True, "Hotkeys": [first, _null_hotkey(), _null_hotkey(), _null_hotkey()]},
        image=image,
    )


def _option_space_hotkey(title: str, image: str | None = None) -> dict[str, Any]:
    """Return the exact macOS Option+Space hotkey used by superwhisper PTT.

    NativeCode and VKeyCode are Carbon key code 49. Space is invariant on the
    French AZERTY layout. A regular Hotkey action holds the chord until the
    physical pedal is released.
    """

    first = _null_hotkey()
    first.update(
        {
            "KeyModifiers": 4,
            "KeyOption": True,
            "NativeCode": 49,
            "QTKeyCode": 32,
            "VKeyCode": 49,
        }
    )
    return _action(
        "com.elgato.streamdeck.system.hotkey",
        "Hotkey",
        title,
        settings={
            "Coalesce": True,
            "Hotkeys": [first, _null_hotkey(), _null_hotkey(), _null_hotkey()],
        },
        image=image,
    )


def _switch_profile(
    profile_id: str,
    title: str,
    image: str | None = None,
    *,
    device_uuid: str = "",
) -> dict[str, Any]:
    return _action(
        "com.elgato.streamdeck.profile.rotate",
        "Switch Profile",
        title,
        settings={"DeviceUUID": device_uuid, "PageIndex": 0, "ProfileUUID": profile_id},
        image=image,
    )


def _multi_action(title: str, steps: Sequence[Sequence[dict[str, Any]]], image: str | None = None) -> dict[str, Any]:
    value = _action(
        "com.elgato.streamdeck.multiactions.routine",
        "Multi Action",
        title,
        settings={},
        plugin={"Name": "Multi Action", "UUID": "com.elgato.streamdeck.multiactions", "Version": "1.0"},
        image=image,
    )
    value["Actions"] = [{"Actions": list(actions)} for actions in steps]
    return value


def _hold_action(title: str, held_action: dict[str, Any], image: str | None = None) -> dict[str, Any]:
    value = _action(
        "com.elgato.streamdeck.keys.logic",
        "Key Logic",
        title,
        settings={},
        plugin={"Name": "Keys", "UUID": "com.elgato.streamdeck.keys", "Version": "1.0"},
        image=image,
    )
    value["Actions"] = [_empty_action(), _empty_action(), held_action]
    return value


def _action_wheel(
    title: str,
    actions: Sequence[dict[str, Any]],
    image: str | None = None,
) -> dict[str, Any]:
    value = _action(
        "com.elgato.streamdeck.keys.stack",
        "Action Wheel",
        title,
        settings={
            "ControlOffset": 0,
            "CurrentIdx": 0,
            "ExtTitles": False,
            "InvertRotation": False,
        },
        plugin={"Name": "Keys", "UUID": "com.elgato.streamdeck.keys", "Version": "1.0"},
        image=image,
    )
    value["Actions"] = list(actions)
    return value


def _dial_stack(
    title: str,
    actions: Sequence[dict[str, Any]],
    image: str | None = None,
) -> dict[str, Any]:
    value = _action(
        "com.elgato.streamdeck.dial.stack",
        "Dial Stack",
        title,
        settings={"CurrentIdx": 0, "FastNavigation": False, "ShowStackIcon": True},
        plugin={"Name": "Dials", "UUID": "com.elgato.streamdeck.dial", "Version": "1.0"},
        image=image,
    )
    value["Actions"] = list(actions)
    return value


def _iter_actions(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("UUID"), str):
            yield value
        for child in value.values():
            yield from _iter_actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_actions(child)


def _iter_visible_actions(page: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    controllers = page.get("Controllers", [])
    if not isinstance(controllers, list):
        return
    for controller in controllers:
        if not isinstance(controller, dict):
            continue
        actions = controller.get("Actions", {})
        if not isinstance(actions, dict):
            continue
        for action in actions.values():
            if not isinstance(action, dict):
                continue
            yield action
            if action.get("UUID") not in VISIBLE_CONTAINER_UUIDS:
                continue
            children = action.get("Actions", [])
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict):
                    yield child


def _action_titles(action: Mapping[str, Any]) -> set[str]:
    titles: set[str] = set()
    name = action.get("Name")
    if isinstance(name, str):
        titles.add(name)
    for state in action.get("States", []):
        if isinstance(state, dict) and isinstance(state.get("Title"), str):
            titles.add(state["Title"])
    return titles


def _retitle_action(action: dict[str, Any], title: str, image: str | None = None) -> dict[str, Any]:
    states = action.get("States")
    if not isinstance(states, list) or not states:
        action["States"] = [_state(title, image)]
    else:
        for state in states:
            if isinstance(state, dict):
                state.update(_state(title, image))
    return action


def _set_state_images(action: dict[str, Any], images: Sequence[str]) -> dict[str, Any]:
    states = action.get("States")
    if not isinstance(states, list) or len(states) != len(images):
        raise ProfileError(
            f"Inconsistent state image count for {action.get('Name', 'unknown action')}"
        )
    if len(set(images)) != len(images):
        raise ProfileError("Duplicate state images")
    for state, image in zip(states, images, strict=True):
        if not isinstance(state, dict):
            raise ProfileError("Invalid action state")
        state["Image"] = image
    return action


def _refresh_action_ids(value: Any) -> None:
    if isinstance(value, dict):
        if "ActionID" in value:
            value["ActionID"] = _new_action_id()
        for child in value.values():
            _refresh_action_ids(child)
    elif isinstance(value, list):
        for child in value:
            _refresh_action_ids(child)


def _strip_external_images(value: Any) -> None:
    if isinstance(value, dict):
        if "Image" in value:
            value.pop("Image", None)
        if "background" in value:
            value.pop("background", None)
        for child in value.values():
            _strip_external_images(child)
    elif isinstance(value, list):
        for child in value:
            _strip_external_images(child)


class LocalCatalog:
    """Read local device, plugin, and source action metadata without logging secrets."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._devices: dict[str, DeviceRecord] | None = None
        self._plugins: dict[str, PluginRecord] | None = None
        self._source_actions: list[dict[str, Any]] | None = None

    @property
    def devices(self) -> dict[str, DeviceRecord]:
        if self._devices is None:
            records: dict[str, DeviceRecord] = {}
            counts: dict[str, int] = {}
            values: dict[str, tuple[str, str]] = {}
            for manifest_path in sorted(self.paths.profiles_root.glob("*.sdProfile/manifest.json")):
                manifest = _read_json(manifest_path)
                device = manifest.get("Device", {})
                model = device.get("Model") if isinstance(device, dict) else None
                device_uuid = device.get("UUID") if isinstance(device, dict) else None
                if not isinstance(model, str) or not isinstance(device_uuid, str):
                    continue
                counts[model] = counts.get(model, 0) + 1
                values.setdefault(model, (model, device_uuid))
            for model, (model_value, device_uuid) in values.items():
                records[model] = DeviceRecord(model_value, device_uuid, counts[model])
            self._devices = records
        return self._devices

    @property
    def plugins(self) -> dict[str, PluginRecord]:
        if self._plugins is None:
            records: dict[str, PluginRecord] = {}
            roots = (self.paths.plugins_root, self.paths.app_plugins_root)
            for root in roots:
                if not root.is_dir():
                    continue
                for path in sorted(root.glob("*.sdPlugin/manifest.json")):
                    try:
                        manifest = _read_json(path)
                    except ProfileError:
                        try:
                            encrypted = path.read_bytes().startswith(b"ELGATO")
                        except OSError:
                            encrypted = False
                        if not encrypted:
                            raise
                        manifest = {}
                    plugin_uuid = manifest.get("UUID")
                    if not isinstance(plugin_uuid, str) or not plugin_uuid:
                        plugin_uuid = path.parent.name.removesuffix(".sdPlugin")
                    source_metadata = self._source_plugin_metadata(plugin_uuid)
                    records.setdefault(
                        plugin_uuid,
                        PluginRecord(
                            uuid=plugin_uuid,
                            name=str(
                                manifest.get("Name")
                                or source_metadata.get("Name")
                                or plugin_uuid
                            ),
                            version=str(
                                manifest.get("Version")
                                or source_metadata.get("Version")
                                or "1.0"
                            ),
                            manifest_path=path,
                        ),
                    )
            self._plugins = records
        return self._plugins

    def _source_plugin_metadata(self, plugin_uuid: str) -> Mapping[str, Any]:
        for action in self.source_actions:
            plugin = action.get("Plugin")
            if isinstance(plugin, dict) and plugin.get("UUID") == plugin_uuid:
                return plugin
        return {}

    @property
    def source_actions(self) -> list[dict[str, Any]]:
        if self._source_actions is None:
            actions: list[dict[str, Any]] = []
            for path in sorted(self.paths.profiles_root.glob("*.sdProfile/Profiles/*/manifest.json")):
                manifest = _read_json(path)
                controllers = manifest.get("Controllers", [])
                for action in _iter_actions(controllers):
                    actions.append(action)
            self._source_actions = actions
        return self._source_actions

    def clone_action(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        action_uuid: str | None = None,
        new_title: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for action in self.source_actions:
            if action_uuid and action.get("UUID") != action_uuid:
                continue
            if name and action.get("Name") != name:
                continue
            if title and title not in _action_titles(action):
                continue
            candidates.append(action)
        if not candidates:
            selector = title or name or action_uuid or "unknown action"
            raise ProfileError(f"Source action not found: {selector}")
        value = copy.deepcopy(candidates[0])
        _refresh_action_ids(value)
        _strip_external_images(value)
        value["Resources"] = None
        if new_title:
            _retitle_action(value, new_title, image)
        return value

    def indicator_light_values(self, forbidden_patterns: Sequence[str]) -> set[str]:
        values: set[str] = set()
        folded = tuple(pattern.casefold() for pattern in forbidden_patterns)
        for action in self.source_actions:
            titles = " ".join(_action_titles(action)).casefold()
            if not any(pattern in titles for pattern in folded):
                continue
            settings = action.get("Settings", {})
            if isinstance(settings, dict) and isinstance(settings.get("light"), str):
                values.add(settings["light"])
        return values


class ProfileBuilder:
    def __init__(self, paths: Paths, output: Path) -> None:
        self.paths = paths
        self.output = output.resolve()
        self.plan = _read_json(paths.plan)
        self.contract = _read_json(paths.contract)
        self.catalog = LocalCatalog(paths)
        self.models = self.plan.get("deviceModels", {})
        self.forbidden_patterns = tuple(self.plan.get("forbiddenActionTitlePatterns", []))
        self.profile_specs = self.plan.get("profiles", {})
        self._validate_contract()

    def _validate_contract(self) -> None:
        plugin = self.contract.get("plugin", {})
        actions = self.contract.get("actions", {})
        if plugin.get("uuid") != "com.yanndouchin.founderos-actions":
            raise ProfileError("Unexpected FounderOS plugin UUID")
        required = {"open", "snooze", "acknowledge", "presence"}
        if not required.issubset(actions):
            raise ProfileError("Incomplete FounderOS contract")
        presence = actions["presence"]
        presets = presence.get("presets", {})
        expected_presets = {
            "focus50",
            "manualCallStart",
            "manualCallStop",
            "recordingStart",
            "recordingStop",
            "recordingRenew",
            "releaseManual",
        }
        if not expected_presets.issubset(presets):
            raise ProfileError("Incomplete FounderOS presence presets")

    def safe_audit(self) -> dict[str, Any]:
        devices: dict[str, Any] = {}
        for semantic, model in self.models.items():
            record = self.catalog.devices.get(model)
            devices[semantic] = {
                "available": record is not None,
                "profileCount": record.profile_count if record else 0,
            }
        plugins = {
            plugin_uuid: {
                "installed": plugin_uuid in self.catalog.plugins,
                "version": self.catalog.plugins[plugin_uuid].version
                if plugin_uuid in self.catalog.plugins
                else None,
            }
            for plugin_uuid in REQUIRED_PLUGIN_UUIDS
        }
        source_checks: dict[str, bool] = {}
        selectors = {
            "hueFunctionalLighting": {"title": "Working Lights Brightness"},
            "hueRecordingLighting": {"title": "Recording Lights Brightness"},
            "cameraHubPrompter": {"name": "Prompter Control"},
            "azertyScreenBrush": {"title": "ScreenBrush"},
            "azertyFaceTracking": {"title": "Face Tracking"},
        }
        for key, selector in selectors.items():
            try:
                self.catalog.clone_action(**selector)
                source_checks[key] = True
            except ProfileError:
                source_checks[key] = False
        return {
            "schemaVersion": 1,
            "devices": devices,
            "plugins": plugins,
            "sourceChecks": source_checks,
            "activeProfilesModified": False,
            "sensitiveIdentifiersLogged": False,
        }

    def build(self) -> list[BuiltProfile]:
        self.output.mkdir(parents=True, exist_ok=True)
        self.output.chmod(0o700)
        live_root = _safe_private_directory(self.output / "live")
        export_root = _safe_private_directory(self.output / "exports")
        icon_assets = _validate_icon_suite(self.paths.icon_assets_root)
        icon_paths: dict[str, dict[str, str]] = {}
        for key in PROFILE_KEYS:
            icon_paths[key] = self._write_profile_icons(live_root, key, icon_assets)

        plus_model = self._model("streamDeckPlus")
        pedal_model = self._model("pedal")
        mobile_model = self._model("mobile")
        plus_uuid = self._device_uuid(plus_model)
        pedal_uuid = self._device_uuid(pedal_model)
        mobile_uuid = self._device_uuid(mobile_model)

        profiles = {
            "cockpit": self._cockpit_page(icon_paths["cockpit"], plus_uuid),
            "call": self._call_page(icon_paths["call"], plus_uuid),
            "studio": self._studio_page(icon_paths["studio"], plus_uuid),
            "pedal": self._pedal_page(icon_paths["pedal"]),
            "presentation": self._presentation_page(icon_paths["presentation"]),
        }
        models = {
            "cockpit": plus_model,
            "call": plus_model,
            "studio": plus_model,
            "pedal": pedal_model,
            "presentation": mobile_model,
        }
        device_uuids = {
            "cockpit": plus_uuid,
            "call": plus_uuid,
            "studio": plus_uuid,
            "pedal": pedal_uuid,
            "presentation": mobile_uuid,
        }

        built: list[BuiltProfile] = []
        for key in PROFILE_KEYS:
            name = str(self.profile_specs[key]["name"])
            live_path = live_root / f"{PROFILE_IDS[key]}.sdProfile"
            self._write_profile_bundle(
                live_path,
                key=key,
                name=name,
                model=models[key],
                device_uuid=device_uuids[key],
                page=profiles[key],
                icon_source=live_root / f".{key}-icons",
            )
            export_profile = self.output / f".{key}-portable" / f"{PROFILE_IDS[key]}.sdProfile"
            self._write_profile_bundle(
                export_profile,
                key=key,
                name=name,
                model=models[key],
                device_uuid="",
                page=copy.deepcopy(profiles[key]),
                icon_source=live_root / f".{key}-icons",
            )
            export_path = export_root / f"{name}.streamDeckProfile"
            self._zip_profile(export_profile, export_path)
            built.append(
                BuiltProfile(
                    key=key,
                    name=name,
                    profile_id=PROFILE_IDS[key],
                    model=models[key],
                    live_path=live_path,
                    export_path=export_path,
                )
            )

        self.validate(live_root)
        report = {
            "schemaVersion": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "profiles": [
                {
                    "key": item.key,
                    "name": item.name,
                    "profileId": item.profile_id,
                    "model": item.model,
                    "export": item.export_path.name,
                }
                for item in built
            ],
            "dependencies": self.safe_audit()["plugins"],
            "containsPrivateHardwareSettings": True,
            "activeProfilesModified": False,
        }
        _write_json(self.output / "build-report.json", report)
        return built

    def _model(self, key: str) -> str:
        value = self.models.get(key)
        if not isinstance(value, str) or not value:
            raise ProfileError(f"Model missing from plan: {key}")
        return value

    def _device_uuid(self, model: str) -> str:
        record = self.catalog.devices.get(model)
        if record is None:
            raise ProfileError(f"Local device not found for model {model}")
        return record.uuid

    def _icon(self, icons: Mapping[str, str], key: str) -> str:
        return icons[key]

    def _founderos_action(
        self,
        key: str,
        title: str,
        image: str | None = None,
        *,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plugin_info = self.contract["plugin"]
        action_info = self.contract["actions"][key]
        plugin = self.catalog.plugins.get(plugin_info["uuid"])
        return _action(
            action_info["uuid"],
            title,
            title,
            settings=settings,
            plugin=_plugin_metadata(plugin, plugin_info["uuid"], plugin_info["name"]),
            image=image,
        )

    def _presence(self, preset: str, title: str, image: str | None = None) -> dict[str, Any]:
        presence = self.contract["actions"]["presence"]
        if preset not in presence["presets"]:
            raise ProfileError(f"Unknown FounderOS preset: {preset}")
        return self._founderos_action(
            "presence",
            title,
            image,
            settings={"schemaVersion": presence["settingsSchemaVersion"], "preset": preset},
        )

    def _plugin_action(
        self,
        plugin_uuid: str,
        action_uuid: str,
        name: str,
        title: str,
        *,
        settings: Mapping[str, Any] | None = None,
        image: str | None = None,
        state_count: int = 1,
    ) -> dict[str, Any]:
        plugin = self.catalog.plugins.get(plugin_uuid)
        return _action(
            action_uuid,
            name,
            title,
            settings=settings,
            plugin=_plugin_metadata(plugin, plugin_uuid, name),
            image=image,
            state_count=state_count,
        )

    def _output_dial(self, image: str | None = None) -> dict[str, Any]:
        return self._plugin_action(
            "com.elgato.volume-controller",
            "com.elgato.volume-controller.output-device-control",
            "Output Device Control",
            "Volume and output",
            settings={
                "action": "adjust",
                "deviceId": "default",
                "friendlyName": "Default output",
                "style": "vertical",
                "volumeStep": "3",
            },
            image=image,
        )

    def _input_control(
        self,
        title: str = "Yeti X",
        *,
        keypad: bool = False,
        image: str | None = None,
    ) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "action": "mute" if keypad else "adjust",
            "deviceId": "default",
            "friendlyName": "Yeti X",
            "style": "vertical",
            "volumeStep": "3",
        }
        return self._plugin_action(
            "com.elgato.volume-controller",
            "com.elgato.volume-controller.input-device-control",
            "Input Device Control",
            title,
            settings=settings,
            image=image,
        )

    def _obs_action(self, action_suffix: str, title: str, image: str | None = None) -> dict[str, Any]:
        action_uuid = f"com.elgato.obsstudio.{action_suffix}"
        state_count = 2 if action_suffix in {"record", "record.pause", "virtualcam"} else 1
        return self._plugin_action(
            "com.elgato.obsstudio",
            action_uuid,
            title,
            title,
            image=image,
            state_count=state_count,
        )

    def _functional_recording_lights(self, title: str, image: str | None = None) -> dict[str, Any]:
        return _multi_action(title, [self._recording_light_actions()], image)

    def _recording_light_actions(self) -> list[dict[str, Any]]:
        power = self.catalog.clone_action(
            title="Recording Lights On",
            action_uuid="com.elgato.philips-hue.power",
            new_title="Video lights",
        )
        scene = self.catalog.clone_action(
            title="Recording Lights Bright",
            action_uuid="com.elgato.philips-hue.scene",
            new_title="Video scene",
        )
        return [power, scene]

    def _working_light_actions(self) -> list[dict[str, Any]]:
        power = self.catalog.clone_action(
            title="Working Lights On",
            action_uuid="com.elgato.philips-hue.power",
            new_title="Task lights",
        )
        scene = self.catalog.clone_action(
            title="Working Lights Bright",
            action_uuid="com.elgato.philips-hue.scene",
            new_title="Task scene",
        )
        return [power, scene]

    def _return_to_cockpit(
        self,
        title: str,
        image: str | None = None,
        *,
        device_uuid: str,
    ) -> dict[str, Any]:
        return _switch_profile(
            PROFILE_IDS["cockpit"],
            title,
            image,
            device_uuid=device_uuid,
        )

    def _cockpit_page(self, icons: Mapping[str, str], device_uuid: str) -> dict[str, Any]:
        open_priority = self._founderos_action(
            "open",
            "Open\npriority",
            self._icon(icons, "cockpit-key-0-0-priority-open"),
        )
        snooze = self._founderos_action(
            "snooze",
            "Snooze\n15 min",
            self._icon(icons, "cockpit-key-1-0-priority-snooze"),
        )
        acknowledge = self._founderos_action(
            "acknowledge",
            "Hold\nAcknowledge",
            self._icon(icons, "cockpit-key-2-0-priority-handle"),
        )
        focus = self._presence(
            "focus50",
            "Focus\n50 min",
            self._icon(icons, "cockpit-key-3-0-focus-50"),
        )
        capture = _website(
            "things:///add?show-quick-entry=true",
            "Capture",
            self._icon(icons, "cockpit-key-0-1-things-capture"),
        )
        prepare_call = _multi_action(
            "Prepare\ncall",
            [
                [
                    self._presence("manualCallStart", "Manual call"),
                    *self._recording_light_actions(),
                ],
                [_switch_profile(PROFILE_IDS["call"], "Call mode", device_uuid=device_uuid)],
            ],
            self._icon(icons, "cockpit-key-1-1-prepare-call"),
        )
        studio = _multi_action(
            "Studio",
            [
                [_open_app("/Applications/OBS.app", "Open OBS")],
                [_switch_profile(PROFILE_IDS["studio"], "Studio mode", device_uuid=device_uuid)],
            ],
            self._icon(icons, "cockpit-key-2-1-open-studio"),
        )
        release = self._presence("releaseManual", "Release manual states")
        finish = _hold_action(
            "Hold\nFinish",
            _multi_action("Finish", [[release], self._working_light_actions()]),
            self._icon(icons, "cockpit-key-3-1-end-session"),
        )

        hue = self.catalog.clone_action(
            title="Working Lights Brightness",
            action_uuid="com.elgato.philips-hue.brightness",
            new_title="Task lighting",
            image=self._icon(icons, "cockpit-dial-2-work-lights"),
        )
        wheel = _action_wheel(
            "Modes",
            [
                _switch_profile(
                    PROFILE_IDS["call"],
                    "Call",
                    self._icon(icons, "cockpit-wheel-0-call"),
                    device_uuid=device_uuid,
                ),
                _switch_profile(
                    PROFILE_IDS["studio"],
                    "Studio",
                    self._icon(icons, "cockpit-wheel-1-studio"),
                    device_uuid=device_uuid,
                ),
                _open_app(
                    "/Applications/Notion.app",
                    "Writing",
                    self._icon(icons, "cockpit-wheel-2-writing"),
                ),
                _open_app(
                    "/Applications/Keynote.app",
                    "Presentation",
                    self._icon(icons, "cockpit-wheel-3-presentation"),
                ),
                _open_app(
                    "/System/Applications/Home.app",
                    "Home",
                    self._icon(icons, "cockpit-wheel-4-home"),
                ),
            ],
            self._icon(icons, "cockpit-dial-3-mode-wheel"),
        )
        return self._page(
            keypad={
                "0,0": open_priority,
                "1,0": snooze,
                "2,0": acknowledge,
                "3,0": focus,
                "0,1": capture,
                "1,1": prepare_call,
                "2,1": studio,
                "3,1": finish,
            },
            encoders={
                "0,0": self._output_dial(
                    self._icon(icons, "cockpit-dial-0-output-volume")
                ),
                "1,0": self._input_control(
                    image=self._icon(icons, "cockpit-dial-1-yeti-input")
                ),
                "2,0": hue,
                "3,0": wheel,
            },
        )

    def _call_page(self, icons: Mapping[str, str], device_uuid: str) -> dict[str, Any]:
        micro = self._input_control(
            "Microphone",
            keypad=True,
            image=self._icon(icons, "call-key-0-0-mic-mute"),
        )
        tracking = self.catalog.clone_action(
            title="Face Tracking",
            new_title="Camera\ntracking",
            image=self._icon(icons, "call-key-1-0-face-tracking"),
        )
        screenbrush = self.catalog.clone_action(
            title="ScreenBrush",
            new_title="ScreenBrush",
            image=self._icon(icons, "call-key-2-0-screenbrush"),
        )
        notes = _website(
            "things:///add?show-quick-entry=true",
            "Notes",
            self._icon(icons, "call-key-3-0-notes"),
        )
        lights = _multi_action(
            "Lights",
            [[
                self._presence("manualCallStart", "Start or renew call"),
                *self._recording_light_actions(),
            ]],
            self._icon(icons, "call-key-0-1-lights"),
        )
        prompter = self.catalog.clone_action(
            name="Prompter Control",
            action_uuid="com.elgato.camerahub.promptercontrol",
            new_title="Prompter",
        )
        _set_state_images(
            prompter,
            [
                self._icon(icons, "call-key-1-1-prompter-inactive"),
                self._icon(icons, "call-key-1-1-prompter-active"),
            ],
        )
        priority = self._founderos_action(
            "open",
            "Open\npriority",
            self._icon(icons, "call-key-2-1-priority-open"),
        )
        end_steps = [
            [self._presence("manualCallStop", "End call")],
            self._working_light_actions(),
            [self._return_to_cockpit("Return to cockpit", device_uuid=device_uuid)],
        ]
        end = _hold_action(
            "Hold\nEnd",
            _multi_action("End", end_steps),
            self._icon(icons, "call-key-3-1-end-call"),
        )
        hue = self.catalog.clone_action(
            title="Recording Lights Brightness",
            action_uuid="com.elgato.philips-hue.brightness",
            new_title="Video lights",
            image=self._icon(icons, "call-dial-2-video-lights"),
        )
        prompter_dial = self.catalog.clone_action(
            name="Prompter Control",
            action_uuid="com.elgato.camerahub.promptercontrol",
            new_title="Prompter",
        )
        _set_state_images(
            prompter_dial,
            [
                self._icon(icons, "call-dial-3-prompter-inactive"),
                self._icon(icons, "call-dial-3-prompter-active"),
            ],
        )
        return self._page(
            keypad={
                "0,0": micro,
                "1,0": tracking,
                "2,0": screenbrush,
                "3,0": notes,
                "0,1": lights,
                "1,1": prompter,
                "2,1": priority,
                "3,1": end,
            },
            encoders={
                "0,0": self._input_control(
                    "Yeti X gain",
                    image=self._icon(icons, "call-dial-0-yeti-gain"),
                ),
                "1,0": self._output_dial(
                    self._icon(icons, "call-dial-1-output-volume")
                ),
                "2,0": hue,
                "3,0": prompter_dial,
            },
        )

    def _studio_page(self, icons: Mapping[str, str], device_uuid: str) -> dict[str, Any]:
        prepare = self._functional_recording_lights(
            "Prepare",
            self._icon(icons, "studio-key-0-0-prepare-lights"),
        )
        start_recording = _multi_action(
            "Start REC",
            [[
                self._obs_action("record", "Record"),
                self._presence("recordingStart", "Recording lease"),
            ]],
        )
        record = _hold_action(
            "Hold\nStart REC",
            start_recording,
            self._icon(icons, "studio-key-1-0-record-start"),
        )
        pause = self._obs_action("record.pause", "Pause")
        _set_state_images(
            pause,
            [
                self._icon(icons, "studio-key-2-0-record-pause"),
                self._icon(icons, "studio-key-2-0-record-resume"),
            ],
        )
        capture = self._obs_action(
            "screenshot",
            "OBS capture",
            self._icon(icons, "studio-key-3-0-obs-screenshot"),
        )
        screenbrush = self.catalog.clone_action(
            title="ScreenBrush",
            new_title="ScreenBrush",
            image=self._icon(icons, "studio-key-0-1-screenbrush"),
        )
        tracking = self.catalog.clone_action(
            title="Face Tracking",
            new_title="Camera\ntracking",
            image=self._icon(icons, "studio-key-1-1-face-tracking"),
        )
        prompter = self.catalog.clone_action(
            name="Prompter Control",
            action_uuid="com.elgato.camerahub.promptercontrol",
            new_title="Prompter",
        )
        _set_state_images(
            prompter,
            [
                self._icon(icons, "studio-key-2-1-prompter-inactive"),
                self._icon(icons, "studio-key-2-1-prompter-active"),
            ],
        )
        stop_recording = _multi_action(
            "End",
            [
                [self._obs_action("record", "Stop REC")],
                [self._presence("recordingStop", "Release recording")],
                self._working_light_actions(),
                [_switch_profile(PROFILE_IDS["cockpit"], "Return to cockpit", device_uuid=device_uuid)],
            ],
        )
        finish = _hold_action(
            "Hold\nEnd",
            stop_recording,
            self._icon(icons, "studio-key-3-1-record-end"),
        )

        display_dial = self.catalog.clone_action(
            name="Prompter Display",
            action_uuid="com.elgato.camerahub.prompterdisplaysettings",
            new_title="Prompter display",
            image=self._icon(icons, "studio-stack-0-display-brightness"),
        )
        control_dial = self.catalog.clone_action(
            name="Prompter Control",
            action_uuid="com.elgato.camerahub.promptercontrol",
            new_title="Prompter control",
        )
        _set_state_images(
            control_dial,
            [
                self._icon(icons, "studio-dial-1-prompter-inactive"),
                self._icon(icons, "studio-dial-1-prompter-active"),
            ],
        )
        scrolling_dial = self.catalog.clone_action(
            name="Prompter Scrolling",
            action_uuid="com.elgato.camerahub.prompterscrollingsettings",
            new_title="Prompter speed",
            image=self._icon(icons, "studio-stack-2-scroll-speed"),
        )
        stack_control_dial = copy.deepcopy(control_dial)
        _refresh_action_ids(stack_control_dial)
        _set_state_images(
            stack_control_dial,
            [
                self._icon(icons, "studio-stack-1-prompter-inactive"),
                self._icon(icons, "studio-stack-1-prompter-active"),
            ],
        )
        prompter_stack = _dial_stack(
            "Prompter",
            [display_dial, stack_control_dial, scrolling_dial],
            self._icon(icons, "studio-dial-3-prompter-stack"),
        )
        hue = self.catalog.clone_action(
            title="Recording Lights Brightness",
            action_uuid="com.elgato.philips-hue.brightness",
            new_title="Video lights",
            image=self._icon(icons, "studio-dial-2-video-lights"),
        )
        return self._page(
            keypad={
                "0,0": prepare,
                "1,0": record,
                "2,0": pause,
                "3,0": capture,
                "0,1": screenbrush,
                "1,1": tracking,
                "2,1": prompter,
                "3,1": finish,
            },
            encoders={
                "0,0": self._input_control(
                    "Yeti X gain",
                    image=self._icon(icons, "studio-dial-0-yeti-gain"),
                ),
                "1,0": control_dial,
                "2,0": hue,
                "3,0": prompter_stack,
            },
        )

    def _pedal_page(self, icons: Mapping[str, str]) -> dict[str, Any]:
        voice = _option_space_hotkey(
            "Voice",
            self._icon(icons, "pedal-0-voice-ptt"),
        )
        capture = _website(
            "things:///add?show-quick-entry=true",
            "Capture",
            self._icon(icons, "pedal-1-things-capture"),
        )
        visual = self.catalog.clone_action(
            title="ScreenBrush",
            new_title="Visual",
            image=self._icon(icons, "pedal-2-screenbrush"),
        )
        return self._page(keypad={"0,0": voice, "1,0": capture, "2,0": visual})

    def _presentation_page(self, icons: Mapping[str, str]) -> dict[str, Any]:
        previous = _arrow_hotkey(
            "left",
            "Previous\nslide",
            self._icon(icons, "presentation-key-0-0-slide-previous"),
        )
        following = _arrow_hotkey(
            "right",
            "Next\nslide",
            self._icon(icons, "presentation-key-1-0-slide-next"),
        )
        prompter = self.catalog.clone_action(
            name="Prompter Control",
            action_uuid="com.elgato.camerahub.promptercontrol",
            new_title="Prompter\nplay / pause",
        )
        _set_state_images(
            prompter,
            [
                self._icon(icons, "presentation-key-2-0-prompter-inactive"),
                self._icon(icons, "presentation-key-2-0-prompter-active"),
            ],
        )
        slower = self.catalog.clone_action(
            name="Prompter Scrolling",
            action_uuid="com.elgato.camerahub.prompterscrollingsettings",
            new_title="Speed -",
            image=self._icon(icons, "presentation-key-0-1-speed-down"),
        )
        slower.setdefault("Settings", {}).update({"actionType": "adjust", "valueAdjustment": -1})
        faster = self.catalog.clone_action(
            name="Prompter Scrolling",
            action_uuid="com.elgato.camerahub.prompterscrollingsettings",
            new_title="Speed +",
            image=self._icon(icons, "presentation-key-1-1-speed-up"),
        )
        faster.setdefault("Settings", {}).update({"actionType": "adjust", "valueAdjustment": 1})
        screenbrush = self.catalog.clone_action(
            title="ScreenBrush",
            new_title="ScreenBrush",
            image=self._icon(icons, "presentation-key-2-1-screenbrush"),
        )
        return self._page(
            keypad={
                "0,0": previous,
                "1,0": following,
                "2,0": prompter,
                "0,1": slower,
                "1,1": faster,
                "2,1": screenbrush,
            }
        )

    @staticmethod
    def _page(
        *,
        keypad: Mapping[str, dict[str, Any]],
        encoders: Mapping[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        controllers: list[dict[str, Any]] = [{"Actions": dict(keypad), "Type": "Keypad"}]
        if encoders is not None:
            controllers.append({"Actions": dict(encoders), "Type": "Encoder"})
        return {"Controllers": controllers, "Icon": "", "Name": ""}

    def _write_profile_icons(
        self,
        live_root: Path,
        key: str,
        icon_assets: Mapping[str, Path],
    ) -> dict[str, str]:
        icon_root = live_root / f".{key}-icons"
        icon_root.mkdir(parents=True, exist_ok=True)
        icon_root.chmod(0o700)
        result: dict[str, str] = {}
        for filename in PROFILE_ICON_FILES[key]:
            stem = Path(filename).stem
            source = icon_assets.get(stem)
            if source is None:
                raise ProfileError(f"Icon is missing from the validated catalog: {filename}")
            target = icon_root / filename
            shutil.copy2(source, target)
            target.chmod(0o600)
            result[stem] = f"Images/{filename}"
        return result

    def _write_profile_bundle(
        self,
        destination: Path,
        *,
        key: str,
        name: str,
        model: str,
        device_uuid: str,
        page: dict[str, Any],
        icon_source: Path,
    ) -> None:
        if destination.exists():
            shutil.rmtree(destination)
        page_id = PAGE_IDS[key]
        page_dir = destination / "Profiles" / page_id
        images_dir = page_dir / "Images"
        images_dir.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o700)
        page_dir.chmod(0o700)
        images_dir.chmod(0o700)
        for source in sorted(icon_source.glob("*.png")):
            target = images_dir / source.name
            shutil.copy2(source, target)
            target.chmod(0o600)
        outer = {
            "Device": {"Model": model, "UUID": device_uuid},
            "Name": name,
            "Pages": {
                "Current": page_id.lower(),
                "Default": DEFAULT_PAGE_IDS[key].lower(),
                "Pages": [page_id.lower()],
            },
            "Version": "3.0",
        }
        if key in SMART_PROFILE_APPS:
            outer["AppIdentifier"] = SMART_PROFILE_APPS[key]
        _write_json(destination / "manifest.json", outer)
        _write_json(page_dir / "manifest.json", page)

    @staticmethod
    def _zip_profile(profile: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(profile.rglob("*")):
                if not path.is_file():
                    continue
                arcname = Path(profile.name) / path.relative_to(profile)
                info = zipfile.ZipInfo(str(arcname).replace(os.sep, "/"))
                info.date_time = (2026, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, path.read_bytes())
        temporary.chmod(0o600)
        os.replace(temporary, destination)

    def validate(
        self,
        profiles_root: Path,
        *,
        allow_materialized_defaults: bool = False,
    ) -> dict[str, Any]:
        problems: list[str] = []
        found: dict[str, Path] = {}
        action_ids: set[str] = set()
        source_icons = _validate_icon_suite(self.paths.icon_assets_root)
        icon_references: set[str] = set()
        visible_action_count = 0
        indicator_values = self.catalog.indicator_light_values(self.forbidden_patterns)
        referenced_plugins: set[str] = set()
        counts: dict[str, dict[str, int]] = {}
        for key in PROFILE_KEYS:
            profile_path = profiles_root / f"{PROFILE_IDS[key]}.sdProfile"
            found[key] = profile_path
            if not profile_path.is_dir():
                problems.append(f"Missing profile: {key}")
                continue
            outer = _read_json(profile_path / "manifest.json")
            if outer.get("Name") != self.profile_specs[key]["name"]:
                problems.append(f"Unexpected profile name: {key}")
            if outer.get("Version") != "3.0":
                problems.append(f"Unexpected profile version: {key}")
            pages = outer.get("Pages", {})
            page_list = pages.get("Pages") if isinstance(pages, dict) else None
            if (
                not isinstance(pages, dict)
                or not isinstance(page_list, list)
                or pages.get("Current") not in page_list
                or pages.get("Default") in page_list
                or pages.get("Current") == pages.get("Default")
            ):
                problems.append(f"Inconsistent default page: {key}")
            expected_app = SMART_PROFILE_APPS.get(key)
            if expected_app and outer.get("AppIdentifier") != expected_app:
                problems.append(f"Missing or inconsistent Smart Profile: {key}")
            if not expected_app and "AppIdentifier" in outer:
                problems.append(f"Unexpected Smart Profile: {key}")
            page_paths = list(profile_path.glob("Profiles/*/manifest.json"))
            current_page_id = pages.get("Current") if isinstance(pages, dict) else None
            current_paths = [
                path
                for path in page_paths
                if isinstance(current_page_id, str)
                and path.parent.name.casefold() == current_page_id.casefold()
            ]
            if len(current_paths) != 1:
                problems.append(f"Expected exactly one current page: {key}")
                continue
            extra_paths = [path for path in page_paths if path not in current_paths]
            if extra_paths:
                default_page_id = pages.get("Default") if isinstance(pages, dict) else None
                valid_materialized_default = (
                    allow_materialized_defaults
                    and len(extra_paths) == 1
                    and isinstance(default_page_id, str)
                    and extra_paths[0].parent.name.casefold() == default_page_id.casefold()
                )
                if valid_materialized_default:
                    default_page = _read_json(extra_paths[0])
                    default_controllers = default_page.get("Controllers", [])
                    if not isinstance(default_controllers, list) or any(
                        not isinstance(controller, dict)
                        or controller.get("Actions") not in (None, {})
                        for controller in default_controllers
                    ):
                        valid_materialized_default = False
                    if list(_iter_actions(default_page)):
                        valid_materialized_default = False
                    default_images = extra_paths[0].parent / "Images"
                    if default_images.is_dir() and any(default_images.iterdir()):
                        valid_materialized_default = False
                if not valid_materialized_default:
                    problems.append(f"Unexpected additional page: {key}")
            page_path = current_paths[0]
            page = _read_json(page_path)
            controller_counts: dict[str, int] = {}
            for controller in page.get("Controllers", []):
                if not isinstance(controller, dict):
                    continue
                controller_type = str(controller.get("Type"))
                actions = controller.get("Actions", {})
                controller_counts[controller_type] = len(actions) if isinstance(actions, dict) else 0
            counts[key] = controller_counts
            expected_files = set(PROFILE_ICON_FILES[key])
            images_root = page_path.parent / "Images"
            actual_files = {
                path.name
                for path in images_root.glob("*.png")
                if path.is_file() and not path.is_symlink()
            }
            if actual_files != expected_files:
                problems.append(f"Inconsistent embedded icon suite for {key}")
            for filename in sorted(expected_files):
                image_path = images_root / filename
                try:
                    metadata = _png_metadata(image_path)
                except ProfileError as exc:
                    problems.append(str(exc))
                    continue
                if metadata != (ICON_SIZE, ICON_SIZE, 8, 6):
                    problems.append(f"Inconsistent embedded icon format: {filename}")
                source_path = source_icons.get(Path(filename).stem)
                if source_path is None or _sha256(image_path) != _sha256(source_path):
                    problems.append(f"Embedded icon differs from its source: {filename}")
            for action in _iter_visible_actions(page):
                visible_action_count += 1
                states = action.get("States")
                if not isinstance(states, list) or not states:
                    problems.append("Visible surface has no icon state")
                    continue
                state_digests: list[str] = []
                for state in states:
                    image = state.get("Image") if isinstance(state, dict) else None
                    if not isinstance(image, str):
                        problems.append("Visible surface has no dedicated icon")
                        continue
                    image_path = Path(image)
                    if (
                        image_path.parent != Path("Images")
                        or image_path.name not in expected_files
                    ):
                        problems.append(f"Unexpected visible icon reference: {image}")
                    if image in icon_references:
                        problems.append(f"Reused visible icon reference: {image}")
                    icon_references.add(image)
                    embedded_path = images_root / image_path.name
                    if embedded_path.is_file():
                        state_digests.append(_sha256(embedded_path))
                if len(state_digests) != len(set(state_digests)):
                    problems.append("Visible states have identical icon content")
                if action.get("UUID") in VISIBLE_CONTAINER_UUIDS:
                    container_digests = list(state_digests)
                    children = action.get("Actions", [])
                    if isinstance(children, list):
                        for child in children:
                            if not isinstance(child, dict):
                                continue
                            for child_state in child.get("States", []):
                                child_image = (
                                    child_state.get("Image")
                                    if isinstance(child_state, dict)
                                    else None
                                )
                                if not isinstance(child_image, str):
                                    continue
                                child_path = images_root / Path(child_image).name
                                if child_path.is_file():
                                    container_digests.append(_sha256(child_path))
                    if len(container_digests) != len(set(container_digests)):
                        problems.append(
                            "A wheel or stack wrapper and choice have identical icon content"
                        )
            for action in _iter_actions(page):
                action_id = action.get("ActionID")
                if isinstance(action_id, str):
                    if action_id in action_ids:
                        problems.append("Duplicate ActionID")
                    action_ids.add(action_id)
                action_uuid = action.get("UUID")
                if isinstance(action_uuid, str):
                    referenced_plugins.add(action_uuid)
                    if any(action_uuid.startswith(prefix) for prefix in DISALLOWED_PLUGIN_UUIDS):
                        problems.append(f"Forbidden dependency: {action_uuid}")
                    if action_uuid == "com.elgato.streamdeck.multiactions.routine":
                        nested = [
                            child
                            for child in _iter_actions(action.get("Actions", []))
                            if child.get("UUID") == "com.elgato.streamdeck.multiactions.routine"
                        ]
                        if nested:
                            problems.append("Nested Multi Action is forbidden")
                searchable = " ".join(_action_titles(action)).casefold()
                if any(pattern.casefold() in searchable for pattern in self.forbidden_patterns):
                    problems.append("Forbidden availability indicator action")
                settings = action.get("Settings", {})
                if (
                    isinstance(settings, dict)
                    and isinstance(settings.get("light"), str)
                    and settings["light"] in indicator_values
                ):
                    problems.append("Availability indicator Hue identifier detected")
        expected_counts = {
            "cockpit": {"Keypad": 8, "Encoder": 4},
            "call": {"Keypad": 8, "Encoder": 4},
            "studio": {"Keypad": 8, "Encoder": 4},
            "pedal": {"Keypad": 3},
            "presentation": {"Keypad": 6},
        }
        for key, expected in expected_counts.items():
            if counts.get(key) != expected:
                problems.append(f"Unexpected action count for {key}: {counts.get(key)}")
        pedal_page = _read_json(
            found["pedal"] / "Profiles" / PAGE_IDS["pedal"] / "manifest.json"
        )
        pedal_hotkeys = [
            action
            for action in _iter_actions(pedal_page)
            if action.get("UUID") == "com.elgato.streamdeck.system.hotkey"
            and "Voice" in _action_titles(action)
        ]
        if len(pedal_hotkeys) != 1:
            problems.append("Missing or duplicate superwhisper shortcut")
        else:
            hotkeys = pedal_hotkeys[0].get("Settings", {}).get("Hotkeys", [])
            first = hotkeys[0] if isinstance(hotkeys, list) and hotkeys else {}
            expected_modifiers = {
                "KeyCmd": False,
                "KeyCtrl": False,
                "KeyModifiers": 4,
                "KeyOption": True,
                "KeyShift": False,
                "NativeCode": 49,
                "QTKeyCode": 32,
                "VKeyCode": 49,
            }
            if not isinstance(first, dict) or any(
                first.get(field) != expected for field, expected in expected_modifiers.items()
            ):
                problems.append("Inconsistent superwhisper Option+Space shortcut")
        all_text = "\n".join(
            path.read_text(encoding="utf-8")
            for profile in found.values()
            if profile.is_dir()
            for path in profile.rglob("*.json")
        )
        if "\ufffd" in all_text:
            problems.append("Unicode replacement character detected")
        if "\u2014" in all_text:
            problems.append("Forbidden em dash detected")
        if unicodedata.normalize("NFC", all_text) != all_text:
            problems.append("Text is not NFC-normalized")
        expected_references = {
            f"Images/{filename}"
            for filename in REQUIRED_ICON_FILES
        }
        if visible_action_count != 53:
            problems.append(
                f"Unexpected visible surface count: {visible_action_count}"
            )
        if icon_references != expected_references:
            problems.append(
                f"Unexpected dedicated icon coverage: {len(icon_references)} of 60"
            )
        if problems:
            raise ProfileError("Validation failed: " + "; ".join(sorted(set(problems))))
        return {
            "schemaVersion": 1,
            "valid": True,
            "profiles": counts,
            "profileCount": len(found),
            "actionIdCount": len(action_ids),
            "visibleActionCount": visible_action_count,
            "iconReferenceCount": len(icon_references),
            "indicatorReferenced": False,
            "disallowedPluginReferenced": False,
        }


def _is_indicator_action(
    action: Mapping[str, Any],
    forbidden_patterns: Sequence[str],
    indicator_values: set[str],
) -> bool:
    searchable = " ".join(_action_titles(action)).casefold()
    if any(pattern.casefold() in searchable for pattern in forbidden_patterns):
        return True
    settings = action.get("Settings", {})
    return (
        isinstance(settings, dict)
        and isinstance(settings.get("light"), str)
        and settings["light"] in indicator_values
    )


def _sanitize_indicator_tree(
    value: Any,
    forbidden_patterns: Sequence[str],
    indicator_values: set[str],
    *,
    preserve_slot: bool = False,
) -> tuple[Any | None, int]:
    if isinstance(value, dict):
        if isinstance(value.get("UUID"), str) and _is_indicator_action(
            value, forbidden_patterns, indicator_values
        ):
            return (_empty_action() if preserve_slot else None), 1
        result = copy.deepcopy(value)
        removed = 0
        for key, child in list(result.items()):
            if key == "Actions" and isinstance(child, dict):
                for coordinate, action in list(child.items()):
                    sanitized, count = _sanitize_indicator_tree(
                        action,
                        forbidden_patterns,
                        indicator_values,
                        preserve_slot=True,
                    )
                    child[coordinate] = sanitized if sanitized is not None else _empty_action()
                    removed += count
            elif key == "Actions" and isinstance(child, list):
                keep_positions = value.get("UUID") == "com.elgato.streamdeck.keys.logic"
                sanitized_list: list[Any] = []
                for action in child:
                    sanitized, count = _sanitize_indicator_tree(
                        action,
                        forbidden_patterns,
                        indicator_values,
                        preserve_slot=keep_positions,
                    )
                    if sanitized is not None:
                        sanitized_list.append(sanitized)
                    removed += count
                result[key] = sanitized_list
            else:
                sanitized, count = _sanitize_indicator_tree(
                    child,
                    forbidden_patterns,
                    indicator_values,
                )
                if sanitized is None and isinstance(child, dict) and isinstance(child.get("UUID"), str):
                    result.pop(key, None)
                elif sanitized is not None:
                    result[key] = sanitized
                removed += count
        return result, removed
    if isinstance(value, list):
        result_list: list[Any] = []
        removed = 0
        for child in value:
            sanitized, count = _sanitize_indicator_tree(
                child,
                forbidden_patterns,
                indicator_values,
                preserve_slot=preserve_slot,
            )
            if sanitized is not None:
                result_list.append(sanitized)
            removed += count
        return result_list, removed
    return copy.deepcopy(value), 0


def _scan_indicator_references(
    profiles_root: Path,
    forbidden_patterns: Sequence[str],
    indicator_values: set[str],
) -> int:
    count = 0
    for page_path in sorted(profiles_root.glob("*.sdProfile/Profiles/*/manifest.json")):
        page = _read_json(page_path)
        for action in _iter_actions(page):
            if _is_indicator_action(action, forbidden_patterns, indicator_values):
                count += 1
    return count


def _is_user_owned_profile(manifest: Mapping[str, Any]) -> bool:
    return not manifest.get("InstalledByPluginUUID") and not manifest.get("PreconfiguredName")


def _sanitize_user_profiles(
    paths: Paths,
    *,
    target_device_uuids: set[str],
    forbidden_patterns: Sequence[str],
    indicator_values: set[str],
    originals_root: Path,
) -> dict[str, int]:
    profile_count = 0
    action_count = 0
    generated = {value.upper() for value in PROFILE_IDS.values()}
    for profile in sorted(paths.profiles_root.glob("*.sdProfile")):
        if profile.stem.upper() in generated:
            continue
        outer = _read_json(profile / "manifest.json")
        device = outer.get("Device", {})
        device_uuid = device.get("UUID") if isinstance(device, dict) else None
        if device_uuid not in target_device_uuids or not _is_user_owned_profile(outer):
            continue
        replacements: list[tuple[Path, dict[str, Any]]] = []
        removed_for_profile = 0
        for page_path in sorted(profile.glob("Profiles/*/manifest.json")):
            page = _read_json(page_path)
            sanitized, removed = _sanitize_indicator_tree(
                page,
                forbidden_patterns,
                indicator_values,
            )
            if removed and isinstance(sanitized, dict):
                replacements.append((page_path, sanitized))
                removed_for_profile += removed
        if not replacements:
            continue
        archived = originals_root / profile.name
        shutil.copytree(profile, archived, copy_function=shutil.copy2)
        for page_path, sanitized in replacements:
            _write_json(page_path, sanitized)
        profile_count += 1
        action_count += removed_for_profile
    return {"profiles": profile_count, "actions": action_count}


def _restore_sanitized_profiles(paths: Paths, originals_root: Path, transaction: Path) -> None:
    if not originals_root.is_dir():
        return
    failed_root = _safe_private_directory(transaction / "failed-sanitized")
    for original in sorted(originals_root.glob("*.sdProfile")):
        destination = paths.profiles_root / original.name
        if destination.exists():
            os.replace(destination, failed_root / destination.name)
        shutil.copytree(original, destination, copy_function=shutil.copy2)


def _backup_manifest(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "backup-manifest.json":
            files.append(
                {
                    "path": str(path.relative_to(root)),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return {"schemaVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(), "files": files}


def create_backup(paths: Paths, destination_root: Path = DEFAULT_BACKUPS, *, include_preferences: bool = True) -> Path:
    if not paths.profiles_root.is_dir():
        raise ProfileError(f"ProfilesV3 not found: {paths.profiles_root}")
    destination_root = destination_root.expanduser().resolve()
    _safe_private_directory(destination_root)
    destination = destination_root / _now_stamp()
    destination.mkdir(mode=0o700)
    profile_copy = destination / "ProfilesV3"
    shutil.copytree(paths.profiles_root, profile_copy, copy_function=shutil.copy2)
    for directory in [profile_copy, *[path for path in profile_copy.rglob("*") if path.is_dir()]]:
        directory.chmod(0o700)
    for file_path in [path for path in profile_copy.rglob("*") if path.is_file()]:
        file_path.chmod(0o600)
    if include_preferences:
        completed = subprocess.run(
            ["/usr/bin/defaults", "export", PREFERENCES_DOMAIN, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.startswith(b"<?xml"):
            raise ProfileError("Stream Deck preferences could not be exported")
        preferences = destination / "preferences.plist"
        preferences.write_bytes(completed.stdout)
        preferences.chmod(0o600)
    _write_json(destination / "backup-manifest.json", _backup_manifest(destination))
    return destination


def validate_backup(backup: Path) -> dict[str, Any]:
    backup = backup.expanduser().resolve()
    manifest_path = backup / "backup-manifest.json"
    manifest = _read_json(manifest_path)
    problems: list[str] = []
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise ProfileError("Invalid backup manifest")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            problems.append("Invalid backup entry")
            continue
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            problems.append("Unsafe backup path")
            continue
        path = backup / relative
        if not path.is_file():
            problems.append(f"Missing file: {relative}")
            continue
        if path.stat().st_size != entry.get("size") or _sha256(path) != entry.get("sha256"):
            problems.append(f"Modified file: {relative}")
    if problems:
        raise ProfileError("Invalid backup: " + "; ".join(problems))
    return {"schemaVersion": 1, "valid": True, "fileCount": len(files)}


def _export_preferences() -> bytes:
    completed = subprocess.run(
        ["/usr/bin/defaults", "export", PREFERENCES_DOMAIN, "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ProfileError("Stream Deck preferences could not be read")
    return completed.stdout


def _import_preferences(data: bytes, transaction: Path) -> None:
    preferences = transaction / "preferences-to-import.plist"
    preferences.write_bytes(data)
    preferences.chmod(0o600)
    completed = subprocess.run(
        ["/usr/bin/defaults", "import", PREFERENCES_DOMAIN, str(preferences)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ProfileError("Stream Deck preferences could not be written")


def _profile_device_uuid(profile: Path) -> str:
    manifest = _read_json(profile / "manifest.json")
    device = manifest.get("Device", {})
    value = device.get("UUID") if isinstance(device, dict) else None
    if not isinstance(value, str) or not value:
        raise ProfileError(f"Missing device UUID: {profile.name}")
    return value


def _updated_preferences(data: bytes, profile_paths: Mapping[str, Path]) -> bytes:
    try:
        value = plistlib.loads(data)
    except Exception as exc:
        raise ProfileError("Unreadable Stream Deck preferences") from exc
    devices = value.get("Devices")
    if not isinstance(devices, dict):
        raise ProfileError("Devices table missing from Stream Deck preferences")
    assignments = {
        "cockpit": ["cockpit", "call", "studio"],
        "pedal": ["pedal"],
        "presentation": ["presentation"],
    }
    for preferred_key, keys in assignments.items():
        device_uuid = _profile_device_uuid(profile_paths[preferred_key])
        device = devices.get(device_uuid)
        if not isinstance(device, dict):
            raise ProfileError(f"Device missing from preferences for {preferred_key}")
        info = device.setdefault("ESDProfilesInfo", {})
        if not isinstance(info, dict):
            raise ProfileError(f"Invalid ESDProfilesInfo for {preferred_key}")
        existing = [
            item.strip().lower()
            for item in str(info.get("ESDProfilesSorting", "")).split(",")
            if item.strip()
        ]
        generated = [PROFILE_IDS[key].lower() for key in keys]
        existing = [candidate for candidate in existing if candidate not in generated]
        info["ESDProfilesSorting"] = ",".join([*generated, *existing])
        info["ESDProfilesPreferred"] = PROFILE_IDS[preferred_key].lower()
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def _stream_deck_app_version() -> str:
    info_path = Path("/Applications/Elgato Stream Deck.app/Contents/Info.plist")
    try:
        value = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ProfileError("Unreadable Stream Deck version") from exc
    version = value.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise ProfileError("Missing Stream Deck version")
    return version


def _validate_active_preferences(data: bytes, profile_paths: Mapping[str, Path]) -> None:
    try:
        value = plistlib.loads(data)
    except Exception as exc:
        raise ProfileError("Unreadable Stream Deck preferences after reload") from exc
    devices = value.get("Devices")
    if not isinstance(devices, dict):
        raise ProfileError("Devices table missing after reload")
    assignments = {
        "cockpit": ["cockpit", "call", "studio"],
        "pedal": ["pedal"],
        "presentation": ["presentation"],
    }
    for preferred_key, keys in assignments.items():
        device_uuid = _profile_device_uuid(profile_paths[preferred_key])
        device = devices.get(device_uuid)
        info = device.get("ESDProfilesInfo") if isinstance(device, dict) else None
        if not isinstance(info, dict):
            raise ProfileError(f"Profile preferences missing for {preferred_key}")
        expected_preferred = PROFILE_IDS[preferred_key].lower()
        if str(info.get("ESDProfilesPreferred", "")).lower() != expected_preferred:
            raise ProfileError(f"Default profile not active for {preferred_key}")
        sorting = [
            item.strip().lower()
            for item in str(info.get("ESDProfilesSorting", "")).split(",")
            if item.strip()
        ]
        expected_prefix = [PROFILE_IDS[key].lower() for key in keys]
        if sorting[: len(expected_prefix)] != expected_prefix:
            raise ProfileError(f"Inconsistent profile order for {preferred_key}")
        if any(sorting.count(profile_id) != 1 for profile_id in expected_prefix):
            raise ProfileError(f"Profile collision detected for {preferred_key}")


def _validate_smart_profiles(profiles_root: Path) -> None:
    associations: dict[str, list[str]] = {}
    for manifest_path in sorted(profiles_root.glob("*.sdProfile/manifest.json")):
        outer = _read_json(manifest_path)
        app_identifier = outer.get("AppIdentifier")
        if isinstance(app_identifier, str) and app_identifier:
            associations.setdefault(app_identifier, []).append(manifest_path.parent.stem.upper())
    for key, app_identifier in SMART_PROFILE_APPS.items():
        expected = PROFILE_IDS[key].upper()
        matches = associations.get(app_identifier, [])
        if matches != [expected]:
            raise ProfileError(f"Smart Profile collision or missing association for {key}")


def verify_live_configuration(
    paths: Paths,
    dist: Path,
    *,
    require_running: bool,
    known_indicator_values: set[str] | None = None,
) -> dict[str, Any]:
    version = _stream_deck_app_version()
    if version != TARGET_STREAM_DECK_VERSION:
        raise ProfileError(
            f"Expected Stream Deck {TARGET_STREAM_DECK_VERSION}, found version {version}"
        )
    profile_paths = {
        key: paths.profiles_root / f"{PROFILE_IDS[key]}.sdProfile" for key in PROFILE_KEYS
    }
    builder = ProfileBuilder(paths, dist)
    validation = builder.validate(
        paths.profiles_root,
        allow_materialized_defaults=True,
    )
    indicator_values = known_indicator_values
    if indicator_values is None:
        indicator_values = builder.catalog.indicator_light_values(builder.forbidden_patterns)
    indicator_count = _scan_indicator_references(
        paths.profiles_root,
        builder.forbidden_patterns,
        indicator_values,
    )
    if indicator_count:
        raise ProfileError("An active action still directly controls the BUSY Bar indicator")
    _validate_smart_profiles(paths.profiles_root)
    _validate_active_preferences(_export_preferences(), profile_paths)
    for key, application in SMART_PROFILE_APPS.items():
        if not Path(application).is_dir():
            raise ProfileError(f"Smart Profile application missing for {key}")
    if require_running and not _is_stream_deck_running():
        raise ProfileError("Stream Deck is not running after reload")
    return {
        "schemaVersion": 1,
        "valid": True,
        "streamDeckVersion": version,
        "profileCount": validation["profileCount"],
        "defaultProfilesVerified": True,
        "smartProfilesVerified": True,
        "smartProfileEditorSuppressionExpected": True,
        "indicatorReferences": 0,
        "reloadValidated": require_running,
    }


def install_profiles(
    paths: Paths,
    dist: Path,
    *,
    apply: bool,
    backups_root: Path = DEFAULT_BACKUPS,
    restart: bool = True,
) -> dict[str, Any]:
    if not apply:
        raise ProfileError("Installation refused without --apply")
    dist = dist.expanduser().resolve()
    live = dist / "live"
    builder = ProfileBuilder(paths, dist)
    validation = builder.validate(live)
    missing = [uuid_value for uuid_value in REQUIRED_PLUGIN_UUIDS if uuid_value not in builder.catalog.plugins]
    if missing:
        raise ProfileError("Required plugins are missing: " + ", ".join(missing))
    for key, application in SMART_PROFILE_APPS.items():
        if not Path(application).is_dir():
            raise ProfileError(f"Smart Profile application missing for {key}")
    indicator_values = builder.catalog.indicator_light_values(builder.forbidden_patterns)
    backup = create_backup(paths, backups_root)
    transaction = _safe_private_directory(backup / "install-transaction")
    replaced = _safe_private_directory(transaction / "replaced")
    sanitized_originals = _safe_private_directory(transaction / "sanitized-originals")
    installed: list[Path] = []
    old_preferences = _export_preferences()
    profile_paths = {key: live / f"{PROFILE_IDS[key]}.sdProfile" for key in PROFILE_KEYS}
    target_device_uuids = {_profile_device_uuid(path) for path in profile_paths.values()}
    sanitized = {"profiles": 0, "actions": 0}
    live_verification: dict[str, Any] | None = None
    try:
        _stop_stream_deck()
        paths.profiles_root.mkdir(parents=True, exist_ok=True)
        sanitized = _sanitize_user_profiles(
            paths,
            target_device_uuids=target_device_uuids,
            forbidden_patterns=builder.forbidden_patterns,
            indicator_values=indicator_values,
            originals_root=sanitized_originals,
        )
        for key in PROFILE_KEYS:
            source = profile_paths[key]
            destination = paths.profiles_root / source.name
            if destination.exists():
                os.replace(destination, replaced / destination.name)
            staging = paths.profiles_root / f".{source.name}.staging-{os.getpid()}"
            shutil.copytree(source, staging, copy_function=shutil.copy2)
            os.replace(staging, destination)
            installed.append(destination)
        indicator_count = _scan_indicator_references(
            paths.profiles_root,
            builder.forbidden_patterns,
            indicator_values,
        )
        if indicator_count:
            raise ProfileError(
                "An active action still directly controls the BUSY Bar indicator"
            )
        new_preferences = _updated_preferences(old_preferences, profile_paths)
        _import_preferences(new_preferences, transaction)
        if restart:
            _start_stream_deck()
            _wait_for_stream_deck()
            time.sleep(2.0)
        live_verification = verify_live_configuration(
            paths,
            dist,
            require_running=restart,
            known_indicator_values=indicator_values,
        )
        receipt = {
            "schemaVersion": 1,
            "installedAt": datetime.now(timezone.utc).isoformat(),
            "backup": str(backup),
            "profileCount": len(PROFILE_KEYS),
            "validation": validation,
            "sanitizedLegacyProfileCount": sanitized["profiles"],
            "sanitizedIndicatorActionCount": sanitized["actions"],
            "liveVerification": live_verification,
        }
        _write_json(transaction / "receipt.json", receipt)
    except BaseException:
        if _is_stream_deck_running():
            try:
                _stop_stream_deck()
            except ProfileError:
                pass
        for path in installed:
            if path.exists():
                os.replace(path, transaction / f"failed-{path.name}")
        for path in replaced.glob("*.sdProfile"):
            os.replace(path, paths.profiles_root / path.name)
        try:
            _restore_sanitized_profiles(paths, sanitized_originals, transaction)
            _import_preferences(old_preferences, transaction)
        finally:
            if restart:
                _start_stream_deck()
        raise
    return {
        "schemaVersion": 1,
        "installed": True,
        "profileCount": len(installed),
        "backup": str(backup),
        "restartRequested": restart,
        "legacyProfilesSanitized": sanitized["profiles"],
        "indicatorActionsNeutralized": sanitized["actions"],
        "liveVerification": live_verification,
    }


def rollback_profiles(
    paths: Paths,
    backup: Path,
    *,
    apply: bool,
    restart: bool = True,
) -> dict[str, Any]:
    if not apply:
        raise ProfileError("Rollback refused without --apply")
    backup = backup.expanduser().resolve()
    validate_backup(backup)
    source = backup / "ProfilesV3"
    preferences = backup / "preferences.plist"
    if not source.is_dir() or not preferences.is_file():
        raise ProfileError("Backup is incomplete for rollback")
    transaction = _safe_private_directory(backup / f"rollback-{_now_stamp()}")
    quarantine = transaction / "ProfilesV3-before-rollback"
    try:
        _stop_stream_deck()
        if paths.profiles_root.exists():
            os.replace(paths.profiles_root, quarantine)
        shutil.copytree(source, paths.profiles_root, copy_function=shutil.copy2)
        _import_preferences(preferences.read_bytes(), transaction)
    except BaseException:
        if paths.profiles_root.exists():
            os.replace(paths.profiles_root, transaction / "failed-restored-ProfilesV3")
        if quarantine.exists():
            os.replace(quarantine, paths.profiles_root)
        if restart:
            _start_stream_deck()
        raise
    if restart:
        _start_stream_deck()
    _write_json(
        transaction / "receipt.json",
        {
            "schemaVersion": 1,
            "rolledBackAt": datetime.now(timezone.utc).isoformat(),
            "sourceBackup": str(backup),
            "previousProfilesQuarantined": True,
        },
    )
    return {
        "schemaVersion": 1,
        "rolledBack": True,
        "sourceBackup": str(backup),
        "previousProfilesQuarantinedAt": str(quarantine),
    }


def _paths_from_args(args: argparse.Namespace) -> Paths:
    return Paths(
        profiles_root=Path(args.profiles_root).expanduser().resolve(),
        plugins_root=Path(args.plugins_root).expanduser().resolve(),
        app_plugins_root=Path(args.app_plugins_root).expanduser().resolve(),
        contract=Path(args.contract).expanduser().resolve(),
        plan=Path(args.plan).expanduser().resolve(),
        icon_assets_root=Path(args.icon_assets_root).expanduser().resolve(),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", default=str(DEFAULT_PROFILES_ROOT))
    parser.add_argument("--plugins-root", default=str(DEFAULT_PLUGINS_ROOT))
    parser.add_argument("--app-plugins-root", default=str(DEFAULT_APP_PLUGINS_ROOT))
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    parser.add_argument("--icon-assets-root", default=str(DEFAULT_ICON_ASSETS_ROOT))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("audit", help="Show an audit without hardware identifiers")
    build = commands.add_parser("build", help="Build private profiles and portable exports")
    build.add_argument("--output", default=str(DEFAULT_DIST))

    validate = commands.add_parser("validate", help="Validate the built profiles")
    validate.add_argument("--dist", default=str(DEFAULT_DIST))

    verify_live = commands.add_parser(
        "verify-live",
        help="Verify active profiles after reloading Stream Deck 7.5.1",
    )
    verify_live.add_argument("--dist", default=str(DEFAULT_DIST))

    backup = commands.add_parser("backup", help="Back up active profiles and preferences")
    backup.add_argument("--destination", default=str(DEFAULT_BACKUPS))
    backup.add_argument("--no-preferences", action="store_true")

    verify_backup = commands.add_parser("validate-backup", help="Validate a backup")
    verify_backup.add_argument("backup")

    install = commands.add_parser("install", help="Install the built profiles transactionally")
    install.add_argument("--dist", default=str(DEFAULT_DIST))
    install.add_argument("--backups-root", default=str(DEFAULT_BACKUPS))
    install.add_argument("--apply", action="store_true")
    install.add_argument("--no-restart", action="store_true")

    rollback = commands.add_parser("rollback", help="Restore a validated backup")
    rollback.add_argument("backup")
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--no-restart", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    paths = _paths_from_args(args)
    try:
        if args.command == "audit":
            result = ProfileBuilder(paths, DEFAULT_DIST).safe_audit()
        elif args.command == "build":
            output = Path(args.output).expanduser().resolve()
            built = ProfileBuilder(paths, output).build()
            result = {
                "schemaVersion": 1,
                "built": True,
                "profileCount": len(built),
                "output": str(output),
                "activeProfilesModified": False,
            }
        elif args.command == "validate":
            dist = Path(args.dist).expanduser().resolve()
            result = ProfileBuilder(paths, dist).validate(dist / "live")
        elif args.command == "verify-live":
            dist = Path(args.dist).expanduser().resolve()
            result = verify_live_configuration(
                paths,
                dist,
                require_running=True,
            )
        elif args.command == "backup":
            destination = create_backup(
                paths,
                Path(args.destination),
                include_preferences=not args.no_preferences,
            )
            result = {"schemaVersion": 1, "backup": str(destination), "valid": True}
        elif args.command == "validate-backup":
            result = validate_backup(Path(args.backup))
        elif args.command == "install":
            result = install_profiles(
                paths,
                Path(args.dist),
                apply=args.apply,
                backups_root=Path(args.backups_root),
                restart=not args.no_restart,
            )
        else:
            result = rollback_profiles(
                paths,
                Path(args.backup),
                apply=args.apply,
                restart=not args.no_restart,
            )
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
