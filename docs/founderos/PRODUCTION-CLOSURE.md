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
| FOS-SEC-006 | Durable credentials could leak through JSON, process arguments, plists, clipboard, or ambient environment overrides | Native Keychain storage uses Security.framework directly, account names are allowlisted, persistent mode never falls back to ambient secrets, clipboard import clears after use, and service plists contain no credential values | `test_autonomous_service`, `production_check.py` | Security | Closed |
| FOS-PRIV-001 | Private state lived inside an iCloud checkout | State defaults outside the checkout. Production rejects a state root inside source, requires every private path below that root, and applies private POSIX modes where supported | `test_production_security`, `production_check.py` | Operations | Closed |
| FOS-PRIV-002 | The supervised emulator could persist uploaded state inside the checkout | The emulator defaults to the platform state directory, and the LaunchAgent pins a private `BUSY_DATA_DIR` below the FounderOS state root | `test_autonomous_service`, `production_check.py` | Operations | Closed |
| FOS-PRIV-003 | Character validation traversed ignored private analysis data and unrelated build trees | The integrity gate enumerates only tracked and non-ignored sources, then excludes binary asset and dependency trees before touching the filesystem | `production_check.py` | Security | Closed |
| FOS-DATA-001 | Stale data looked like an empty source | Typed failures preserve last-known events and publish a visible source-health event | `test_connectors`, `test_scheduler_health` | Integrations | Closed |
| FOS-DATA-002 | Snapshots lacked a governed publication path | The snapshot CLI validates and writes atomically outside Git | `apps/founderos_snapshot.py` | Integrations | Closed |
| FOS-DATA-003 | Founder-only Linear scope produced a false calm while delegated critical work remained open | Production uses an allowlisted portfolio scope with bounded pagination, relevance filters, owner metadata, and project-risk rollups | `test_connector_polling` | Product | Closed |
| FOS-DATA-004 | Every unread email was treated as an action | Gmail deterministically separates requests, important decisions, artifacts, and FYI billing or automated mail | `test_connectors` | Product | Closed |
| FOS-DATA-005 | Gmail VIP substring matches and negated requests created false actions | VIPs require an exact address or domain, and explicit non-action language overrides embedded action keywords | `test_connectors` | Product | Closed |
| FOS-DATA-006 | Linear treated `Unblocked` as blocked | Blocker detection uses exact multilingual tokens and rejects explicit unblocked states | `test_connectors` | Product | Closed |
| FOS-CLOSE-001 | Source priority could not represent who owed what to whom | A private SQLite commitment ledger persists owner, counterparty, next actor, due date, source observations, state, and append-only transitions | `test_closure_engine`, `test_operator_cli` | Product | Closed |
| FOS-CLOSE-002 | Technically ready work could hide missing access, deployment, proof, or validation | Profile-specific operational gates prevent final close until required state is satisfied or explicitly waived | `test_closure_engine` | Product | Closed |
| FOS-CLOSE-003 | A later blocker could remain hidden behind stale satisfied evidence | Newer source gate state wins, and changed observations retract their previous evidence before quorum evaluation | `test_closure_engine` | Product | Closed |
| FOS-CLOSE-004 | Deadline concentration and absence had no handoff control | Capacity governance detects same-day owner concentration and explicit unavailability, requires a delegate, and records a satisfied handoff when one exists | `test_closure_engine`, `test_operator_cli` | Product | Closed |
| FOS-CLOSE-005 | Linear and Slack bursts could monopolize ranking | Outcome correlation compacts bursts, caps source priority, and adds only bounded closure context | `test_closure_engine` | Ranking | Closed |
| FOS-CLOSE-006 | Meetings disappeared without a durable next move | Stable meeting identity governs before and after phases, routine meetings are excluded, and the operator can record a concrete next action and holder | `test_closure_engine`, `test_operator_cli` | Product | Closed |
| FOS-CLOSE-007 | Customer follow-ups lacked stage and cooling memory | Relationship records retain stage, last interaction, next decision, resume boundary, cooling boundary, and open obligations | `test_closure_engine` | Product | Closed |
| FOS-CLOSE-008 | One generic proof could masquerade as multi-market acceptance | Evidence quorum supports required categories plus per-category scopes for markets, languages, pricing, analytics, and devices | `test_closure_engine`, `test_closure_connectors` | Product | Closed |
| FOS-CLOSE-009 | Customer feedback could remain unowned and unlinked | Feedback profiles surface only missing ownership or decision gates and correlate through explicit project, customer, relationship, and entity aliases | `test_closure_engine`, `test_closure_connectors` | Product | Closed |
| FOS-CLOSE-010 | Poll timestamps could reopen manually closed work | Semantic fingerprints exclude volatile observation and lease timestamps while retaining all meaningful source fields | `test_closure_engine` | Runtime | Closed |
| FOS-CLOSE-011 | The obligation state had no review surface | A localhost-only emulator endpoint and Obligations tab expose the private atomic snapshot, while all corrections remain audited CLI operations | `test_emulator_contract`, frontend build, `test_operator_cli` | Operations | Closed |
| FOS-CONN-001 | Google tokens expired without refresh | Gmail and Calendar refresh OAuth credentials in memory, cache them, and invalidate on `401` | `test_google_oauth` | Integrations | Closed |
| FOS-CONN-002 | HTTP work had weak bounds and could leak authorization across redirects | Connector, display, and LLM clients disable redirects, validate schemes, cap responses and errors, and apply finite timeouts and retries | `test_http_client`, `test_display`, `test_llm_fallback` | Integrations | Closed |
| FOS-CONN-003 | Linear authentication conflated personal keys and OAuth tokens | `api_key` sends Linear's required raw header, while explicit `bearer` mode handles OAuth | `test_connector_polling` | Integrations | Closed |
| FOS-CONN-004 | Calendar could silently omit later pages, and Google detail work lacked a total deadline | Calendar follows bounded page tokens, while Gmail and Calendar share strict per-poll deadlines across detail and refresh work | `test_connector_polling` | Integrations | Closed |
| FOS-CONN-005 | Remote HTTP error bodies could leak private data into source-health logs or pixels | Connector and display errors retain only the method, sanitized endpoint path, and status code | `test_http_client` | Security | Closed |
| FOS-CONN-006 | Linear OAuth refresh-token rotation could invalidate the unattended service | PKCE authorization stores the durable refresh token, and every rotated token is committed to the Keychain before its paired access token is returned | `test_autonomous_service` | Integrations | Closed |
| FOS-CONN-007 | Provider-defined Slack and Linear error strings could repeat private remote text in local logs | Slack accepts only bounded machine error codes, and Linear records bounded GraphQL extension codes rather than provider messages | `test_connector_polling` | Security | Closed |
| FOS-CONN-008 | OAuth refresh and HTTP retry work could exceed a connector’s total poll budget | Gmail, Calendar, and Linear propagate one monotonic deadline through token refresh, API attempts, retry delays, and one-time `401` recovery | `test_http_client`, `test_connector_polling`, `test_google_oauth` | Runtime | Closed |
| FOS-CONN-009 | A partial OAuth grant or verbose denial could be accepted or logged | Google and Linear validate returned scopes before storage, and callback failures retain only a bounded machine error code | `test_autonomous_service` | Security | Closed |
| FOS-CONN-010 | Slack commitments inside reply threads were invisible | The connector reads a bounded number of allowlisted conversation threads under the same total poll deadline and marks configured founder user IDs as outgoing | `test_connector_polling` | Integrations | Closed |
| FOS-CONN-011 | Outgoing email promises were absent from the live loop | Gmail supports separate incoming and outgoing query contracts and retains only deterministic promise matches from sent mail | `test_connector_polling`, `test_closure_engine` | Integrations | Closed |
| FOS-CONN-012 | Generic CI success could falsely satisfy deployment readiness | GitHub counts only configured deployment workflow names as deployment proof; other successful workflows provide code evidence only | `test_closure_connectors` | Integrations | Closed |
| FOS-CONN-013 | Optional high-value sources had schema placeholders but no adapters | Notion, Drive, Sheets, GitHub, deployment, Sentry, PostHog, Shopify, Superhuman reminder, Stripe, and Home Assistant have bounded read-only adapters and remain disabled until configured | `test_closure_connectors`, `test_config_production` | Integrations | Closed |
| FOS-CONN-014 | Google authorization could over-request or under-request Workspace scopes | OAuth derives the smallest supported read-only set from enabled connectors and validates the complete returned grant before storage | `test_autonomous_service` | Security | Closed |
| FOS-AUTO-001 | Calendar occupancy could require a direct, vendor-specific Hue integration | FounderOS publishes one generic state through the BUSY Bar Matter switch. Apple Home, Google Home, or Home Assistant owns the Hue mapping | `test_calendar_busy_automation`, `test_cli`, `production_check.py` | Integrations | Closed |
| FOS-AUTO-002 | Starting a device BUSY session for Calendar occupancy could block the decision display | The output uses only `/api/smart_home/switch`, never a BUSY timer, and the production invariant rejects a timer path in the automation | `test_display`, `test_calendar_busy_automation`, `production_check.py` | Display | Closed |
| FOS-AUTO-003 | Cancelled, declined, free, stale, or all-day Calendar data could advertise the wrong presence state | Deterministic event semantics exclude cancelled, declined, and transparent events, exclude all-day events by default, hold the previous state when Calendar is stale, confirm writes, delay off transitions, and retry with bounded backoff | `test_calendar_busy_automation`, `test_connectors`, `test_runtime`, `test_autonomous_service` | Runtime | Closed |
| FOS-RUN-001 | Serial polling froze the runtime | Connector workers are concurrent. Normal ticks never wait, and overdue polls create incidents | `test_scheduler_health` | Runtime | Closed |
| FOS-RUN-002 | Corrupt or unbounded ranking memory could crash or grow forever | Memory tolerates malformed sections, timestamps acknowledgements, resurfaces updated events, prunes by age and count, and fsyncs atomic writes | `test_memory` | Runtime | Closed |
| FOS-RANK-001 | Permission priority was only a bonus | Permissions form a strict selection partition and never invoke the LLM tie breaker | `test_ranking` | Ranking | Closed |
| FOS-RANK-002 | Display hold delayed permissions | Permission requests bypass the hold | `test_runtime` | Ranking | Closed |
| FOS-DISP-001 | Hardware merge left stale elements | Clear the application before structural transitions | `test_runtime` | Display | Closed |
| FOS-DISP-002 | Display calls omitted auth and version | Send the token when configured and API version `25.0.0` | `test_display` | Display | Closed |
| FOS-DISP-003 | Animated icons resent the entire frame every second and restarted scrolling text | Stable element IDs and same-application merge now send only changed icon pixels. Refresh probes exclude scrolling elements, while bounded leases still remove abandoned frames | `test_runtime`, `test_emulator_contract` | Display | Closed |
| FOS-DISP-004 | Firmware timer, hidden physical-session, menu, and smart-home blockers caused unbounded retry noise or false visibility | Emulator scenarios reproduce all four 409 conditions. Runtime applies bounded exponential retry, revokes input visibility, and forces a complete recovery draw | `test_runtime_visibility`, `test_emulator_contract` | Display | Closed |
| FOS-DISP-005 | Emulator behavior could not be compared with the actual raw framebuffer | `/api/screen` emits the observed BGR888 front and gray8 back buffers, while the client validates exact sizes and converts front data to RGB | `test_display`, `test_emulator_contract` | Display | Closed |
| FOS-TIME-001 | Linear dates expired around UTC midnight | Resolve date-only deadlines at local end of day | `test_connectors` | Data model | Closed |
| FOS-TIME-002 | All-day Calendar events disappeared | Normalize date-only events with exclusive local end dates | `test_connectors` | Data model | Closed |
| FOS-UX-001 | Normal events had no actions | Trusted input can acknowledge, snooze, and queue open actions | `test_runtime` | Product | Closed |
| FOS-I18N-001 | Accents were stripped or rendered as replacement glyphs | Normalize all event text to NFC, preserve labels, use the global scrolling font for non-ASCII titles, and verify the complete French glyph set | `test_models`, `test_display`, `production_check.py` | Display | Closed |
| FOS-I18N-002 | Uppercase accents existed in the atlas but were clipped above its visible line | Fold only top overshoot pixels into the first scanline while preserving the baseline, then reject every negative French glyph offset | `test_display`, `test_capture`, `production_check.py` | Display | Closed |
| FOS-I18N-003 | FounderOS mixed French system labels into an English interface | System fallbacks, health incidents, permission controls, readiness labels, and demo copy are English, while multilingual content recognition and Unicode rendering remain intact | `production_check.py`, connector tests | Product | Closed |
| FOS-I18N-004 | Atlas tests could pass while a physical device still rendered French glyphs incorrectly | `founderosctl display verify-accents` compares native screen readback pixel by pixel. A double-buffered PNG mode preserves Unicode when a firmware fails the native check | `test_display`, physical release gate | Display | Closed |
| FOS-CONFIG-001 | The CLI silently replaced a configured display host with port 8080 | `--host` is now an explicit override only. Omitting it preserves the configuration file | `test_cli` | Runtime | Closed |
| FOS-OPS-001 | No delivery gate existed | CI tests Python, builds the frontend, checks invariants, and runs the demo | `.github/workflows/ci.yml` | Maintainers | Closed |
| FOS-OPS-002 | Gallery claims referenced missing files | A dependency-free SSE exporter produces seven synthetic 720×160 captures, and production checks verify their signatures and dimensions | `test_capture`, `production_check.py` | Maintainers | Closed |
| FOS-OPS-003 | A one-shot run could exit successfully while real connectors were degraded | `--require-healthy` turns source health into an executable preflight, and production one-shot runs fail on unhealthy critical sources | `test_cli` | Operations | Closed |
| FOS-OPS-004 | Polling stopped when the terminal closed or the Mac restarted | Private user LaunchAgents supervise the loopback emulator and FounderOS, restart unsuccessful exits, and expose a content-free mode `0600` heartbeat whose PID must match launchd before installation succeeds | `test_autonomous_service`, `production_check.py` | Operations | Closed |
| FOS-OPS-005 | The emulator silently listened on every network interface | The default server and its LaunchAgent bind explicitly to `127.0.0.1`; hardware deployment requires an explicit separate host and token policy | `test_autonomous_service`, `production_check.py` | Security | Closed |
| FOS-OPS-006 | Launchd could stall while reading a checkout inside a macOS protected folder, and a failed service replacement could leave no working definition | A shell bootstrap archives committed runtime sources into a private content-addressed deployment outside the checkout. LaunchAgent replacement retries transient bootstrap failures, verifies readiness, and transactionally restores both prior definitions and loaded states on failure | `test_autonomous_service`, `production_check.py` | Operations | Closed |
| FOS-OPS-007 | Community-tool observations were informal and could drift without review | BarPilot is pinned by commit and file hash. All 53 paths, all 69 HTTP operations, the status WebSocket, and the firmware quirks are executable through `tools/barpilot_compat.py` and protected by contract tests | `test_emulator_contract`, `barpilot-api25-contract.json` | Maintainers | Closed |

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
3. A real BUSY Bar returns API major `25`, renders a structural transition without stale elements, accepts its configured token, exposes valid front and back screen buffers, and passes either native or raster French-glyph readback.
4. Linear, Calendar, Slack, and Gmail each complete a live poll with `healthy` status using least-privilege credentials.
5. If device input is enabled, its adapter passes allow, deny, replay, stale-context, acknowledge, snooze, and open tests.
6. No private snapshot, token, email body, Slack content, or permission payload appears in Git or CI logs.
7. If the Calendar busy indicator is enabled, the physical BUSY Bar is commissioned into a Matter fabric, its API token is present in the Keychain, both switch transitions are confirmed, and the controller maps only the intended Hue accessory.

