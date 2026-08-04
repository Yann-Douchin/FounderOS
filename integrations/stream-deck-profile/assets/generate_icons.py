#!/usr/bin/env python3
"""Generate the FounderOS Stream Deck icon suite from deterministic SVG masters.

The profile assets are 144 x 144 RGBA PNG files. Each visible surface gets its
own file, while related actions share a small set of geometric glyphs, colors,
spacing, and stroke rules. The same renderer also produces the custom plugin
icons so the whole Stream Deck setup stays visually coherent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_ICON_ROOT = HERE / "icons"
DEFAULT_PLUGIN_ROOT = (
    HERE.parents[1]
    / "stream-deck"
    / "com.yanndouchin.founderos-actions.sdPlugin"
)

SIZE = 144
FOREGROUND = "#F7FAFF"
MUTED = "#9EB0C7"
BACKGROUND_START = "#0A111B"
BACKGROUND_END = "#152235"

ACCENTS = {
    "founderos": "#4EA1FF",
    "focus": "#A678FF",
    "capture": "#39D98A",
    "meet": "#FFB14E",
    "studio": "#FF5A64",
    "safe": "#39D98A",
    "danger": "#FF5A64",
    "utility": "#8AA4C8",
    "presentation": "#62C8FF",
}


@dataclass(frozen=True)
class IconAsset:
    filename: str
    glyph: str
    accent: str
    label: str
    active: bool = False


GLYPHS = {
    "status": """
<path d="M42 78a30 30 0 0 1 60 0"/>
<path d="M52 78a20 20 0 0 1 40 0" opacity=".42"/>
<path d="M72 78l18-19"/>
<circle cx="72" cy="78" r="6" fill="{accent}" stroke="none"/>
""",
    "priority_open": """
<rect x="40" y="42" width="64" height="48" rx="12" opacity=".36"/>
<path d="M57 76l31-31M71 45h17v17"/>
""",
    "snooze": """
<circle cx="73" cy="65" r="28"/>
<path d="M73 50v17l12 8"/>
<path d="M48 42H35v13"/>
<path d="M36 54a40 40 0 0 1 8-13" opacity=".62"/>
""",
    "priority_handle": """
<circle cx="72" cy="65" r="30"/>
<path d="M55 65l11 12 24-27"/>
""",
    "shield_check": """
<path d="M72 35l27 10v19c0 18-11 29-27 36-16-7-27-18-27-36V45z"/>
<path d="M57 66l10 10 21-22"/>
""",
    "shield_x": """
<path d="M72 35l27 10v19c0 18-11 29-27 36-16-7-27-18-27-36V45z"/>
<path d="M60 57l24 24M84 57L60 81"/>
""",
    "focus": """
<circle cx="72" cy="65" r="28"/>
<circle cx="72" cy="65" r="12"/>
<path d="M72 30v12M72 88v12M37 65h12M95 65h12"/>
<circle cx="72" cy="65" r="5" fill="{accent}" stroke="none"/>
""",
    "things_capture": """
<path d="M43 70v13c0 6 4 10 10 10h38c6 0 10-4 10-10V70"/>
<path d="M72 35v38M55 54h34"/>
""",
    "prepare_meet": """
<rect x="37" y="46" width="59" height="42" rx="10"/>
<path d="M96 58l13-8v34l-13-8z"/>
<path d="M51 35v20M41 45h20" opacity=".74"/>
""",
    "open_studio": """
<rect x="39" y="50" width="66" height="45" rx="8"/>
<path d="M39 50l7-18h66l-7 18zM55 32l-7 18M78 32l-7 18M101 32l-7 18"/>
<circle cx="72" cy="72" r="9" fill="{accent}" stroke="none"/>
""",
    "end_session": """
<path d="M55 42a31 31 0 1 0 34 0"/>
<path d="M72 30v37"/>
""",
    "output_volume": """
<path d="M38 59h16l20-17v46L54 71H38z"/>
<path d="M87 54a18 18 0 0 1 0 22M97 44a31 31 0 0 1 0 42"/>
""",
    "microphone": """
<rect x="58" y="31" width="28" height="50" rx="14"/>
<path d="M47 65a25 25 0 0 0 50 0M72 90v14M57 104h30"/>
""",
    "microphone_muted": """
