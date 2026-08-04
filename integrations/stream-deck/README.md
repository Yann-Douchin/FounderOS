# FounderOS Actions for Stream Deck

FounderOS Actions is a thin local client for the FounderOS interaction bridge. It targets Stream Deck 7.5.1, Stream Deck +, and the official `@elgato/streamdeck` 2.1.0 SDK.

The plugin never reads Gmail, Calendar, Slack, Linear, or their credentials. It contains no OAuth flow and ranks no priorities. FounderOS remains the sole decision engine. The plugin reads only a content-free context, then sends a command bound to the exact event currently visible.

## Actions

| Action | Behavior |
|---|---|
| FounderOS Status | Shows connection, generic context type, and presence state. A press forces a refresh. |
| Open priority | Requests that FounderOS open the resource linked to the visible event. |
| Snooze 15 min | Snoozes the event. During a permission request, it automatically becomes `Deny`. |
| Acknowledge | Requires a 1,200 ms hold. During a permission request, it automatically becomes `Allow` with the same hold. |
| Allow | Dedicated advanced action with a 1,200 ms hold. |
| Deny | Dedicated advanced action. |
| FounderOS Presence | Runs an explicit presence preset, never a toggle. |

The presence presets are `focus50`, `manualCallStart`, `manualCallStop`, `recordingStart`, `recordingStop`, `recordingRenew`, and `releaseManual`. The final preset releases only leases created by Stream Deck. It can never release a Calendar-owned state.

The UUID contract and settings expected by a profile generator are defined in [action-contract.json](./action-contract.json).

## Security

The production path uses the private Unix socket at `~/Library/Application Support/FounderOS/founderos-input.sock`. Its parent directory must belong to the current account and use mode `0700`. The socket must belong to the same account and use mode `0600`. Regular files, symbolic links, and broader permissions are rejected.

The normal production path connects to that socket directly from Node with protocol version 1 operations `context`, `input`, and `presence`. It starts no child process, invokes no `plutil`, and never reads, receives, or logs the HMAC secret. Requests are limited to 4,096 bytes, responses to 65,536 bytes, and both framing and UTF-8 decoding are strict. Every exchange has an absolute three-second deadline that periodic response bytes cannot extend.

Only a genuinely absent socket enables the compatibility fallback. Invalid ownership, permissions, file type, framing, payload, or bridge errors fail closed and never reach the fallback. When fallback is needed, the plugin strictly validates the `com.founderos.runtime` LaunchAgent, then runs `apps/founderos_input.py` and `founderos.runtime.json` from the same immutable FounderOS deployment identified by a SHA-256 digest. Concurrent context fallbacks are deduplicated, successful fallback contexts have a short cooldown, and failures use bounded exponential backoff so the two-second UI refresh cannot create a process storm. Every fallback mutation invalidates the cached context before and after execution, including older requests still in flight, so the post-action refresh always reads fresh state.

The helper fallback uses the HMAC bridge fixed at `http://127.0.0.1:8765`. Non-loopback destinations, redirects, and URLs containing credentials are rejected. It resolves its secret from the existing macOS Keychain:

- service: `com.founderos.runtime`
- account: `FOUNDEROS_INPUT_SECRET`

The secret is never stored in Stream Deck settings, the manifest, profiles, logs, the socket, child process environments, or process arguments. Execution never uses a shell, has a bounded timeout, and receives a minimal environment.

On the HTTP fallback, each mutation uses canonical JSON and an HMAC-SHA256 signature. On both transports, every mutation carries a fresh timestamp and a single-use nonce. Event actions reread the context immediately before sending. FounderOS then verifies the exact event, request, and capabilities.

## FounderOS preparation

In the autonomous FounderOS configuration:

1. Add `FOUNDEROS_INPUT_SECRET` to `secrets.accounts`.
2. Enable `interaction` in `signed_http` mode on `127.0.0.1:8765`.
3. Install or reinstall the autonomous service:

```bash
python3 ../../apps/founderosctl.py \
  --config ../../founderos.autonomous.local.json \
  service install
```

The FounderOS installer automatically generates the secret inside Keychain. It never places the value in terminal history or process arguments. No manual secret handling is required.

Run a content-free diagnostic of the helper and bridge without reading the secret in Node:

```bash
npm test
node scripts/preflight-helper.mjs
```

## Build and verification

Node 20.5.1 or later is required. The manifest uses the Node 20 runtime bundled with Stream Deck 7.5.1.

```bash
npm ci
npm run verify
npm run pack
```

`npm run verify` performs the TypeScript check, unit tests, Rollup build, and official Elgato validation. The installable package is created in `dist/`.

## Transactional installation

After installing or restarting FounderOS, without any manual secret handling:

```bash
./scripts/install.zsh
```

The installer is non-interactive. It checks the deployed helper with a content-free context read, runs the full validation, prepares a temporary copy, atomically replaces the plugin, and asks Stream Deck to restart it. If copying or restarting fails, the prior version is restored.

## Development

The main entry points are:

- `src/helper-client.ts`, direct private socket transport plus validated helper fallback
- `src/bridge.ts`, injectable HMAC client reserved for compatibility tests
- `src/actions.ts`, Stream Deck actions and hold behavior
- `src/coordinator.ts`, shared refresh every two seconds
- `src/domain.ts`, context contract and presence presets

## Shared icon system

The seven plugin actions use the same text-free vector system as the personalized profiles. Status, Open, Snooze, Acknowledge, Allow, Deny, and Presence each have a dedicated glyph, a 72 x 72 key SVG, a 144 x 144 high-density SVG, and a monochrome action-list SVG. Their grid, stroke, background, and functional colors are generated from the shared source in [`../stream-deck-profile/assets`](../stream-deck-profile/assets/README.md).

Regenerate them with `npm run icons`. No secret or private setting is required to build or test the plugin.

Reference documentation: [Stream Deck SDK](https://docs.elgato.com/streamdeck/sdk/introduction/plugin-environment/), [actions](https://docs.elgato.com/streamdeck/sdk/guides/actions/), [keys](https://docs.elgato.com/streamdeck/sdk/guides/keys/), [settings](https://docs.elgato.com/streamdeck/sdk/guides/settings/), [manifest](https://docs.elgato.com/streamdeck/sdk/references/manifest/), [validation](https://docs.elgato.com/streamdeck/cli/commands/validate/), and [package creation](https://docs.elgato.com/streamdeck/cli/commands/pack/).
