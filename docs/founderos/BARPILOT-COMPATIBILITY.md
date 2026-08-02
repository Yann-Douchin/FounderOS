# BarPilot compatibility profile

FounderOS treats BarPilot as a pinned external conformance reference, not as an application architecture to import. The reviewed source is:

- repository: <https://github.com/nastea1/barpilot>
- commit: `5c4afe96e178982d7e5f95a9dfea0cf761804d80`
- file SHA-256: `b4e3e0b1b493b298ec92cf00f66c43dbe0d227b44d99e80adcce3a6819bf49c8`
- license: MIT

The full source file is not vendored. Its immutable reference, all 53 endpoint paths, all 69 HTTP operations, the status WebSocket, and the observed firmware 1.1.1 quirks are recorded in `tests/fixtures/barpilot-api25-contract.json`.

## Behaviors adopted

| Behavior | FounderOS implementation | Evidence |
| --- | --- | --- |
| Same-application updates merge by element `id` | Icon animation sends only changed pixel elements. The emulator preserves every untouched element | `test_runtime`, `test_emulator_contract` |
| Redrawing scrolling text restarts it | Runtime probes and icon frames exclude scrolling text. A full redraw occurs only after a decision change, recovery, or lease renewal | `test_runtime` |
| Timer sessions block canvas drawing at every priority | API BUSY timers return 409 from the draw endpoint | `test_emulator_contract` |
| A physically started session can be absent from `/api/busy/snapshot` | Emulator-only `physical_busy` blocker rejects draws without changing the API snapshot | `test_emulator_contract` |
| Device menus and smart-home timers block drawing | Emulator scenario controls reproduce both 409 conditions | `test_emulator_contract` |
| Another application needs strictly higher priority | Equal priority is rejected for a different owner. Equal priority is accepted for the current owner | `test_emulator_contract` |
| `/api/screen` front data is BGR888 | Emulator emits 3,456 BGR bytes. The Python client converts them to RGB | `test_display`, `test_emulator_contract` |
| `/api/screen` back data is gray8 | Emulator and client enforce 6,400 bytes | `test_display`, `test_emulator_contract` |
| Screen readback must match every supported visual asset | Server readback decodes PNG, JPEG, animated GIF, WebP, bounded static SVG, stock icons, and stock animations | `emulator_contract_test.js` |
| Status WebSocket uses an explicit enable message | `/api/status/ws` accepts `{"enable":true}` and streams bounded JSON status frames | server contract |
| Mutable image animation needs safe slot switching | Accent fallback uploads the next PNG to alternating `a` and `b` assets before drawing it | `test_display` |
| Conflicts should not trigger a hot retry loop | Runtime applies bounded exponential retry and forces a complete recovery draw after ownership returns | runtime tests |

## Complete endpoint matrix

Every row below returns a working, documented emulator response. Stateful operations persist in the private emulator state. Hardware-affecting operations change emulator state only.

