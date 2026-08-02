# FounderOS configuration

FounderOS uses JSON for non-secret settings. It supports environment-provided secrets for portable deployments and the native macOS Keychain for the autonomous local service. Start from the tracked example:

```bash
cp founderos.example.json founderos.local.json
python3 apps/founderos.py --config founderos.local.json
```

`founderos.local.json` and legacy `.data/` are ignored by Git. Runtime state defaults to the platform application-state directory, for example `~/Library/Application Support/FounderOS` on macOS. Do not put access tokens directly in JSON.

Before starting the long-running process, execute a live connector preflight. It polls every enabled source once and exits with code `3` unless all report `healthy`:

```bash
python3 apps/founderos.py --config founderos.local.json --once --dry-run --require-healthy
```

Production one-shot runs enforce the same rule automatically for every connector marked `critical`.

## Autonomous macOS deployment

Start from `founderos.macos.example.json` and save the deployment settings in an ignored local file:

```bash
cp founderos.macos.example.json founderos.autonomous.local.json
```

The `secrets.accounts` list is an explicit allowlist. Every secret used by an enabled API connector must appear there. When the Keychain provider is selected, credentials resolve only from that persistent store. Ambient environment variables are never a fallback. This prevents stale process state from overriding or impersonating rotated credentials.

### Linear OAuth

Create a private Linear OAuth application with this exact redirect URI:

```text
http://127.0.0.1:8766/oauth/callback
```

Request only the `read` scope, leave webhooks disabled, and authorize it with:

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  auth linear \
  --client-id YOUR_LINEAR_CLIENT_ID
```

FounderOS stores the client ID and refresh token in the Keychain. Linear refresh-token rotation is persisted before the corresponding access token can be used.

### Google OAuth

In a Google Cloud project owned by the deployment organization, enable the APIs used by the configured Google connectors. Gmail and Calendar are the live default. Enable Drive or Sheets only before activating those optional connectors. Configure the OAuth consent screen, then create and download a Desktop app OAuth client. Web application clients are rejected because the loopback port is intentionally ephemeral.

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  auth google \
  --client-json ~/Downloads/client_secret.json
```

The authorization request computes the smallest supported read-only scope set from enabled connectors, requests offline access, uses PKCE, validates a random state value, accepts the callback only on `127.0.0.1`, and rejects a partial scope grant before storing credentials. Use `--include-drive` or `--include-sheets` only when provisioning those connectors before enabling them.

### Slack app

Create the private Slack app from `deploy/slack-app-manifest.yaml`, install it in the intended workspace, and invite the FounderOS bot to every allowlisted conversation. The manifest grants only `channels:history` and `groups:history`. It enables neither events, commands, socket mode, nor message writes.

Copy the resulting bot token and immediately import it from the macOS clipboard:

```bash
python3 apps/founderosctl.py \
  --config founderos.autonomous.local.json \
  secret import-clipboard SLACK_BOT_TOKEN
```

This command never prints the token and clears the clipboard after the Keychain write. `secret status` reports only `configured` or `missing`.

### Preflight and LaunchAgents

No service is installed until all enabled connectors complete one healthy read-only poll:

```bash
python3 apps/founderos.py \
  --config founderos.autonomous.local.json \
  --once --dry-run --require-healthy

npm ci
npm ci --prefix web
npm run build

install -d -m 700 "$HOME/Library/Application Support/FounderOS/config"
install -m 600 \
  founderos.autonomous.local.json \
  "$HOME/Library/Application Support/FounderOS/config/founderos.autonomous.json"

python3 apps/founderosctl.py \
  --config "$HOME/Library/Application Support/FounderOS/config/founderos.autonomous.json" \
  service install
```

