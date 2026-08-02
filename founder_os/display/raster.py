"""Deterministic BUSY Bar text rasterization used as an accent-safe fallback."""

from __future__ import annotations

import binascii
import json
import struct
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


FONT_ALIASES = {
    "tiny": "tiny",
    "small": "small",
    "normal": "normal",
    "condensed": "condensed",
    "bold": "bold",
    "large": "large",
    "extra_large": "extra_large",
    "global": "global",
    "medium": "normal",
    "big": "extra_large",
}


@dataclass(frozen=True, slots=True)
class RasterMask:
    width: int
    height: int
    pixels: bytes
    space_width: int


class FontAtlas:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._atlas = json.loads(self.path.read_text(encoding="utf-8"))

    def rasterize(self, value: str, font_id: str) -> RasterMask:
        font = self._atlas[FONT_ALIASES.get(font_id, "normal")]
        glyphs = font["glyphs"]
        pen = 0
        right = 0
        parts: list[tuple[int, Mapping[str, Any]]] = []
        for character in unicodedata.normalize("NFC", str(value)):
            glyph = glyphs.get(str(ord(character))) or glyphs.get("63")
            if not glyph:
                continue
            parts.append((pen, glyph))
            right = max(right, pen + int(glyph["ox"]) + int(glyph["w"]))
            pen += int(glyph["adv"])
        width = max(1, pen, right)
        height = int(font["lineh"])
        pixels = bytearray(width * height)
        for origin, glyph in parts:
            glyph_width = int(glyph["w"])
            for row, encoded in enumerate(glyph["rows"]):
                if not encoded:
                    continue
                bits = int(encoded, 16)
                total_bits = len(encoded) * 4
                y = int(glyph["oy"]) + row
                if not 0 <= y < height:
                    continue
                for column in range(glyph_width):
                    if bits & (1 << (total_bits - 1 - column)):
                        x = origin + int(glyph["ox"]) + column
                        if 0 <= x < width:
                            pixels[y * width + x] = 1
        space = glyphs.get("32") or {"adv": 4}
        return RasterMask(width, height, bytes(pixels), int(space["adv"]))


def parse_color(value: str) -> tuple[int, int, int, int]:
    text = str(value or "0xFFFFFFFF").strip().removeprefix("0x").removeprefix("#")
    if len(text) in {3, 4}:
        text = "".join(character * 2 for character in text)
    if len(text) < 6:
        text = "FFFFFF"
    if len(text) < 8:
        text += "FF"
    try:
        return tuple(int(text[index : index + 2], 16) for index in range(0, 8, 2))  # type: ignore[return-value]
    except ValueError:
        return 255, 255, 255, 255


def viewport_png(
    mask: RasterMask,
    *,
    width: int,
    offset: int,
    color: tuple[int, int, int, int],
    repeat: bool,
) -> bytes:
    width = max(1, int(width))
    rgba = bytearray(width * mask.height * 4)
    gap = max(3, mask.space_width * 3)
    travel = mask.width + gap
    for y in range(mask.height):
        for x in range(width):
            source_x = x + offset
            if repeat:
                source_x %= travel
            lit = source_x < mask.width and mask.pixels[y * mask.width + source_x]
            if not lit:
                continue
            at = (y * width + x) * 4
            rgba[at : at + 4] = bytes(color)
    return encode_rgba_png(width, mask.height, bytes(rgba))


def encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA buffer length does not match its dimensions")
    scanlines = b"".join(
        b"\x00" + rgba[row * width * 4 : (row + 1) * width * 4]
        for row in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + chunk(b"IEND", b"")
    )
