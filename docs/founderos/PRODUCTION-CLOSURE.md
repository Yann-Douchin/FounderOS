# FounderOS production closure

This register converts the hostile review into owned controls, executable evidence, and release gates. A finding is closed only when the unsafe behavior is impossible by default and a regression test protects the boundary.

## Closure states

- `Closed`: implemented and protected by automated evidence.
- `Controlled`: the unsafe path is disabled, and activation requires a documented external capability.
- `Release gate`: operational evidence required for a particular deployment, not an unresolved code defect.

## Finding register

| ID | Finding | Control and exit criterion | Evidence | Owner | State |
| --- | --- | --- | --- | --- | --- |
| FOS-SEC-001 | Emulator input could approve a request | Emulator SSE is untrusted. Mutations require loopback HMAC, exact event and request IDs, a fresh timestamp, and a one-use nonce | `test_production_security`, `test_agent_bridge` | Security | Closed |
| FOS-SEC-002 | A decision file could be overwritten | Decisions use exclusive creation. Provider, request, schema, and expiry are verified | `test_agent_bridge` | Security | Closed |
| FOS-SEC-003 | An invisible or display-conflicted event could still accept input | Input context exists only for the exact event confirmed on screen and is invalidated before every redraw or action | `test_runtime_visibility` | Security | Closed |
| FOS-SEC-004 | A local CLI could bypass signed device input | The decision CLI is removed. Bridge decisions require signed transport metadata, nonce, request binding, and a fresh decision window | `test_agent_bridge`, `production_check.py` | Security | Closed |
| FOS-SEC-005 | The project hook failed under the macOS system Python | Hook dependencies remain Python 3.9 compatible and the production check executes the exact configured interpreter | `production_check.py` | Security | Closed |
| FOS-PRIV-001 | Private state lived inside an iCloud checkout | State defaults outside the checkout. Production rejects a state root inside source, requires every private path below that root, and applies private POSIX modes where supported | `test_production_security`, `production_check.py` | Operations | Closed |
| FOS-DATA-001 | Stale data looked like an empty source | Typed failures preserve last-known events and publish a visible source-health event | `test_connectors`, `test_scheduler_health` | Integrations | Closed |
| FOS-DATA-002 | Snapshots lacked a governed publication path | The snapshot CLI validates and writes atomically outside Git | `apps/founderos_snapshot.py` | Integrations | Closed |
| FOS-DATA-003 | Founder-only Linear scope produced a false calm while delegated critical work remained open | Production uses an allowlisted portfolio scope with bounded pagination, relevance filters, owner metadata, and project-risk rollups | `test_connector_polling` | Product | Closed |
| FOS-DATA-004 | Every unread email was treated as an action | Gmail deterministically separates requests, important decisions, artifacts, and FYI billing or automated mail | `test_connectors` | Product | Closed |
| FOS-DATA-005 | Gmail VIP substring matches and negated requests created false actions | VIPs require an exact address or domain, and explicit non-action language overrides embedded action keywords | `test_connectors` | Product | Closed |
| FOS-DATA-006 | Linear treated `Unblocked` as blocked | Blocker detection uses exact multilingual tokens and rejects explicit unblocked states | `test_connectors` | Product | Closed |
| FOS-CONN-001 | Google tokens expired without refresh | Gmail and Calendar refresh OAuth credentials in memory, cache them, and invalidate on `401` | `test_google_oauth` | Integrations | Closed |
| FOS-CONN-002 | HTTP work had weak bounds and could leak authorization across redirects | Connector, display, and LLM clients disable redirects, validate schemes, cap responses and errors, and apply finite timeouts and retries | `test_http_client`, `test_display`, `test_llm_fallback` | Integrations | Closed |
| FOS-CONN-003 | Linear authentication conflated personal keys and OAuth tokens | `api_key` sends Linear's required raw header, while explicit `bearer` mode handles OAuth | `test_connector_polling` | Integrations | Closed |
| FOS-CONN-004 | Calendar could silently omit later pages, and Google detail work lacked a total deadline | Calendar follows bounded page tokens, while Gmail and Calendar share strict per-poll deadlines across detail and refresh work | `test_connector_polling` | Integrations | Closed |
| FOS-CONN-005 | Remote HTTP error bodies could leak private data into source-health logs or pixels | Connector and display errors retain only the method, sanitized endpoint path, and status code | `test_http_client` | Security | Closed |
| FOS-RUN-001 | Serial polling froze the runtime | Connector workers are concurrent. Normal ticks never wait, and overdue polls create incidents | `test_scheduler_health` | Runtime | Closed |
| FOS-RUN-002 | Corrupt or unbounded ranking memory could crash or grow forever | Memory tolerates malformed sections, timestamps acknowledgements, resurfaces updated events, prunes by age and count, and fsyncs atomic writes | `test_memory` | Runtime | Closed |
| FOS-RANK-001 | Permission priority was only a bonus | Permissions form a strict selection partition and never invoke the LLM tie breaker | `test_ranking` | Ranking | Closed |
| FOS-RANK-002 | Display hold delayed permissions | Permission requests bypass the hold | `test_runtime` | Ranking | Closed |
| FOS-DISP-001 | Hardware merge left stale elements | Clear the application before structural transitions | `test_runtime` | Display | Closed |
| FOS-DISP-002 | Display calls omitted auth and version | Send the token when configured and API version `25.0.0` | `test_display` | Display | Closed |
| FOS-TIME-001 | Linear dates expired around UTC midnight | Resolve date-only deadlines at local end of day | `test_connectors` | Data model | Closed |
| FOS-TIME-002 | All-day Calendar events disappeared | Normalize date-only events with exclusive local end dates | `test_connectors` | Data model | Closed |
| FOS-UX-001 | Normal events had no actions | Trusted input can acknowledge, snooze, and queue open actions | `test_runtime` | Product | Closed |
| FOS-I18N-001 | Accents were stripped or rendered as replacement glyphs | Normalize all event text to NFC, preserve labels, use the global scrolling font for non-ASCII titles, and verify the complete French glyph set | `test_models`, `test_display`, `production_check.py` | Display | Closed |
| FOS-I18N-002 | Uppercase accents existed in the atlas but were clipped above its visible line | Fold only top overshoot pixels into the first scanline while preserving the baseline, then reject every negative French glyph offset | `test_display`, `test_capture`, `production_check.py` | Display | Closed |
| FOS-CONFIG-001 | The CLI silently replaced a configured display host with port 8080 | `--host` is now an explicit override only. Omitting it preserves the configuration file | `test_cli` | Runtime | Closed |
| FOS-OPS-001 | No delivery gate existed | CI tests Python, builds the frontend, checks invariants, and runs the demo | `.github/workflows/ci.yml` | Maintainers | Closed |
| FOS-OPS-002 | Gallery claims referenced missing files | A dependency-free SSE exporter produces seven synthetic 720×160 captures, and production checks verify their signatures and dimensions | `test_capture`, `production_check.py` | Maintainers | Closed |
| FOS-OPS-003 | A one-shot run could exit successfully while real connectors were degraded | `--require-healthy` turns source health into an executable preflight, and production one-shot runs fail on unhealthy critical sources | `test_cli` | Operations | Closed |