The emulator requires Node.js 20.9.0 or newer. The install command verifies that version, then delegates packaging to `apps/founderos_install.zsh`. It archives tracked runtime sources from the current committed `HEAD`, adds the already validated `web/dist` build, the locked Sharp image decoder, stock animation assets needed by `/api/screen`, and the private configuration, then creates a content-addressed deployment below `~/Library/Application Support/FounderOS/deployments`. Launchd never executes the source checkout.

The installation creates two private user LaunchAgents. `com.founderos.busybar-emulator` binds the emulator to `127.0.0.1` and stores emulator state under the private FounderOS application-state directory, never in the checkout. `com.founderos.runtime` starts FounderOS after login and restarts it after unsuccessful exits. Their plists contain executable paths and non-secret environment settings only. The installer snapshots both prior definitions, retries transient `launchctl bootstrap` failures, and waits for a fresh `running` heartbeat whose PID matches the process reported by launchd, whose display is healthy, and whose connectors have completed healthy polls. If readiness fails, it restores the exact prior plists and loaded states. An old heartbeat or a live but degraded process cannot hide a failed start.

```bash
python3 apps/founderosctl.py \
  --config "$HOME/Library/Application Support/FounderOS/config/founderos.autonomous.json" \
  service status
python3 apps/founderosctl.py \
  --config "$HOME/Library/Application Support/FounderOS/config/founderos.autonomous.json" \
  service uninstall
```

The heartbeat is mode `0600` and contains source names, counts, state, and boolean error presence only. It never contains task titles, message bodies, email subjects, remote error text, or credentials. Logs and state live under `~/Library/Application Support/FounderOS` with private directory modes.

When using physical hardware, set its host in the local configuration and pass `service install --skip-emulator`. A non-loopback production display requires a Keychain-allowlisted BUSY Bar API token. Plain HTTP also requires the explicit `display.allow_insecure_http` risk acceptance.

## Display lifecycle and firmware compatibility

The display adapter uses element leases and differential updates so a one-pixel icon change never resends scrolling task text. A normal refresh sends one non-scrolling probe element. A complete frame is sent after a decision change, a transport or priority failure, a structural layout transition, or lease renewal.

```json
{
  "display": {
    "lease_seconds": 300,
    "lease_refresh_ratio": 0.8,
    "conflict_retry_seconds": 2,
    "conflict_retry_max_seconds": 30,
    "clear_on_shutdown": true,
    "text_rendering": "auto"
  }
}
```

- `lease_seconds` bounds how long a frame can remain after an ungraceful process exit.
- `lease_refresh_ratio` renews the complete frame after the selected fraction of its lease.
- conflict retries use bounded exponential delay and force a complete recovery frame once drawing is possible again.
- graceful shutdown clears only the configured FounderOS application namespace.
- `text_rendering` defaults to `auto`. At startup, FounderOS reads the firmware identity and compares `Échéance`, `décision`, `ingénierie`, `œ`, and `’` pixel by pixel through `/api/screen`. It selects `native` only after an exact successful readback. Otherwise it selects `raster_non_ascii`, verifies that path, and stores the result in a private mode `0600` cache keyed by host and firmware version. The fallback uploads Unicode text as alternating PNG assets and scrolls it through differential image updates.

Run the firmware, screen, and accent checks described in [BarPilot compatibility](BARPILOT-COMPATIBILITY.md) before closing physical-device acceptance.

## Content-aware animated icon

FounderOS reserves an 8 by 8 pixel area beside the selected task. The icon is chosen deterministically from the event's urgency, kind, title, body, and source. The governed visual vocabulary contains exactly six two-frame states: waiting, blocked, decision, meeting, validation, and success. This selection never invokes an LLM.

```json
{
  "display": {
    "content_icon": {
      "enabled": true,
      "frame_seconds": 1.0
    }
  }
}
```

`frame_seconds` controls how long each animation frame remains visible and must be greater than zero. Set `enabled` to `false` to restore the text-only layout. Icons use only the firmware's standard one-pixel `rectangle` elements and stable element IDs, so the same frames work in the emulator and on the physical BUSY Bar.