<rect x="58" y="31" width="28" height="50" rx="14"/>
<path d="M47 65a25 25 0 0 0 41 19M72 90v14M57 104h30M42 35l60 65"/>
""",
    "work_lights": """
<path d="M53 64a19 19 0 1 1 38 0c0 10-8 14-10 23H63c-2-9-10-13-10-23z"/>
<path d="M63 96h18M58 35l-7-9M86 35l7-9M46 54l-11-3M98 54l11-3"/>
""",
    "mode_wheel": """
<circle cx="72" cy="65" r="27"/>
<circle cx="72" cy="65" r="7" fill="{accent}" stroke="none"/>
<path d="M72 38v20M72 72v20M45 65h20M79 65h20"/>
""",
    "writing": """
<path d="M45 90l7-22 35-35 18 18-35 35zM52 68l18 18M87 33l18 18"/>
<path d="M45 90l20-5-15-15z" fill="{accent}" stroke="none"/>
""",
    "presentation": """
<rect x="37" y="37" width="70" height="50" rx="8"/>
<path d="M72 87v15M56 102h32"/>
<path d="M64 52l22 11-22 11z" fill="{accent}" stroke="none"/>
""",
    "home": """
<path d="M38 65l34-29 34 29M46 60v38h52V60M64 98V76h16v22"/>
""",
    "camera_tracking": """
<path d="M46 49V38h15M98 49V38H83M46 80v11h15M98 80v11H83"/>
<circle cx="72" cy="65" r="18"/>
<circle cx="72" cy="65" r="6" fill="{accent}" stroke="none"/>
""",
    "meet_camera": """
<rect x="37" y="45" width="59" height="42" rx="10"/>
<path d="M96 57l13-8v34l-13-8z"/>
<circle cx="66" cy="66" r="8" fill="{accent}" stroke="none"/>
""",
    "screenbrush": """
<path d="M49 87l34-43 17 14-35 43H46z"/>
<path d="M83 44l8-10 17 14-8 10M46 101c-9 4-15-1-12-10 2-5 8-7 14-4"/>
""",
    "notes": """
<path d="M47 34h36l17 17v49H47zM83 34v18h17"/>
<path d="M60 67h27M60 81h27"/>
""",
    "call_lights": """
<circle cx="72" cy="62" r="21"/>
<path d="M72 27v10M72 87v12M37 62h10M97 62h10M47 37l7 8M90 79l8 8M97 37l-8 8M54 79l-8 8"/>
<circle cx="72" cy="62" r="8" fill="{accent}" stroke="none"/>
""",
    "prompter": """
<rect x="35" y="35" width="74" height="55" rx="9"/>
<path d="M53 51h38M53 64h29M72 90v12M57 102h30"/>
""",
    "prompter_active": """
<rect x="35" y="35" width="74" height="55" rx="9"/>
<path d="M51 51h28M51 64h20M72 90v12M57 102h30"/>
<path d="M88 49l15 10-15 10z" fill="{accent}" stroke="none"/>
""",
    "end_call": """
<path d="M42 78c16-16 44-16 60 0l-12 15-12-10H66L54 93z"/>
<path d="M72 36v29M60 53l12 12 12-12"/>
""",
    "microphone_gain": """
<rect x="47" y="34" width="23" height="42" rx="12"/>
<path d="M38 62a21 21 0 0 0 40 8M59 83v16M47 99h24"/>
<path d="M91 39v54M84 49h14M84 78h14"/>
<circle cx="91" cy="64" r="6" fill="{accent}" stroke="none"/>
""",
    "video_lights": """
<path d="M47 42h50l-8 52H55z"/>
<path d="M58 54h28M72 94v10M57 104h30"/>
<circle cx="72" cy="70" r="9" fill="{accent}" stroke="none"/>
""",
    "studio_prepare": """
<rect x="39" y="50" width="66" height="45" rx="8"/>
<path d="M39 50l7-18h66l-7 18zM55 32l-7 18M78 32l-7 18M101 32l-7 18"/>
<path d="M72 62v22M61 73h22"/>
""",
    "record_start": """
<circle cx="72" cy="65" r="31"/>
<circle cx="72" cy="65" r="17" fill="{accent}" stroke="none"/>
""",
    "record_pause": """
<circle cx="72" cy="65" r="31"/>
<rect x="58" y="48" width="9" height="34" rx="3" fill="{accent}" stroke="none"/>
<rect x="77" y="48" width="9" height="34" rx="3" fill="{accent}" stroke="none"/>
""",
    "record_resume": """
