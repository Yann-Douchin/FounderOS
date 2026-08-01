# FounderOS configuration

FounderOS uses JSON plus environment variables for secrets. Start from the tracked example:

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
    "mention_markers": ["<@U012FOUNDER>"],
    "poll_interval_seconds": 60
  }
}
```

`mention_markers` limits ordinary messages to explicit founder mentions. Urgent incidents, dependency phrases such as `waiting for` or `need access`, and explicit decision requests still pass. These signal dictionaries are configurable through `urgent_keywords`, `risk_keywords`, and `decision_keywords`. Every surfaced message records the matched signal, and pagination plus the total poll deadline are bounded. The 60-second default respects the restrictive rate tier documented for some newly distributed Slack apps.

Reference: [Slack `conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).

## Gmail and Google Calendar

For a short run, set `GOOGLE_ACCESS_TOKEN`. For an autonomous service, configure `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`. FounderOS refreshes access tokens in memory and never persists them.

Recommended least-privilege scopes:

- Gmail: `https://www.googleapis.com/auth/gmail.readonly`
- Calendar: `https://www.googleapis.com/auth/calendar.events.readonly`

Gmail message metadata is fetched concurrently with bounded workers and a total poll deadline. Unread mail is not automatically actionable. A deterministic classifier separates explicit requests, important messages, received attachments, and informational mail such as invoices, receipts, refunds, and newsletters. Explicit phrases such as `aucune action requise` override embedded action words. VIP entries match an exact address or an exact domain, never an address substring. Tune the classifier with `vip_senders`, `action_keywords`, `non_action_keywords`, `fyi_keywords`, and `urgent_keywords`.

Calendar includes timed and all-day events. Meetings whose titles match `readiness_keywords` become a `PRÉPA` action during the configurable 30-minute readiness window. This gives launch, customer, investor, contract, and strategy boundaries precedence without calling a model. Calendar follows page tokens up to `max_events`, `max_pages`, and one total poll deadline. Both Google connectors invalidate and refresh once after an HTTP `401` when refresh credentials are available.

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

## Planned connectors

GitHub, Stripe, Shopify, and Home Assistant are present in the configuration schema with `status: "planned"` and `enabled: false`. Enabling one in V1 fails fast with a configuration error so an unfinished adapter can never silently produce misleading events.
