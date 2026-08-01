#!/usr/bin/env python3
"""Export the current emulator front matrix to a dependency-free 720x160 PNG."""

from __future__ import annotations

import argparse
import json
import struct
import unicodedata
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
WIDTH = 72
HEIGHT = 16
SCALE = 10
FONT_ALIASES = {"medium": "normal", "big": "extra_large"}
ALIGN = {
    "top_left": (0.0, 0.0), "top_mid": (0.5, 0.0), "top_right": (1.0, 0.0),
    "mid_left": (0.0, 0.5), "center": (0.5, 0.5), "mid_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0), "bottom_mid": (0.5, 1.0), "bottom_right": (1.0, 1.0),
}


def read_state(url: str, timeout: float) -> Mapping[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the emulator event URL must use loopback HTTP")
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        event_name = ""
        while True:
            raw = response.readline(2 * 1024 * 1024)
            if not raw:
                raise RuntimeError("the emulator event stream closed before a state event")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:") and event_name == "state":
                state = json.loads(line[5:].strip())
                if not isinstance(state, Mapping):
                    raise RuntimeError("the emulator state must be an object")
                return state


def parse_color(value: Any) -> tuple[int, int, int]:
    text = str(value or "0xFFFFFFFF").strip().removeprefix("0x").removeprefix("#")
    if len(text) < 6:
        return 255, 255, 255
    try:
        return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return 255, 255, 255


def anchor(element: Mapping[str, Any], width: int, height: int) -> tuple[int, int]:
    horizontal, vertical = ALIGN.get(str(element.get("align") or "top_left"), ALIGN["top_left"])
    return (
        int(element.get("x") or 0) - round(width * horizontal),
        int(element.get("y") or 0) - round(height * vertical),
    )


def put(grid: list[list[tuple[int, int, int]]], x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        grid[y][x] = color


def rasterize(text: str, font_id: str, atlas: Mapping[str, Any]) -> tuple[int, int, list[tuple[int, int]]]:
    font_key = FONT_ALIASES.get(font_id, font_id if font_id in atlas else "normal")
    font = atlas[font_key]
    pen = 0
    right = 0
    parts: list[tuple[int, Mapping[str, Any]]] = []
    for character in unicodedata.normalize("NFC", text):
        glyph = font["glyphs"].get(str(ord(character))) or font["glyphs"].get("63")
        if not glyph:
            continue
        parts.append((pen, glyph))
        right = max(right, pen + int(glyph["ox"]) + int(glyph["w"]))
        pen += int(glyph["adv"])
    width = max(1, pen, right)
    height = int(font["lineh"])
    pixels: list[tuple[int, int]] = []
    for offset, glyph in parts:
        for row_index, row in enumerate(glyph["rows"]):
            bits = int(row, 16) if row else 0
            total = len(row) * 4
            y = int(glyph["oy"]) + row_index
            for x_index in range(int(glyph["w"])):
                if bits & (1 << (total - 1 - x_index)):
                    pixels.append((offset + int(glyph["ox"]) + x_index, y))
    return width, height, pixels


def draw_rectangle(grid: list[list[tuple[int, int, int]]], element: Mapping[str, Any]) -> None:
    width = int(element.get("width") or 0)
    height = int(element.get("height") or 0)
    origin_x, origin_y = anchor(element, width, height)
    colors = element.get("fill_colors") or []
    if element.get("fill") != "none" and colors:
        color = parse_color(colors[0])
        for y in range(height):
            for x in range(width):
                put(grid, origin_x + x, origin_y + y, color)
    border_width = int(element.get("border_width") or 0)
    if border_width:
        color = parse_color(element.get("border_color"))
        for inset in range(border_width):
            for x in range(width):
                put(grid, origin_x + x, origin_y + inset, color)
                put(grid, origin_x + x, origin_y + height - 1 - inset, color)
            for y in range(height):
                put(grid, origin_x + inset, origin_y + y, color)
                put(grid, origin_x + width - 1 - inset, origin_y + y, color)


def draw_text(grid: list[list[tuple[int, int, int]]], element: Mapping[str, Any], atlas: Mapping[str, Any]) -> None:
    width, height, pixels = rasterize(
        str(element.get("text") or ""), str(element.get("font") or "normal"), atlas
    )
    clip_left = 0
    clip_right = WIDTH - 1
    if float(element.get("scroll_rate") or 0) > 0:
        box_width = int(element.get("width") or (WIDTH - int(element.get("x") or 0)))
        horizontal, vertical = ALIGN.get(str(element.get("align") or "top_left"), ALIGN["top_left"])
        origin_x = int(element.get("x") or 0) - round(box_width * horizontal)
        origin_y = int(element.get("y") or 0) - round(height * vertical)
        clip_left, clip_right = origin_x, origin_x + box_width - 1
    else:
        origin_x, origin_y = anchor(element, width, height)
        if element.get("width") is not None:
            clip_left, clip_right = origin_x, origin_x + int(element["width"]) - 1
    color = parse_color(element.get("color"))
    for x, y in pixels:
        target_x = origin_x + x
        if clip_left <= target_x <= clip_right:
            put(grid, target_x, origin_y + y, color)


def render(elements: Any, atlas: Mapping[str, Any]) -> list[list[tuple[int, int, int]]]:
    if not isinstance(elements, list):
        raise RuntimeError("emulator frame elements must be a list")
    grid = [[(0, 0, 0) for _ in range(WIDTH)] for _ in range(HEIGHT)]
    for element in elements:
        if not isinstance(element, Mapping) or element.get("display") == "back":
            continue
        if element.get("type") == "rectangle":
            draw_rectangle(grid, element)
        elif element.get("type") == "text":
            draw_text(grid, element, atlas)
        else:
            raise RuntimeError(f"unsupported capture element type: {element.get('type')!r}")
    return grid


def corrected(color: tuple[int, int, int]) -> tuple[int, int, int]:
    values = []
    for channel in color:
        value = channel / 255
        values.append(255 * ((value ** (1 / 0.35) + 0.08 * value) / 1.08) * 0.8)
    boost = 30 * max(values) / 255
    return tuple(min(255, round(value + boost)) for value in values)


def encode_png(grid: list[list[tuple[int, int, int]]]) -> bytes:
    output_width = WIDTH * SCALE
    output_height = HEIGHT * SCALE
    pixels = [[(8, 8, 9) for _ in range(output_width)] for _ in range(output_height)]
    for y in range(HEIGHT):
        for x in range(WIDTH):
            color = corrected(grid[y][x])
            if max(color) < 2:
                continue
            glow = tuple(min(255, 8 + round(channel * 0.14)) for channel in color)
            for py in range(y * SCALE, (y + 1) * SCALE):
                for px in range(x * SCALE, (x + 1) * SCALE):
                    pixels[py][px] = glow
            for py in range(y * SCALE + 1, (y + 1) * SCALE - 1):
                for px in range(x * SCALE + 1, (x + 1) * SCALE - 1):
                    pixels[py][px] = color
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in pixels)

    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", output_width, output_height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-url", default="http://127.0.0.1:8080/events")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    state = read_state(args.events_url, max(0.5, args.timeout))
    frame = state.get("frame") or {}
    if not isinstance(frame, Mapping) or not frame.get("elements"):
        raise RuntimeError("the emulator has no active display frame")
    atlas = json.loads((ROOT / "public/fonts/font-atlas.json").read_text(encoding="utf-8"))
    payload = encode_png(render(frame["elements"], atlas))
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(output)
    output.chmod(0o644)
    print(f"captured {frame.get('application_name')} with {len(frame['elements'])} elements to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