<circle cx="72" cy="65" r="31"/>
<path d="M64 47l27 18-27 18z" fill="{accent}" stroke="none"/>
""",
    "obs_screenshot": """
<path d="M39 49h17l6-9h20l6 9h17v45H39z"/>
<circle cx="72" cy="71" r="15"/>
<circle cx="72" cy="71" r="6" fill="{accent}" stroke="none"/>
""",
    "record_end": """
<circle cx="72" cy="65" r="31"/>
<rect x="57" y="50" width="30" height="30" rx="5" fill="{accent}" stroke="none"/>
""",
    "prompter_stack": """
<rect x="42" y="32" width="62" height="45" rx="8" opacity=".42"/>
<rect x="35" y="44" width="74" height="52" rx="9"/>
<path d="M53 61h38M53 74h28"/>
""",
    "display_brightness": """
<rect x="35" y="39" width="74" height="52" rx="9"/>
<circle cx="72" cy="65" r="10" fill="{accent}" stroke="none"/>
<path d="M72 47v6M72 77v6M54 65h6M84 65h6M60 53l4 4M80 73l4 4M84 53l-4 4M64 73l-4 4"/>
""",
    "scroll_speed": """
<path d="M42 82a32 32 0 0 1 60 0"/>
<path d="M72 82l20-22"/>
<circle cx="72" cy="82" r="6" fill="{accent}" stroke="none"/>
<path d="M49 70l-8-5M95 70l8-5M72 50V40"/>
""",
    "voice_ptt": """
<rect x="58" y="31" width="28" height="48" rx="14"/>
<path d="M49 62a23 23 0 0 0 46 0M72 86v15M60 101h24"/>
<path d="M38 54v17M30 59v7M106 54v17M114 59v7" opacity=".68"/>
""",
    "slide_previous": """
<rect x="35" y="37" width="74" height="55" rx="9"/>
<path d="M78 52L62 65l16 13M63 65h27"/>
""",
    "slide_next": """
<rect x="35" y="37" width="74" height="55" rx="9"/>
<path d="M66 52l16 13-16 13M81 65H54"/>
""",
    "prompter_play_pause": """
<rect x="35" y="35" width="74" height="55" rx="9"/>
<path d="M55 51l18 12-18 12z" fill="{accent}" stroke="none"/>
<path d="M84 51v24M95 51v24"/>
<path d="M72 90v12M57 102h30"/>
""",
    "speed_down": """
<path d="M42 77a32 32 0 0 1 60 0M72 77l16-18"/>
<circle cx="72" cy="77" r="5" fill="{accent}" stroke="none"/>
<path d="M56 96h32"/>
""",
    "speed_up": """