## Codex connector bridge

Use snapshot mode for a user-triggered transfer from an authorized app when its OAuth token cannot be exposed to a subprocess. Publish the normalized event array with `apps/founderos_snapshot.py`. It writes atomically below `FOUNDEROS_STATE_DIR`.

```json
{
  "connectors": {
    "demo": {"enabled": false},
    "linear": {
      "enabled": true,
      "mode": "snapshot",
      "snapshot_path": "/absolute/private/state/connectors/linear.json",
      "max_snapshot_age_minutes": 10,
      "poll_interval_seconds": 30
    }
  }
}
```

Each snapshot is a complete source state:

```json
{
  "schema_version": 1,
  "source": "linear",
  "generated_at": "2026-08-01T12:00:00Z",
  "events": [
    {
      "id": "linear:launch",
      "title": "Launch blocked",
      "priority": 94,
      "action_required": true,
      "kind": "blocker",
      "expires_at": "2026-08-02T12:00:00Z"
    }
  ]
}
```

Snapshot mode has three safety properties:

- OAuth credentials stay inside the authorized connector.
- private state remains outside Git and the iCloud checkout.
- a missing or stale snapshot creates a source-health incident while the last known events remain available. It cannot produce a false `ALL CLEAR`.

This bridge is appropriate for progressive, user-triggered synchronization. For an autonomous background service, keep `mode` set to `api` and configure the least-privilege credentials described below.

## Credential-free demo

```bash
python3 apps/founderos.py --demo
python3 apps/founderos.py --demo --scenario linear_blocker
python3 apps/founderos.py --demo --scenario calendar
python3 apps/founderos.py --demo --scenario gmail
python3 apps/founderos.py --demo --scenario slack
python3 apps/founderos.py --demo --scenario clear
```

## Obligation closure engine

Production enables the deterministic closure layer. Connector events are observations. The persisted obligation is the governed unit sent to ranking.

```json
{
  "closure": {
    "enabled": true,
    "rank_raw_events": false,
    "default_owner": "Yann",
    "self_aliases": ["self", "me", "Yann"],
    "source_priority_cap": 72,
    "burst_window_minutes": 240,
    "burst_threshold": 4,
    "audit_max_entries": 100000,
    "capacity": {
      "due_day_threshold": 5,
      "require_handoff_when_unavailable": true
    },
    "proof_profiles": {
      "release": {
        "required_categories": ["deployment", "analytics", "market", "language", "pricing", "device"],
        "minimum_categories": 6,
        "required_scopes": {
          "market": ["FR", "ES"],
          "language": ["fr", "es"]
        }
      }
    }
  }
}
```

The mode `0600` SQLite ledger and atomic JSON snapshot live below `FOUNDEROS_STATE_DIR`. The snapshot contains structured obligations and relationship memory, not connector credentials. The loopback emulator exposes it only through `GET /api/_founderos/obligations` for the **Obligations** tab.

Use the audited operator commands to correct source inference, capture the next meeting action, attach scoped evidence, record a delegate, or set a relationship cooling period:

```bash
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation list
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation action OBLIGATION_ID "Send the validated proposal" --actor Yann
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation delegate OBLIGATION_ID Sam
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation evidence OBLIGATION_ID market --scope FR
python3 apps/founderosctl.py --config founderos.autonomous.local.json relationship show partner.example
python3 apps/founderosctl.py --config founderos.autonomous.local.json relationship set partner.example --stage design_partner --next-decision "Approve rollout" --cooling-off-until 2026-08-20T08:00:00+02:00
```

Manual close and cancellation remain stable across polling timestamp changes. A new event or a semantic source change reopens the obligation. `stale_after_days` limits the initial import of old observations, but never expires an open obligation already accepted into the ledger. New contradictory state from the same source observation retracts its older evidence. Source evidence expires deterministically even if the source event disappears, while operator evidence can remain valid without an expiry. The audit retains the newest `audit_max_entries` rows, with a validated range of 1,000 to 10,000,000, so unattended polling cannot grow it without bound. Relationship fields set through the CLI remain pinned across later source polls. Pass `--next-decision none` to clear a pinned decision.