| Path | Methods | Behavior |
| --- | --- | --- |
| `/api/version` | GET | API version |
| `/api/status` | GET | Grouped status |
| `/api/status/device` | GET | Device identity |
| `/api/status/firmware` | GET | Firmware identity |
| `/api/status/system` | GET | Uptime and system state |
| `/api/status/power` | GET | Battery and USB power |
| `/api/transport` | GET | Current transport |
| `/api/log_dump` | POST | Creates a readable storage file |
| `/api/name` | GET, POST | Device name |
| `/api/access` | GET, POST | API access mode, without exposing keys |
| `/api/display/brightness` | GET, POST | Brightness |
| `/api/audio/volume` | GET, POST | Volume |
| `/api/busy/snapshot` | GET, PUT | BUSY session |
| `/api/busy/profiles/busy` | GET, PUT | BUSY profile |
| `/api/busy/profiles/custom` | GET, PUT | Custom profile |
| `/api/input` | POST | Virtual physical input |
| `/api/display/draw` | POST, DELETE | Canvas draw, merge, arbitration, expiry, and clear |
| `/api/audio/play` | POST, DELETE | Emulated sound playback |
| `/api/assets/upload` | POST, DELETE | Application assets |
| `/api/screen` | GET | Front BGR888 and back gray8 readback |
| `/api/storage/list` | GET | Immediate directory children |
| `/api/storage/read` | GET | Stored bytes |
| `/api/storage/write` | POST | Bounded stored bytes |
| `/api/storage/remove` | DELETE | File or directory-tree removal |
| `/api/storage/mkdir` | POST | Directory creation |
| `/api/storage/rename` | POST | File or directory-tree rename |
| `/api/storage/status` | GET | Calculated capacity and use |
| `/api/time` | GET | Device clock |
| `/api/time/timestamp` | POST | Device clock update |
| `/api/time/timezone` | GET, POST | Timezone |
| `/api/time/tzlist` | GET | Supported timezones |
| `/api/wifi/status` | GET | Wi-Fi and IP state |
| `/api/wifi/networks` | GET | Deterministic scan results |
| `/api/wifi/connect` | POST | Emulated connection |
| `/api/wifi/disconnect` | POST | Emulated disconnection |
| `/api/ble/status` | GET | BLE status |
| `/api/ble/enable` | POST | Enable BLE state |
| `/api/ble/disable` | POST | Disable BLE state |
| `/api/ble/pairing` | DELETE | Remove pairing state |
| `/api/smart_home/pairing` | GET, POST, DELETE | Matter pairing window and erase |
| `/api/smart_home/switch` | GET, POST | Switch and startup state |
| `/api/account/info` | GET | Redacted account information |
| `/api/account/status` | GET | Account transport status |
| `/api/account/backend` | GET, PUT | Backend configuration |
| `/api/account/link` | POST | Emulated link window |
| `/api/account` | DELETE | Account unlink |
| `/api/update` | POST | Safe firmware payload simulation |
| `/api/update/check` | POST | Update check |
| `/api/update/status` | GET | Check and installation state |
| `/api/update/changelog` | GET | Version changelog |
| `/api/update/install` | POST | Safe installation simulation |
| `/api/update/abort_download` | POST | Download cancellation |
| `/api/update/autoupdate` | GET, POST | Automatic update window |

`WS /api/status/ws` is governed separately because it is not part of BarPilot's 53-path HTTP console. It requires `{"enable":true}` before streaming status.

## Architectural exclusions

FounderOS does not adopt BarPilot's single-file application structure, plaintext token persistence in browser storage, ASCII-only quick-message conversion, or its large decorative animation catalog. FounderOS remains an event-driven prioritization engine, uses the macOS Keychain for durable secrets, preserves Unicode, and keeps content animation deterministic and tied to the selected decision.

## Reproducible checks

Verify a downloaded BarPilot source file without contacting the network:

```bash
python3 tools/barpilot_compat.py --source /path/to/barpilot.html --source-only
```

Run the read-only API and screen checks against an emulator or controlled device:

```bash
python3 tools/barpilot_compat.py --host 127.0.0.1:8080
```

For authenticated hardware, pass `--config` instead of placing a token on the command line. The configured secret resolver reads the allowlisted token from the macOS Keychain.

The controlled mutation pass validates merge, equal-priority rejection, and higher-priority takeover. The blocker pass is emulator-only:

```bash
python3 tools/barpilot_compat.py \
  --host 127.0.0.1:8080 \
  --mutating \
  --emulator-blockers
```

These mutation checks temporarily own and clear the display. Run them only on an emulator or during a planned hardware acceptance window.

Exercise BarPilot's complete 53-path, 69-operation console against an isolated emulator. The command first proves that `/api/_scenario` exists, so it refuses to run the destructive matrix against physical hardware:

```bash
python3 tools/barpilot_compat.py \
  --host 127.0.0.1:8080 \
  --full-emulator-api
```

## Accent acceptance

Inspect the selected firmware profile and both screen buffers:

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  display status
```

Verify the French global-font glyphs against the baked device atlas using actual screen readback:

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  display verify-accents
```

With `display.text_rendering` set to its default `auto`, FounderOS runs the exact native check at startup. It selects and verifies the double-buffered PNG path automatically if native rendering differs. The explicit fallback command remains available for diagnostics:

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  display verify-accents --raster-fallback
```

The native or raster test must pass before physical BUSY Bar acceptance can close.