<path d="M42 77a32 32 0 0 1 60 0M72 77l16-18"/>
<circle cx="72" cy="77" r="5" fill="{accent}" stroke="none"/>
<path d="M72 89v28M58 103h28"/>
""",
}


PROFILE_ASSETS = (
    IconAsset("cockpit-key-0-0-priority-open.png", "priority_open", "founderos", "Cockpit, open priority"),
    IconAsset("cockpit-key-1-0-priority-snooze.png", "snooze", "founderos", "Cockpit, snooze"),
    IconAsset("cockpit-key-2-0-priority-handle.png", "priority_handle", "safe", "Cockpit, acknowledge"),
    IconAsset("cockpit-key-3-0-focus-50.png", "focus", "focus", "Cockpit, focus"),
    IconAsset("cockpit-key-0-1-things-capture.png", "things_capture", "capture", "Cockpit, capture"),
    IconAsset("cockpit-key-1-1-prepare-meet.png", "prepare_meet", "meet", "Cockpit, prepare Meet"),
    IconAsset("cockpit-key-2-1-open-studio.png", "open_studio", "studio", "Cockpit, studio"),
    IconAsset("cockpit-key-3-1-end-session.png", "end_session", "danger", "Cockpit, finish"),
    IconAsset("cockpit-dial-0-output-volume.png", "output_volume", "utility", "Cockpit, volume"),
    IconAsset("cockpit-dial-1-yeti-input.png", "microphone_gain", "utility", "Cockpit, Yeti X"),
    IconAsset("cockpit-dial-2-work-lights.png", "work_lights", "capture", "Cockpit, lighting"),
    IconAsset("cockpit-dial-3-mode-wheel.png", "mode_wheel", "founderos", "Cockpit, modes"),
    IconAsset("cockpit-wheel-0-meet.png", "prepare_meet", "meet", "Meet mode"),
    IconAsset("cockpit-wheel-1-studio.png", "open_studio", "studio", "Studio mode"),
    IconAsset("cockpit-wheel-2-writing.png", "writing", "focus", "Writing mode"),
    IconAsset("cockpit-wheel-3-presentation.png", "presentation", "presentation", "Presentation mode"),
    IconAsset("cockpit-wheel-4-home.png", "home", "capture", "Home mode"),
    IconAsset("meet-key-0-0-microphone.png", "microphone_muted", "meet", "Meet, microphone"),
    IconAsset("meet-key-1-0-camera.png", "meet_camera", "meet", "Meet, camera"),
    IconAsset("meet-key-2-0-screenbrush.png", "screenbrush", "utility", "Meet, ScreenBrush"),
    IconAsset("meet-key-3-0-notes.png", "notes", "capture", "Meet, notes"),
    IconAsset("meet-key-0-1-lights.png", "call_lights", "meet", "Meet, lights"),
    IconAsset("meet-key-1-1-camera-tracking.png", "camera_tracking", "meet", "Meet, camera tracking"),
    IconAsset("meet-key-2-1-prompter-inactive.png", "prompter", "utility", "Meet, Prompter inactive"),
    IconAsset("meet-key-2-1-prompter-active.png", "prompter_active", "meet", "Meet, Prompter active", True),
    IconAsset("meet-key-3-1-end.png", "end_call", "danger", "Meet, end"),
    IconAsset("meet-dial-0-yeti-gain.png", "microphone_gain", "meet", "Meet, Yeti X gain"),
    IconAsset("meet-dial-1-output-volume.png", "output_volume", "utility", "Meet, volume"),
    IconAsset("meet-dial-2-video-lights.png", "video_lights", "meet", "Meet, video lights"),
    IconAsset("meet-dial-3-prompter-inactive.png", "prompter", "utility", "Meet, Prompter dial"),
    IconAsset("meet-dial-3-prompter-active.png", "prompter_active", "meet", "Meet, Prompter dial active", True),
    IconAsset("studio-key-0-0-prepare-lights.png", "studio_prepare", "studio", "Studio, prepare"),
    IconAsset("studio-key-1-0-record-start.png", "record_start", "danger", "Studio, start REC"),
    IconAsset("studio-key-2-0-record-pause.png", "record_pause", "studio", "Studio, pause"),
    IconAsset("studio-key-2-0-record-resume.png", "record_resume", "safe", "Studio, resume", True),
    IconAsset("studio-key-3-0-obs-screenshot.png", "obs_screenshot", "utility", "Studio, OBS capture"),
    IconAsset("studio-key-0-1-screenbrush.png", "screenbrush", "utility", "Studio, ScreenBrush"),
    IconAsset("studio-key-1-1-face-tracking.png", "camera_tracking", "studio", "Studio, camera tracking"),
    IconAsset("studio-key-2-1-prompter-inactive.png", "prompter", "utility", "Studio, Prompter inactive"),
    IconAsset("studio-key-2-1-prompter-active.png", "prompter_active", "studio", "Studio, Prompter active", True),
    IconAsset("studio-key-3-1-record-end.png", "record_end", "danger", "Studio, end REC"),
    IconAsset("studio-dial-0-yeti-gain.png", "microphone_gain", "studio", "Studio, Yeti X gain"),
    IconAsset("studio-dial-1-prompter-inactive.png", "prompter", "utility", "Studio, Prompter control"),
    IconAsset("studio-dial-1-prompter-active.png", "prompter_active", "studio", "Studio, Prompter control active", True),
    IconAsset("studio-dial-2-video-lights.png", "video_lights", "studio", "Studio, video lights"),
    IconAsset("studio-dial-3-prompter-stack.png", "prompter_stack", "studio", "Studio, Prompter stack"),
    IconAsset("studio-stack-0-display-brightness.png", "display_brightness", "utility", "Prompter, display"),
    IconAsset("studio-stack-1-prompter-inactive.png", "prompter", "utility", "Prompter, control"),
    IconAsset("studio-stack-1-prompter-active.png", "prompter_active", "studio", "Prompter, control active", True),
    IconAsset("studio-stack-2-scroll-speed.png", "scroll_speed", "studio", "Prompter, speed"),
    IconAsset("pedal-0-voice-ptt.png", "voice_ptt", "focus", "Pedal, voice"),
    IconAsset("pedal-1-things-capture.png", "things_capture", "capture", "Pedal, capture"),
    IconAsset("pedal-2-screenbrush.png", "screenbrush", "utility", "Pedal, visual"),
    IconAsset("presentation-key-0-0-slide-previous.png", "slide_previous", "presentation", "Presentation, previous"),
    IconAsset("presentation-key-1-0-slide-next.png", "slide_next", "presentation", "Presentation, next"),
    IconAsset("presentation-key-2-0-prompter-inactive.png", "prompter_play_pause", "presentation", "Presentation, Prompter"),
    IconAsset("presentation-key-2-0-prompter-active.png", "prompter_active", "presentation", "Presentation, Prompter active", True),
    IconAsset("presentation-key-0-1-speed-down.png", "speed_down", "presentation", "Presentation, slower"),
    IconAsset("presentation-key-1-1-speed-up.png", "speed_up", "presentation", "Presentation, faster"),
    IconAsset("presentation-key-2-1-screenbrush.png", "screenbrush", "utility", "Presentation, ScreenBrush"),
)


PLUGIN_ACTIONS = {
    "status": ("status", "founderos"),
    "open": ("priority_open", "founderos"),
    "snooze": ("snooze", "founderos"),
    "acknowledge": ("priority_handle", "safe"),
    "allow": ("shield_check", "safe"),
    "deny": ("shield_x", "danger"),
    "presence": ("focus", "focus"),
}


def _glyph_svg(glyph: str, accent: str, *, action_icon: bool = False) -> str:
    fragment = GLYPHS[glyph].format(
        accent=accent,
        foreground=FOREGROUND,
        muted=MUTED,
    ).strip()
    if action_icon:
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="20 18 104 94">
<g fill="none" stroke="#FFFFFF" stroke-width="8" stroke-linecap="round" stroke-linejoin="round">
{fragment.replace(accent, '#FFFFFF')}
</g>
</svg>"""
    return fragment


