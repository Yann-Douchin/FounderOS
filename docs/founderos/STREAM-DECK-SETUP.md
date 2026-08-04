# Stream Deck, FounderOS, and BUSY Bar

This document describes the installed FounderOS configuration for Stream Deck 7.5.1. It covers Stream Deck +, Stream Deck Pedal, Stream Deck Mobile on iPhone, and the local FounderOS bridge.

## Design principle

The BUSY Bar is the perception and priority surface. It displays what needs attention and the aggregate presence state.

Stream Deck is the action surface. It opens, snoozes, or closes the displayed priority, starts work modes, and controls the studio, audio, camera, Prompter, and task lighting.

This division prevents these duplications:

- no Gmail, Calendar, Slack, or Linear counter on Stream Deck
- no second priority ranking
- no direct Stream Deck action on the Hue indicator reserved for BUSY Bar
- no ambiguous presence toggle
- no release of a Calendar-owned state by a Stream Deck action

Presence priority is deterministic:

```text
recording > Calendar meeting > manual call > focus > available
```

Manual states are temporary leases. They expire automatically if the end action is forgotten. The `Finish` action releases only leases created by Stream Deck.

## Installed profiles

### FounderOS Cockpit, main Stream Deck + profile

| Position | Action |
|---|---|
| Top 1 | Open the resource for the visible FounderOS priority |
| Top 2 | Snooze for 15 minutes, or become `Deny` during a permission request |
| Top 3 | Acknowledge after a hold, or become `Allow` during a permission request |
| Top 4 | Focus for 50 minutes |
| Bottom 1 | Open Things Quick Entry |
| Bottom 2 | Activate Arc, acquire call mode, prepare video lights, and open the Meet profile |
| Bottom 3 | Open OBS and the Studio profile |
| Bottom 4 | After a hold, release manual modes and restore task lights |
| Dial 1 | Default audio output and volume |
| Dial 2 | Default input gain and mute, currently the Yeti X |
| Dial 3 | Task-light brightness |
| Dial 4 | Action wheel for Meet, Studio, Notion, Keynote, and Home |

### FounderOS Meet, manual Arc profile