## Explicit capability boundary

The emulator `/events` endpoint is useful for visual testing but provides no authenticated input. The reviewed BUSY Bar HTTP contract provides display APIs, not a host-readable physical button stream. FounderOS does not treat those events as authoritative.

The production input contract is `SignedInputListener`:

1. The adapter authenticates to `GET /context` on loopback.
2. It reads the exact selected `event_id` and optional `request_id`.
3. It sends the key, context, Unix timestamp, and random nonce to `POST /input`.
4. It signs the canonical JSON body with `FOUNDEROS_INPUT_SECRET`.

`apps/founderos_input.py` is the reference client. A physical transport adapter may implement the same contract. Bridging emulator SSE into this trusted endpoint is prohibited.

## Hook behavior

The Codex hook follows the official `PermissionRequest` contract. `allow` proceeds without the normal prompt, `deny` blocks, and no decision returns to the normal approval flow. FounderOS emits an empty object on timeout or internal failure. See [OpenAI Hooks, PermissionRequest](https://learn.chatgpt.com/docs/hooks#permissionrequest).

## Release gates

1. CI is green on the dedicated GitHub repository.
2. Branch protection requires the Python and web jobs.
3. A real BUSY Bar returns API major `25`, renders a structural transition without stale elements, and accepts its configured token.
4. Linear, Calendar, Slack, and Gmail each complete a live poll with `healthy` status using least-privilege credentials.
5. If device input is enabled, its adapter passes allow, deny, replay, stale-context, acknowledge, snooze, and open tests.
6. No private snapshot, token, email body, Slack content, or permission payload appears in Git or CI logs.

## Gate status, 2026-08-01

| Gate | Status | Evidence or remaining owner |
| --- | --- | --- |
| Local CI equivalent | Passed | Python suite, production invariants, frontend build, and demo |
| Emulator API 25 and rendering | Passed | Seven synthetic captures, accented glyph inspection, structural clear, and live HTTP 409 ownership test |
| Signed interaction | Passed locally | Exact-context allow, consumed-context rejection, and untrusted SSE refusal |
| Dedicated GitHub repository and branch protection | Pending external | Repository owner must create or select the remote and enable required checks |
| Live Linear, Calendar, Slack, and Gmail credentials | Pending external | Deployment owner must provision least-privilege secrets and capture healthy poll evidence |
| Physical BUSY Bar acceptance | Pending hardware | Device owner must run token, transition, animation, and button-adapter cases |
| Codex project hook approval | Pending operator UI | Open `/hooks` in Codex and approve the reviewed local definition once |