## Gate status, 2026-08-02

| Gate | Status | Evidence or remaining owner |
| --- | --- | --- |
| Local CI equivalent | Passed | 225 Python and Node-backed tests, production invariants, frontend build, and deterministic demo |
| Emulator API 25 and rendering | Passed | Pinned BarPilot source hash, WebSocket status, BGR and gray screen buffers, same-app merge, cross-app priority arbitration, all governed blockers, and native plus raster accent readback |
| Signed interaction | Passed locally | Exact-context allow, consumed-context rejection, and untrusted SSE refusal |
| Dedicated GitHub repository and branch protection | Passed | Public repository [Yann-Douchin/FounderOS](https://github.com/Yann-Douchin/FounderOS); protected `main` requires `web`, `python (3.11)`, and `python (3.13)` with strict, linear, pull-request-only changes |
| Live Linear | Passed | Read-only OAuth refresh completed from the macOS Keychain; portfolio poll reported `healthy` with 9 current events |
| Live Slack | Passed | Private app installed with `channels:history` and `groups:history` only; six allowlisted conversations passed read-only checks and the connector reported `healthy` |
| Live Calendar and Gmail | Passed | Offline read-only Google OAuth credentials are stored in the macOS Keychain; Gmail reported `healthy` with 2 current events and Calendar reported `healthy` with no current event |
| Autonomous polling supervisor | Passed | Both LaunchAgents are active from a private content-addressed deployment; the fresh heartbeat PID matches launchd, the emulator API reports `25.0.0`, the display is healthy, and all four live connectors report `healthy` |
| Calendar to Matter indicator | Controlled | The deterministic output, authenticated API 25 contract, health gate, stale-state policy, confirmation, and retries are implemented. Physical commissioning, the BUSY Bar LAN endpoint, and the Apple Home Hue mapping remain deployment-specific evidence |
| Physical BUSY Bar acceptance | Pending hardware | Run the documented BarPilot mutation window and both `founderosctl display` checks, then complete token, transition, animation, and button-adapter cases |
| Codex project hook approval | Pending operator UI | Open `/hooks` in Codex and approve the reviewed local definition once |
