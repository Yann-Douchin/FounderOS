# FounderOS architecture

FounderOS is not a collection of display apps. It is one decision engine with many event sources and exactly one display output.

## Data flow

```text
Linear  Slack  Gmail  Calendar  LinkedIn  Claude  Codex
   \      |      |       |         |        |      /
              normalized Event contract
                         |
                      EventBus
                         |
            deterministic ranking + memory
                         |
             optional close-tie LLM fallback
                         |
               one selected RankedEvent
                         |
          semantic icon classifier + 72x16 renderer
                         |
                POST /api/display/draw
```

Connectors import no display code. The display layer imports no connector code. The runtime is the only composition root.

Authorized Codex app connectors can also feed the same contract through private, expiring JSON snapshots. This bridge keeps OAuth credentials out of the FounderOS process and lets the project move from gallery fixtures to real data one source at a time. Direct API polling remains the autonomous deployment path.

Claude and Codex permission hooks share a second, narrowly scoped local protocol:

```text
agent PermissionRequest
          |
  redacted request file
          |
 deterministic priority event
          |
 signed, request-bound input
          |
 one-use decision file
          |
 exact agent hook response
```

Requests expire after 45 seconds. A timeout returns no decision, which preserves the agent's normal approval path. The bridge never persists an allow rule and never sends permission details to a model. ChatGPT/Codex usage comes from the official local app-server `account/rateLimits/read` method. Claude usage enters through an expiring snapshot because no equivalent personal quota API is documented.

## Normalized event

The minimum valid event is:

```json
{
  "source": "linear",
  "priority": 90,
  "title": "Quantity Fix blocked",
  "action_required": true
}
```

The complete model also supports stable IDs, deduplication keys, kind, urgency, impact, timestamps, due and expiry times, confidence, URL, body, and connector metadata. Unknown feed fields are retained inside `metadata`.

Source normalization is deliberately opinionated. Linear portfolio mode retains founder-owned work and allowlisted team risks, then collapses multiple project issues into one outcome event. Slack labels dependency and decision signals. Gmail separates actions from FYI mail and received artifacts. Calendar emits a deterministic readiness state for important meetings. These rules reduce source volume before global ranking and remain fully inspectable.

## Deterministic ranking

Every candidate receives an inspectable score:

```text
base priority
+ source weight
+ action-required bonus
+ urgency weight
+ impact weight
+ event-kind weight
+ due-time boost
- age decay
- confidence adjustment
+ memory adjustment
```

The winner is sorted by score, action requirement, recency, then stable event ID. This makes identical inputs produce identical output.

Memory provides three controls:

- `acknowledge` removes a deduplication key from future consideration;
- `snooze` suppresses it until a timestamp;
- recent-display memory penalizes repeated interruptions while a small stickiness bonus prevents flicker.

Memory is persisted below the platform FounderOS state directory, outside the checkout, and contains no credentials. A newer occurrence with the same deduplication key resurfaces after acknowledgement. Corrupt sections are ignored safely, while age and entry-count limits bound growth.

## LLM boundary

`NoLLMFallback` is the default. No model is loaded or called during connector polling, normalization, scoring, selection, layout, or display refresh.

The optional OpenAI fallback is eligible only when all of these conditions are true:

1. `llm.enabled` is `true`.
2. The configured API key exists.
3. At least two deterministic scores are within `ranking.tie_threshold`.
4. The hourly fallback budget is not exhausted.

The request contains only compact candidate fields, uses a strict JSON schema, sets `store` to `false`, and may return only one of the supplied event IDs. Any timeout, malformed response, provider error, or unexpected ID falls back to deterministic order.

## Display contract

FounderOS calls the real device endpoint directly:

```text
POST /api/display/draw
DELETE /api/display/draw?application_name=founderos
```

Every draw contains `application_name`, `priority`, and `elements`, plus API version and optional token headers. HTTP 409 is handled as a higher-priority owner. Structural layout changes clear the FounderOS application before drawing, which prevents stale merged elements on hardware.

The renderer also chooses one of nine animated 8 by 8 icons from event semantics. Urgency and content keywords take precedence, while the connector source is only a fallback. Each frame redraws the same 64 one-pixel rectangles with stable IDs, using the background color for inactive pixels. This matches the physical firmware's element-merging behavior, stays below the 100-element request limit, and requires no emulator-specific asset or LLM call.

Permission and agent-usage events use dedicated layouts. A permission request pulses its accent, shows a countdown, and reserves red and green answer rails. A usage event renders up to two deterministic quota bars. These layouts use only standard text and rectangle elements.

Emulator SSE input is explicitly untrusted. The production adapter is a loopback HMAC endpoint that binds every key to the exact selected event and request, checks freshness, and rejects nonce replay. A physical transport must implement this contract because the display HTTP API has no documented outbound button stream.

Trusted context is published only after the display confirms that exact event. It is removed before redraws, conflicts, and actions, so an invisible event cannot be acknowledged or approved.

## Package map

```text
founder_os/
  connectors/   read-only polling and normalization
  core/         event bus, scheduler, priority engine, runtime
  ranking/      deterministic scorer, persistent memory, optional LLM
  display/      BUSY Bar HTTP client, layouts, frame sequences
apps/
  founderos.py  emulator and hardware entry point
tests/          contracts, ranking, connector fixtures, runtime
```