[Elgato Smart Profiles](https://help.elgato.com/hc/en-us/articles/360053419071-Elgato-Stream-Deck-Smart-Profiles) match the foreground application, not a browser tab. Because Google Meet runs in Arc, binding this profile to Arc would replace Cockpit during every ordinary browsing session. FounderOS Meet is therefore entered explicitly with `Prepare Meet`.

`Prepare Meet` brings the current Arc window forward, acquires the manual call lease, prepares video lights, and opens the Meet profile. It deliberately does not navigate to a generic Meet home page, so the real Calendar or invitation link remains the meeting entry point. The `Lights` action can renew the lease when needed.

| Keys | Dials |
|---|---|
| Meet microphone, Meet camera, ScreenBrush, Things notes | Yeti X gain |
| Call lights and presence, camera tracking, Prompter, finish after a hold | Audio output, video-light brightness, Prompter |

The microphone key sends Command+D and the camera key sends Command+E, which are the [official Google Meet shortcuts on macOS](https://support.google.com/meet/answer/9298571?hl=en-GB). Their physical key codes are validated for Yann's French AZERTY keyboard.

After leaving the meeting in Arc, `End` releases the manual call, restores task lighting, and returns to Cockpit. It deliberately does not send Command+W because focus may have moved to another Arc tab.

### FounderOS Studio, OBS Smart Profile

Bringing OBS to the foreground automatically selects this profile.

| Keys | Dials |
|---|---|
| Prepare lights, start recording after a hold, pause, OBS capture | Yeti X gain |
| ScreenBrush, camera tracking, Prompter, end after a hold | Prompter, video-light brightness, Prompter settings stack |

Starting a recording acquires a `recording` lease. Ending it stops OBS, releases the lease, restores task lighting, and returns to Cockpit.

### FounderOS Pedal

| Pedal | Action |
|---|---|
| Left | Push to talk with superwhisper, physical shortcut Option + Space |
| Center | Open Things Quick Entry |
| Right | ScreenBrush |

The superwhisper shortcut uses the macOS Space keycode and Option as its only modifier. It is correct for a French AZERTY keyboard and replaces the former shortcut incorrectly labeled `⌘ V`.

### FounderOS Presentation, iPhone

The six free Stream Deck Mobile keys are used for:

1. previous slide
2. next slide
3. Prompter play or pause
4. slower Prompter speed
5. faster Prompter speed
6. ScreenBrush

Left and Right arrows are independent of the AZERTY layout.

## Visual system

Every visible action has a dedicated icon from one shared FounderOS system. The five profiles embed 60 static PNG files, each paired with an editable SVG master. This covers all keys, Pedal switches, iPhone keys, dials, Action Wheel choices, Dial Stack choices, and the active or inactive variants used by Prompter and recording controls.

The suite follows one 144 x 144 pixel grid with a midnight-blue rounded tile, a text-free white glyph, a uniform rounded stroke, and a functional accent color. FounderOS actions are blue, focus is purple, capture is green, Meet is amber, studio and risky actions are red, presentations are cyan, and neutral hardware controls use slate blue. Titles remain native Stream Deck text so labels stay editable and legible.

[Elgato specifies](https://docs.elgato.com/stream-deck/icons/getting-started/) 144 x 144 pixels for Stream Deck key and dial icons, supports SVG and PNG for static assets, and recommends keeping assets under 1 MB. Stream Deck scales this format across compatible devices. The custom quarter-screen [touch-strip canvas](https://docs.elgato.com/streamdeck/sdk/references/touch-strip-layout/) is a separate 200 x 100 pixel surface, so third-party dial layouts remain under their owning plugins rather than being replaced by decorative backgrounds.

Sources and outputs are stored under [`integrations/stream-deck-profile/assets`](../../integrations/stream-deck-profile/assets/README.md). The complete visual contact sheet is [`icon-suite-preview.png`](../../integrations/stream-deck-profile/assets/icon-suite-preview.png).

## Minimal operating ritual

The configuration intentionally centers on a few complete gestures:

- when an idea arrives, use the center pedal or `Capture`
- when a FounderOS priority arrives, use `Open`, `Snooze`, or hold `Acknowledge`
- when a Meet call starts, open its Calendar or invitation link, then use `Prepare Meet`
- when a recording starts, use `Studio`, `Prepare`, then hold `Start REC`
- when focused work starts, use `Focus 50`
- when the day or a manual mode ends, hold `Finish`

Destructive or irreversible actions require a hold. Frequent actions remain immediate.

## FounderOS bridge

The `com.yanndouchin.founderos-actions` plugin uses the official Elgato SDK. On the normal path, its Node client talks directly to the private Unix socket at `~/Library/Application Support/FounderOS/founderos-input.sock`. The two-second visual refresh therefore starts no helper process. Each exchange has an absolute three-second deadline that cannot be extended by a slow response stream. The socket parent must be owned by the current account, must not be a symbolic link, and must use mode `0700`. The socket must also be owned by the current account, must be a real Unix socket rather than a file or symbolic link, and must use mode `0600`.

The plugin cannot access connector OAuth credentials. Node never reads or receives the HMAC secret. On the normal Unix-socket path, the FounderOS runtime remains the only process holding Keychain secrets. The HMAC bridge on `127.0.0.1:8765` remains available as a compatible fallback. Only when the private socket is absent does the plugin locate and run the helper from the immutable deployed FounderOS runtime. That helper may then resolve the Keychain secret without storing it in profiles, Stream Deck settings, process arguments, child process environments, or logs. The socket is still checked on every two-second refresh. A successful fallback context is reused for ten seconds, while failures use bounded exponential backoff from five to sixty seconds, preventing a process storm while still detecting the socket immediately when it returns.

Event actions remain bound to the exact visible context identifier. FounderOS rejects a stale action.

## Physical BUSY Bar arrival

The software is ready, but Matter output remains disabled until the hardware, its local address, and its token are available. On arrival:

1. pair the BUSY Bar and obtain its local host
2. import its token into Keychain with the FounderOS command
3. set the host in the Git-ignored local configuration
4. run the firmware, screen, accent, and readback tests in [BarPilot compatibility](BARPILOT-COMPATIBILITY.md)
5. reinstall the service with physical output and without the emulator
6. validate `focus`, `manual_call`, `recording`, a Calendar meeting, and the return to `available`

The BUSY Bar retains ownership of the availability indicator. Task and video Hue scenes remain available from Stream Deck, but no key directly changes the presence indicator color.

## Build, validation, and installation

From the repository root:

```bash
cd integrations/stream-deck
npm ci
npm run icons
npm run verify
./scripts/install.zsh

cd ../..
python3 integrations/stream-deck-profile/streamdeck_profiles.py audit
python3 integrations/stream-deck-profile/streamdeck_profiles.py build
python3 integrations/stream-deck-profile/streamdeck_profiles.py validate
python3 integrations/stream-deck-profile/streamdeck_profiles.py install --apply
```

The generator reads local Hue, Camera Hub, and device identifiers only in memory. Private profiles and exports remain outside the repository and outside iCloud under `~/Library/Application Support/FounderOS/stream-deck-profiles`. Backups also remain outside Git.

## Backup and rollback

Keep the manual pre-installation backup outside the repository. Each active installation also creates a validated backup under:

```text
~/Library/Application Support/com.elgato.StreamDeck/FounderOSBackups/TIMESTAMP
```

Transactional rollback:

```bash
python3 integrations/stream-deck-profile/streamdeck_profiles.py rollback \
  "$HOME/Library/Application Support/com.elgato.StreamDeck/FounderOSBackups/TIMESTAMP" \
  --apply
```

## Dependencies and maintenance

The active profiles depend on FounderOS Actions, Volume Controller, Philips Hue, Camera Hub, and OBS Studio. Meet controls use built-in Stream Deck hotkeys, so no Zoom or Teams plugin is required.

OBS is the only application-bound Smart Profile. FounderOS Meet remains manual because Arc hosts both meetings and ordinary browsing.

Smart Profiles do not switch automatically while the Stream Deck configuration window itself is in the foreground. Close or hide that window when testing OBS switching. Test Meet by pressing `Prepare Meet`, confirming Arc activation, and then using the microphone and camera keys inside an active Meet call.
