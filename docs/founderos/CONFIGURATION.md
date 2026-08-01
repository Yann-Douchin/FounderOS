# FounderOS configuration

FounderOS uses JSON plus environment variables for secrets. Start from the tracked example:

```bash
cp founderos.example.json founderos.local.json
python3 apps/founderos.py --config founderos.local.json
```

`founderos.local.json` and `.data/` are ignored by Git. Do not put access tokens directly in JSON.

## Content-aware animated icon

FounderOS reserves an 8 by 8 pixel area beside the selected task. The icon is chosen deterministically from the event's urgency, kind, title, body, and source. Alerts, calendar events, mail, chat, code, trends, decisions, tasks, and focus each have a two-frame pixel animation. This selection never invokes an LLM.

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

Use snapshot mode when Linear, Slack, Gmail, or Google Calendar is already authorized as a Codex app, but its OAuth token must not be exposed to a local subprocess. Codex reads the authorized source, normalizes only the useful events, and writes a private snapshot under `.data/connectors/`.

```json
{
  "connectors": {
    "demo": {"enabled": false},
    "linear": {
      "enabled": true,
      "mode": "snapshot",
      "snapshot_path": ".data/connectors/linear.json",
      "max_snapshot_age_minutes": 1440,
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
- `.data/` and `founderos.local.json` remain outside Git.
- a snapshot older than `max_snapshot_age_minutes` returns no events, so stale private data cannot masquerade as a current decision.

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

## Linear

Set `LINEAR_API_KEY` and enable the connector. Personal API keys and OAuth bearer tokens are accepted by Linear's GraphQL endpoint.

```bash
export LINEAR_API_KEY='...'
```

Optional `team_keys` restricts assigned issues to selected teams. Empty means all teams. The connector reads active issues assigned to the authenticated user and never mutates them.

Reference: [Linear GraphQL authentication](https://linear.app/developers/graphql).

## Slack

Set `SLACK_BOT_TOKEN`, list the conversation IDs to watch, and grant the corresponding history scopes. Slack requires the bot to be a member of conversations it reads.

```json
{
  "slack": {
    "enabled": true,
    "token_env": "SLACK_BOT_TOKEN",
    "channel_ids": ["C0123456789"],
    "channel_names": {"C0123456789": "launch"},
    "mention_markers": ["<@U012FOUNDER>"],
    "poll_interval_seconds": 60
  }
}
```

`mention_markers` limits ordinary messages to explicit founder mentions. Urgent keywords still pass. The 60-second default respects the restrictive rate tier documented for some newly distributed Slack apps.

Reference: [Slack `conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).

## Gmail and Google Calendar

Set `GOOGLE_ACCESS_TOKEN` to an OAuth access token and enable either connector. FounderOS only makes GET requests.

Recommended least-privilege scopes:

- Gmail: `https://www.googleapis.com/auth/gmail.readonly`
- Calendar: `https://www.googleapis.com/auth/calendar.events.readonly`

The V1 runtime consumes an already-issued access token. Long-running installations should renew that token through their existing OAuth service and update the environment before restart.

References: [Gmail scopes](https://developers.google.com/identity/protocols/oauth2/scopes#gmail), [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth).

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

Both agents use the same private, local bridge. Their official `PermissionRequest` hooks create an expiring request under `.data/agents/`. FounderOS ranks that request above ordinary blockers and displays only its redacted summary. A response writes one atomic decision file that the waiting hook consumes immediately.

```json
{
  "interaction": {
    "enabled": true,
    "mode": "emulator_sse",
    "allow_key": "ok",
    "deny_key": "back"
  },
  "connectors": {
    "claude": {
      "enabled": true,
      "mode": "agent_bridge",
      "state_dir": ".data/agents",
      "usage": {"mode": "snapshot"},
      "poll_interval_seconds": 2
    },
    "chatgpt_codex": {
      "enabled": true,
      "mode": "agent_bridge",
      "state_dir": ".data/agents",
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

- `OK` returns `behavior: allow` for this request only.
- `BACK` returns `behavior: deny` with a short reason.
- no permanent permission rule is written;
- request details are reduced to a short summary and common secret patterns are masked;
- if FounderOS, the emulator input stream, or the response is unavailable for 45 seconds, the hook returns no decision and the agent resumes its normal approval prompt;
- an existing deny or ask policy still takes precedence over an allow decision.

The hook output follows the official formats documented by [Claude Code PermissionRequest hooks](https://code.claude.com/docs/en/hooks#permissionrequest) and [Codex hooks](https://developers.openai.com/codex/hooks/).

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

`interaction.mode: emulator_sse` listens to the emulator's `/events` stream and is intentionally disabled by default. The documented BUSY Bar HTTP API can inject a key with `POST /api/input`, but it does not currently expose a host-readable stream for physical button presses. Display output and quota bars work unchanged on hardware. Physical `OK` and `BACK` approvals require an official or firmware-side outbound input transport before this adapter can be enabled against the real device. The hook always falls back safely while that transport is absent.

## Optional OpenAI fallback

The normal loop needs no model and no API key. To permit bounded tie-breaking:

```bash
export OPENAI_API_KEY='...'
```

Then set `llm.enabled` to `true`. The default model is `gpt-5.6-sol`, resolved from OpenAI's current model guidance when this V1 was built. Set an explicit model in configuration if reproducibility across future releases matters.

The fallback uses the Responses API with Structured Outputs and `store: false`.

References: [Responses API quickstart](https://platform.openai.com/docs/quickstart), [Structured Outputs in Responses](https://platform.openai.com/docs/api-reference/responses-streaming/response/output_item/added).

## Planned connectors

GitHub, Stripe, Shopify, and Home Assistant are present in the configuration schema with `status: "planned"` and `enabled: false`. Enabling one in V1 fails fast with a configuration error so an unfinished adapter can never silently produce misleading events.
