# Third-party assets

All assets listed below are sourced from the open-source firmware repository https://github.com/busy-app/busybar-firmware and are copyright © Flipper Devices.

## Graphics (CC-BY 4.0)

**`public/animations/`**: 72×16 status and effect animation frames (all 12 status themes: keep_out, dnd, meeting, on_call, lunch, back_soon, booked, flow, chill_time, on_air, coding, low_social_battery; plus effects: indicator_busy_72x16, finished_confetti_72x16, etc.). Licensed under CC-BY 4.0 (attribution required).

**`public/icons/`** and **`public/icons.json`**: Stock icon set (66 SVG icons from the device draw tool: faces/emoji-grinning, sun, cloud, heart, check, bolt, and more). Licensed under CC-BY 4.0 (attribution required).

**`public/brand/`**, **`.github/logo.svg`** and **`web/public/favicon.png`**: BUSY logo, app icon and device render (busybar-device.png illustration used in the web UI; favicon used as the repository logo and web UI icon). Licensed under CC-BY 4.0 (attribution required).

## Sounds (CC-BY-SA 4.0)

**`public/sounds/*.wav`**: The three stock device sounds (calendar_event_starts, calendar_reminder_ends, volume_change) from the firmware's `assets/shared/sounds/`. Copyright © 2024-2026 Flipper FZCO, licensed under CC-BY-SA 4.0 (per the firmware's `REUSE.toml`).

## Fonts (SIL OFL 1.1)

**`public/fonts/*.ttf`**: Pixel and proportional typefaces:
- busy_tiny, busy_regular_5px, busy_regular_7px, busy_condensed_7px, busy_bold_7px, busy_regular_9px, busy_bold_10px (Flipper BUSY Bar device fonts)
- LanaPixel (UI font)
- Inter (fallback)

All licensed under the SIL Open Font License, version 1.1.

**`public/fonts/font-atlas.json`**: Derived work. This glyph atlas is a baked artifact created with lv_font_conv using the same parameters as the firmware, mapping the above fonts to a 1-bpp bitmap atlas for efficient browser rendering. The atlas inherits the OFL 1.1 license from its source fonts.

## Attribution

This project bundles these assets to enable faithful prototyping against the BUSY Bar API. "BUSY Bar" is a product of Flipper Devices Inc.; this project is unaffiliated and unofficial.

## BarPilot compatibility reference

FounderOS uses [BarPilot](https://github.com/nastea1/barpilot), copyright its contributors and licensed under the MIT License, as an external behavior and interoperability reference. The pinned reference is commit [`5c4afe96e178982d7e5f95a9dfea0cf761804d80`](https://github.com/nastea1/barpilot/blob/5c4afe96e178982d7e5f95a9dfea0cf761804d80/barpilot.html).

BarPilot is not bundled into FounderOS. FounderOS independently implements the observed API 25 behaviors, including all 53 paths and 69 HTTP operations in BarPilot's endpoint console, differential element updates, priority conflicts, raw screen decoding, firmware blockers, and double-buffered raster uploads. The source pin and its SHA-256 are recorded in `tests/fixtures/barpilot-api25-contract.json` for reproducible review.

The emulator uses Sharp as an npm dependency for bounded server-side decoding of PNG, JPEG, GIF, WebP, and SVG assets. Sharp is licensed under Apache-2.0. Its transitive license metadata is preserved by npm in `package-lock.json`.