def _tile_svg(glyph: str, accent_key: str, *, width: int = SIZE) -> str:
    accent = ACCENTS[accent_key]
    fragment = _glyph_svg(glyph, accent)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{width}" viewBox="0 0 144 144">
<defs>
  <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{BACKGROUND_START}"/>
    <stop offset="1" stop-color="{BACKGROUND_END}"/>
  </linearGradient>
  <radialGradient id="halo" cx="84%" cy="12%" r="70%">
    <stop offset="0" stop-color="{accent}" stop-opacity=".24"/>
    <stop offset="1" stop-color="{accent}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect x="3" y="3" width="138" height="138" rx="27" fill="url(#background)" stroke="#293B51" stroke-width="2"/>
<rect x="3" y="3" width="138" height="138" rx="27" fill="url(#halo)"/>
<path d="M54 13h36" stroke="{accent}" stroke-width="5" stroke-linecap="round"/>
<circle cx="119" cy="24" r="5" fill="{accent}"/>
<g transform="translate(0 -5)" fill="none" stroke="{FOREGROUND}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round">
{fragment}
</g>
</svg>"""


def _write_text(path: Path, text: str) -> None:
    normalized = unicodedata.normalize("NFC", text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(normalized + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _render_png(
    svg_path: Path,
    png_path: Path,
    magick: str,
    qlmanage: str,
) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = png_path.with_name(f".{png_path.name}.tmp.png")
    with tempfile.TemporaryDirectory(prefix="founderos-icon-render-") as raw_directory:
        raw_root = Path(raw_directory)
        quicklook = subprocess.run(
            [qlmanage, "-t", "-s", "512", "-o", str(raw_root), str(svg_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        raw_png = raw_root / f"{svg_path.name}.png"
        if quicklook.returncode != 0 or not raw_png.is_file():
            detail = quicklook.stderr.strip() or quicklook.stdout.strip()
            raise RuntimeError(f"SVG rendering failed for {svg_path.name}: {detail}")
        completed = subprocess.run(
            [
                magick,
                str(raw_png),
                "-trim",
                "+repage",
                "-resize",
                "138x138!",
                "-gravity",
                "center",
                "-background",
                "none",
                "-extent",
                f"{SIZE}x{SIZE}",
                "(",
                "-size",
                f"{SIZE}x{SIZE}",
                "xc:none",
                "-fill",
                "white",
                "-stroke",
                "none",
                "-draw",
                "roundrectangle 3,3 140,140 27,27",
                ")",
                "-compose",
                "DstIn",
                "-composite",
                "-colorspace",
                "sRGB",
                "-strip",
                f"PNG32:{temporary}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"PNG normalization failed for {svg_path.name}: "
                f"{completed.stderr.strip()}"
            )
    temporary.replace(png_path)


def generate_profile_assets(icon_root: Path, magick: str, qlmanage: str) -> None:
    icon_root.mkdir(parents=True, exist_ok=True)
    expected = {asset.filename for asset in PROFILE_ASSETS}
    expected.update({Path(name).with_suffix(".svg").name for name in expected})
    for existing in icon_root.iterdir():
        if existing.is_file() and existing.name not in expected:
            existing.unlink()
    for asset in PROFILE_ASSETS:
        svg_path = icon_root / Path(asset.filename).with_suffix(".svg")
        png_path = icon_root / asset.filename
        _write_text(svg_path, _tile_svg(asset.glyph, asset.accent))
        _render_png(svg_path, png_path, magick, qlmanage)

    preview_png = HERE / "icon-suite-preview.png"
    _render_preview(icon_root, preview_png, magick)


def _render_preview(icon_root: Path, png_path: Path, magick: str) -> None:
    temporary = png_path.with_name(f".{png_path.name}.tmp.png")
    sources = [str(icon_root / asset.filename) for asset in PROFILE_ASSETS]
    completed = subprocess.run(
        [
            magick,
            "montage",
            *sources,
            "-tile",
            "5x12",
            "-geometry",
            "144x144+14+14",
            "-background",
            "#070C13",
            "-strip",
            f"PNG32:{temporary}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Preview failed: {completed.stderr.strip()}")
    temporary.replace(png_path)


def generate_plugin_assets(plugin_root: Path) -> None:
    image_root = plugin_root / "imgs"
    for name, (glyph, accent_key) in PLUGIN_ACTIONS.items():
        destination = image_root / "actions" / name
        _write_text(destination / "key.svg", _tile_svg(glyph, accent_key, width=72))
        _write_text(destination / "key@2x.svg", _tile_svg(glyph, accent_key, width=144))
        action = _glyph_svg(glyph, "#FFFFFF", action_icon=True)
        _write_text(destination / "action.svg", action)
        _write_text(
            destination / "action@2x.svg",
            action.replace('width="20" height="20"', 'width="40" height="40"', 1),
        )

    plugin_directory = image_root / "plugin"
    category = _glyph_svg("status", "#FFFFFF", action_icon=True)
    _write_text(
        plugin_directory / "category.svg",
        category.replace('width="20" height="20"', 'width="28" height="28"', 1),
    )
    _write_text(
        plugin_directory / "category@2x.svg",
        category.replace('width="20" height="20"', 'width="56" height="56"', 1),
    )


def _validate_assets(icon_root: Path) -> None:
    expected = {asset.filename for asset in PROFILE_ASSETS}
    actual = {path.name for path in icon_root.glob("*.png")}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(f"Incomplete suite, missing={missing}, unexpected={extra}")
    if len(expected) != 60:
        raise RuntimeError(f"Expected 60 PNG files, found {len(expected)} definitions")
    if len({asset.label for asset in PROFILE_ASSETS}) != len(PROFILE_ASSETS):
        raise RuntimeError("Duplicate surface label")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--icon-root", type=Path, default=DEFAULT_ICON_ROOT)
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--plugin-only", action="store_true")
    args = parser.parse_args()

    magick = shutil.which("magick")
    qlmanage = shutil.which("qlmanage")
    if not args.plugin_only:
        if magick is None or qlmanage is None:
            raise RuntimeError("ImageMagick and Quick Look are required to generate PNG files")
        generate_profile_assets(args.icon_root.resolve(), magick, qlmanage)
        _validate_assets(args.icon_root.resolve())
    if args.plugin_root is not None:
        generate_plugin_assets(args.plugin_root.resolve())
    print(
        f"FounderOS suite generated, {0 if args.plugin_only else len(PROFILE_ASSETS)} profile PNG files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
