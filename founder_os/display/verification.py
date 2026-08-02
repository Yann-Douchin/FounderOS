"""Readback verification for the BUSY Bar French global-font glyphs."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from founder_os.display.busybar import BusyBarDisplay
from founder_os.display.raster import FontAtlas


ACCENT_PROBES = ("Échéance", "décision", "ingénierie", "œ", "’")
ACCENT_PROBE = " | ".join(ACCENT_PROBES)


@dataclass(frozen=True, slots=True)
class GlyphVerification:
    passed: bool
    text: str
    expected_lit_pixels: int
    matched_lit_pixels: int
    missing_lit_pixels: int
    unexpected_lit_pixels: int
    recall: float
    precision: float
    cases: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def verify_french_glyphs(
    display: BusyBarDisplay,
    atlas_path: str | Path,
    *,
    settle_seconds: float = 0.25,
) -> GlyphVerification:
    atlas = FontAtlas(atlas_path)
    x, y = 1, 2
    expected_total = matched_total = missing_total = unexpected_total = 0
    cases: list[dict[str, object]] = []
    for probe in ACCENT_PROBES:
        mask = atlas.rasterize(probe, "global")
        if mask.width + x > 72 or mask.height + y > 16:
            raise ValueError(f"accent probe does not fit the front display: {probe}")
        frame = [
            {
                "id": "probe-bg",
                "type": "rectangle",
                "x": 0,
                "y": 0,
                "width": 72,
                "height": 16,
                "border_width": 0,
                "fill": "solid",
                "fill_colors": ["0x000000FF"],
            },
            {
                "id": "probe-text",
                "type": "text",
                "text": probe,
                "x": x,
                "y": y,
                "font": "global",
                "color": "0xFF0000FF",
            },
        ]
        try:
            display.draw(frame)
            time.sleep(max(0.0, settle_seconds))
            capture = display.screen(0)
        finally:
            display.clear()
        expected = {
            (x + column, y + row)
            for row in range(mask.height)
            for column in range(mask.width)
            if mask.pixels[row * mask.width + column]
        }
        actual = {
            (column, row)
            for row in range(16)
            for column in range(72)
            if capture.pixel(column, row)[0] >= 128
            and capture.pixel(column, row)[1] <= 32
            and capture.pixel(column, row)[2] <= 32
        }
        matched = expected & actual
        missing = expected - actual
        unexpected = actual - expected
        recall = len(matched) / len(expected) if expected else 1.0
        precision = len(matched) / len(actual) if actual else 0.0
        case_passed = recall >= 0.98 and precision >= 0.98
        cases.append({
            "text": probe,
            "passed": case_passed,
            "expected_lit_pixels": len(expected),
            "matched_lit_pixels": len(matched),
            "missing_lit_pixels": len(missing),
            "unexpected_lit_pixels": len(unexpected),
            "recall": round(recall, 4),
            "precision": round(precision, 4),
        })
        expected_total += len(expected)
        matched_total += len(matched)
        missing_total += len(missing)
        unexpected_total += len(unexpected)
    recall = matched_total / expected_total if expected_total else 1.0
    actual_total = matched_total + unexpected_total
    precision = matched_total / actual_total if actual_total else 0.0
    return GlyphVerification(
        passed=all(bool(case["passed"]) for case in cases),
        text=ACCENT_PROBE,
        expected_lit_pixels=expected_total,
        matched_lit_pixels=matched_total,
        missing_lit_pixels=missing_total,
        unexpected_lit_pixels=unexpected_total,
        recall=round(recall, 4),
        precision=round(precision, 4),
        cases=tuple(cases),
    )