## Linear

Set `LINEAR_API_KEY` and enable the connector. Personal API keys are sent as the raw `Authorization` value required by Linear. For an OAuth access token, set `auth_scheme` to `bearer`.

```bash
export LINEAR_API_KEY='...'
```

Use `assigned` scope for a personal queue. Use `portfolio` scope for founder oversight of explicitly authorized teams:

```json
{
  "linear": {
    "enabled": true,
    "mode": "api",
    "token_env": "LINEAR_API_KEY",
    "auth_scheme": "api_key",
    "scope": "portfolio",
    "team_keys": ["BUSY"],
    "portfolio_priority_ceiling": 2,
    "portfolio_due_horizon_days": 14,
    "rollup_projects": true
  }
}
```

Portfolio scope retains issues assigned to the authenticated user and adds team issues that are blocked, urgent or high priority, or due inside the configured horizon. Two or more relevant issues in the same project collapse into one project-risk event with the earliest deadline and current owners. Pagination is bounded by `page_size`, `max_issues`, `max_pages`, and the total poll deadline. FounderOS never mutates Linear.

References: [Linear GraphQL authentication](https://linear.app/developers/graphql), [filtering](https://linear.app/developers/filtering), and [pagination](https://linear.app/developers/pagination).

## Slack

Set `SLACK_BOT_TOKEN`, list the conversation IDs to watch, and grant the corresponding history scopes. Slack requires the bot to be a member of conversations it reads.

```json
{
  "slack": {
    "enabled": true,
    "token_env": "SLACK_BOT_TOKEN",
    "channel_ids": ["C0123456789"],
    "channel_names": {"C0123456789": "launch"},
    "channel_projects": {"C0123456789": "Launch"},
    "channel_relationships": {"C0123456789": "partner.example"},
    "channel_customers": {"C0123456789": "Design Partner"},
    "mention_markers": ["<@U012FOUNDER>"],
    "self_user_ids": ["U012FOUNDER"],
    "max_threads_per_poll": 10,
    "poll_interval_seconds": 60
  }
}
```

`mention_markers` limits ordinary messages to explicit founder mentions. Urgent incidents, dependency phrases such as `waiting for` or `need access`, explicit decision requests, and outgoing founder promises still pass. FounderOS reads a bounded number of reply threads through `conversations.replies`, so a commitment made inside a recently observed thread is not lost. `self_user_ids` identifies outgoing founder commitments. `channel_projects`, `channel_relationships`, and `channel_customers` are explicit because a channel name is not automatically a project or customer identity. These signal dictionaries are configurable through `urgent_keywords`, `risk_keywords`, `decision_keywords`, and `promise_keywords`. Pagination and the total poll deadline are bounded.

Reference: [Slack `conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).

## Gmail and Google Calendar

For a short run, set `GOOGLE_ACCESS_TOKEN`. For an autonomous service, configure `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`. FounderOS refreshes access tokens in memory and never persists them.

Recommended least-privilege scopes:

- Gmail: `https://www.googleapis.com/auth/gmail.readonly`
- Calendar: `https://www.googleapis.com/auth/calendar.events.readonly`
- Drive, only when enabled: `https://www.googleapis.com/auth/drive.metadata.readonly`
- Sheets, only when enabled: `https://www.googleapis.com/auth/spreadsheets.readonly`

The Google authorization command derives the smallest scope set from enabled connectors. `--include-drive` and `--include-sheets` are explicit opt-ins when provisioning those connectors before activation.

Gmail message metadata is fetched concurrently with bounded workers and a total poll deadline. Unread mail is not automatically actionable. A deterministic classifier separates explicit requests, important messages, received attachments, and informational mail such as invoices, receipts, refunds, and newsletters. Explicit phrases such as `aucune action requise` override embedded action words. VIP entries match an exact address or an exact domain, never an address substring. Production should configure two bounded queries, one incoming query and one explicit outgoing-promise query. Tune the classifier with `vip_senders`, `action_keywords`, `promise_keywords`, `non_action_keywords`, `fyi_keywords`, and `urgent_keywords`.

Calendar includes timed and all-day events. Meetings whose titles match `readiness_keywords` become a `PREP` action during the configurable 30-minute readiness window. Important completed meetings become a governed follow-up transition. All-day absences can map an explicit title marker to a person through `availability_owner_map`, or use a `founderos_owner` extended property. This gives launch, customer, investor, contract, strategy, and handoff boundaries precedence without calling a model. Calendar follows page tokens up to `max_events`, `max_pages`, and one total poll deadline. Google connectors invalidate and refresh once after an HTTP `401` when refresh credentials are available.

References: [Gmail scopes](https://developers.google.com/identity/protocols/oauth2/scopes#gmail), [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth).

## Calendar busy indicator through BUSY Bar Matter

Matter supplies the local device state and transport. A smart-home controller still owns the cross-device automation that maps the BUSY Bar switch to the Hue light. Home Assistant is not required when Apple Home or Google Home already controls both accessories.

The governed path is:

```text
Google Calendar event
        |
deterministic occupancy policy
        |
BUSY Bar /api/smart_home/switch
        |
Matter controller automation
        |
Desk Recording Indicator, red or off
```

Pair the physical BUSY Bar with the same Matter home that contains the Hue accessory. Then add two controller automations:

1. When the BUSY Bar switch turns on, turn `Desk Recording Indicator` on, set it to red, and select the desired brightness.
2. When the BUSY Bar switch turns off, turn `Desk Recording Indicator` off.

Apple Home can be that controller. Home Assistant is an alternative when more complex conditions, diagnostics, or cross-platform control are needed. Do not configure either automation to start a BUSY session on the device. Firmware BUSY sessions can block display drawing, while the smart-home switch leaves FounderOS free to render the selected obligation.

Enable the output only after the physical device is commissioned and its LAN API token is stored in the same secret provider as the runtime:

```json
{
  "automations": {
    "calendar_busy_indicator": {
      "enabled": true,
      "mode": "busybar_matter",
      "host": "http://192.168.1.42",
      "api_token_env": "BUSY_API_TOKEN",
      "allow_insecure_http": true,
      "require_pairing": true,
      "include_all_day": false,
      "include_tentative": true,
      "off_delay_seconds": 15,
      "verify_interval_seconds": 60,
      "retry_seconds": 5,
      "retry_max_seconds": 60
    }
  }
}
```

For a Keychain deployment, `BUSY_API_TOKEN` must also appear in `secrets.accounts`. The explicit `allow_insecure_http` setting acknowledges that the device LAN API uses HTTP on deployments without a local TLS proxy. The API token remains mandatory for any non-loopback production target.

Before enabling the output, verify that the configured physical endpoint reports at least one Matter fabric:

```bash
python3 apps/founderosctl.py --config founderos.autonomous.local.json secret import-clipboard BUSY_API_TOKEN
python3 apps/founderosctl.py --config founderos.autonomous.local.json display matter-status
```

This command reports only commissioning count, pairing status, and switch state. It never opens a pairing window and never prints a QR payload or manual commissioning code.

The occupancy policy is deterministic:

- an event is busy only while its start and end interval contains the current time;
- cancelled, self-declined, and `transparent` Calendar events are excluded;
- tentative events count by default, all-day events do not;
- an unavailable or stale Calendar source holds the last applied state instead of falsely advertising availability;
- a 15-second off delay absorbs adjacent meetings and short polling transitions;
- writes are confirmed by reading the switch back, then retried with bounded exponential backoff;
- automation health is part of the private service heartbeat and the production health gate.

The public examples keep this output disabled because a repository cannot safely invent the physical BUSY Bar address, token, Matter fabric, Hue accessory, or home. Those are deployment facts, not application defaults.

Official references: [BUSY Bar HTTP API reference](https://api.busy.app/busybar/docs), [BUSY Bar smart-home capabilities](https://busy.app/), [BUSY Bar Matter architecture](https://blog.busy.app/new-design-busy-bar/), [Connectivity Standards Alliance certification](https://csa-iot.org/csa_product/busy-bar/).

## LinkedIn

LinkedIn does not expose a general personal notification stream through open permissions. Many webhook products require an approved use case. FounderOS therefore consumes a user-controlled HTTPS JSON bridge instead of pretending that a universal notifications endpoint exists.

The bridge response must be:

```json
{
  "events": [
    {
      "id": "linkedin:lead:123",
      "title": "Acme replied to founder outreach",
      "priority": 72,
      "action_required": true,
      "kind": "message",
      "expires_at": "2026-08-01T18:00:00Z"
    }
  ]
}
```

References: [LinkedIn API access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access), [LinkedIn webhook availability](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/webhook-validation).

## Claude and ChatGPT/Codex

Both agents use the same private local bridge. Their `PermissionRequest` hooks create an expiring request below the platform FounderOS state directory. FounderOS gives active permissions strict precedence and displays only a redacted summary. A response creates one exclusive decision file that the waiting hook consumes immediately.

```json
{
  "interaction": {
    "enabled": true,
    "mode": "signed_http",
    "listen_host": "127.0.0.1",
    "listen_port": 8765,
    "secret_env": "FOUNDEROS_INPUT_SECRET",
    "allow_key": "ok",
    "deny_key": "back"
  },
  "connectors": {
    "claude": {
      "enabled": true,
      "mode": "agent_bridge",
      "usage": {"mode": "snapshot"},
      "poll_interval_seconds": 2
    },
    "chatgpt_codex": {
      "enabled": true,
      "mode": "agent_bridge",
      "usage": {
        "mode": "codex_app_server",
        "codex_binary": "",
        "timeout_seconds": 5,
        "ttl_seconds": 120,
        "refresh_seconds": 60
      },
      "poll_interval_seconds": 2
    }
  }
}
```

The repository includes both hook definitions:

- Claude loads `.claude/settings.json` and invokes `apps/agent_permission_hook.py --provider claude`.
- Codex loads `.codex/hooks.json` and invokes the same hook with `--provider chatgpt_codex`. Open `/hooks` in Codex and trust the exact project-local definition before first use.

The safety behavior is identical for both agents:

- a trusted `OK` input returns `behavior: allow` for the exact request only.
- a trusted `BACK` input returns `behavior: deny` with a short reason.
- unsigned, replayed, expired, or context-mismatched input is ignored.
- no permanent permission rule is written;
- request details are reduced to a short summary and common secret patterns are masked;
- if FounderOS or the trusted input adapter is unavailable for 45 seconds, the hook returns no decision and the agent resumes its normal approval prompt;
- an existing deny or ask policy still takes precedence over an allow decision.

The hook output follows the official formats documented by [Claude Code PermissionRequest hooks](https://code.claude.com/docs/en/hooks#permissionrequest) and [Codex hooks](https://learn.chatgpt.com/docs/hooks#permissionrequest).

### Live usage limits

ChatGPT/Codex quota data comes from the stable `account/rateLimits/read` method of the local [Codex app-server](https://developers.openai.com/codex/app-server/). FounderOS starts the bundled or installed Codex executable, reuses its existing local login, and receives only plan and quota-window fields. It never receives or stores an OAuth token. Set `FOUNDEROS_CODEX_BINARY` when `codex` is not on `PATH` and the ChatGPT application is not installed in its standard macOS location.

Permission files are checked every 2 seconds. `usage.refresh_seconds` independently limits the app-server quota read to once per minute, while the last valid short-lived quota snapshot remains visible.

Claude Code documents the subscription bars in `/usage`, but does not expose a stable machine-readable personal quota endpoint. FounderOS therefore accepts a short-lived supported snapshot instead of scraping the Claude settings page or private local files:

```bash
python3 apps/agent_bridge.py usage \
  --provider claude \
  --window 5H:32 \
  --window SEM:71 \
  --ttl-seconds 900
```

An external collector may run that command after reading the values from a supported source. When the snapshot expires, its bars disappear. See [Claude Code usage and costs](https://code.claude.com/docs/en/costs#using-the-usage-command).

For a visual test without invoking an agent:

```bash
python3 apps/agent_bridge.py request \
  --provider claude \
  --tool Bash \
  --summary "Lancer les tests ?"
```

### Input boundary

`interaction.mode: emulator_sse` is development telemetry only. Its input is always untrusted and cannot mutate an event. Production uses `signed_http`, bound to loopback, with at least a 32-byte secret from `FOUNDEROS_INPUT_SECRET`. Generate one with `python3 apps/founderos_input.py --generate-secret`. The reference client first reads the exact context and then signs a fresh request: `python3 apps/founderos_input.py ok` or `python3 apps/founderos_input.py back`. A physical button transport must implement that same contract. The hook falls back safely while no trusted transport exists.

## Optional OpenAI fallback

The normal loop needs no model and no API key. To permit bounded tie-breaking:

```bash
export OPENAI_API_KEY='...'
```

Then set `llm.enabled` to `true`. The default model is `gpt-5.6-sol`, resolved from OpenAI's current model guidance when this V1 was built. Set an explicit model in configuration if reproducibility across future releases matters.

The fallback uses the Responses API with Structured Outputs and `store: false`.

References: [Responses API quickstart](https://platform.openai.com/docs/quickstart), [Structured Outputs in Responses](https://platform.openai.com/docs/api-reference/responses-streaming/response/output_item/added).

## Additional closure connectors

The following adapters are implemented and disabled by default. Enabling one requires its explicit allowlists and least-privilege credential in the configured secret provider.

| Connector | Closure value | Required configuration |
| --- | --- | --- |
| Notion | Decisions and approved document evidence | Integration token plus a database allowlist, or an explicit all-shared-pages opt-in |
| Drive | Document metadata and custom scoped evidence properties | Google read-only metadata scope plus a folder allowlist, or an explicit all-files opt-in |
| Sheets | Feedback registers and proof matrices | Google read-only values scope, spreadsheet IDs, ranges, and column mappings |
| GitHub | Reviews, code checks, and configured deployment workflows | Fine-grained token, repository allowlist, optional project map and deployment workflow names |
| Deployment | Deployment gate state from an operator-owned endpoint | HTTPS endpoint and optional bearer token |
| Sentry | Unresolved production regressions | Read-only token, organization, and project allowlist |
| PostHog | Configured analytics thresholds and passing evidence | Personal read token, project ID, bounded query objects, value paths, and comparators |
| Shopify | Merchant access and catalog readiness | Narrow Admin API token, exact shop host, and required scope list |
| Superhuman | Reminder obligations through a real Gmail label or query | Google Gmail read scope and the user's actual reminder query |
| Stripe | Overdue invoices, actionable disputes, and resolved evidence | Restricted read key |
| Home Assistant | Opt-in availability and handoff context | Loopback or HTTPS endpoint, long-lived token, and entity allowlist |

Successful generic CI does not count as deployment proof. GitHub accepts deployment evidence only when the case-insensitive workflow name exactly matches a configured `deployment_workflows` entry. PostHog and Sheets checks are declarative and bounded. FounderOS never invents a market, language, device, repository, customer, or Home Assistant entity.
