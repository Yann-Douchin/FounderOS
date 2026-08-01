# Security policy

FounderOS can influence Claude and Codex permission decisions. Treat its input path, state directory, configuration, and host account as a security boundary.

## Supported version

Security fixes are applied to the latest commit on the default branch. Releases intended for production must pass `python tools/production_check.py`, the Python test suite, and the emulator build.

## Reporting

Use a private GitHub security advisory when the dedicated repository is published. Do not put credentials, connector payloads, permission summaries, or private event data in a public issue.

## Trust model

- Emulator SSE input is untrusted. It cannot approve, deny, acknowledge, snooze, or open an event.
- Mutating input uses a loopback-only HMAC bridge. Every message is bound to the exact selected event and permission request, expires after a short clock window, and carries a one-use nonce.
- A signed message is actionable only while that exact event is confirmed as the current screen owner. Draw errors and priority conflicts invalidate the context before input can be accepted.
- The authenticated operating-system user remains trusted. A process already running as that user can read the configured secret or modify local state.
- Claude and Codex hooks return no decision when FounderOS is unavailable or times out. Their normal approval flow then continues.
- Connector and hook state defaults to the platform application-state directory, outside the checkout and iCloud project folder. Directories use mode `0700` and files use mode `0600` where the platform supports POSIX permissions.
- Connector credentials are read from environment variables. Google refresh credentials and access tokens are kept in memory and are never persisted by FounderOS.
- A non-loopback BUSY Bar endpoint requires `X-API-Token` in production. FounderOS also sends `X-API-Sem-Ver: 25.0.0`.
- HTTP clients reject embedded credentials, disable redirects, cap responses, and allow plain HTTP only on loopback. Queued open actions accept HTTPS links or loopback HTTP links only.

## Deployment rules

1. Generate a dedicated `FOUNDEROS_INPUT_SECRET` with `python apps/founderos_input.py --generate-secret`.
2. Restrict the FounderOS process and its state directory to one operating-system account.
3. Use `founderos.production.example.json` as the deployment baseline.
4. Keep `llm.enabled` false unless an explicit data-processing review approves the fallback.
5. Never bridge the emulator `/events` stream into the trusted HMAC endpoint.
6. Rotate connector and input secrets after suspected host compromise.
