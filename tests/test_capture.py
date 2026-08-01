from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path

from tools.capture_emulator import encode_png, rasterize


ROOT = Path(__file__).resolve().parents[1]


class EmulatorCaptureTests(unittest.TestCase):
    def test_dependency_free_capture_writes_gallery_dimensions(self) -> None:
        grid = [[(0, 0, 0) for _ in range(72)] for _ in range(16)]
        payload = encode_png(grid)
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", payload[16:24]), (720, 160))

    def test_accent_pixel_survives_rasterization(self) -> None:
        atlas = json.loads((ROOT / "public/fonts/font-atlas.json").read_text(encoding="utf-8"))
        _, _, accented = rasterize("É", "global", atlas)
        _, _, plain = rasterize("E", "global", atlas)
        self.assertIn(0, {y for _, y in accented})
        self.assertNotEqual(accented, plain)


if __name__ == "__main__":
    unittest.main()
