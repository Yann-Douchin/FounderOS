import assert from "node:assert/strict";
import test from "node:test";

import { capabilityForCockpitSlot, parseBridgeContext } from "../src/domain.js";

test("parses the version 2 non-sensitive context and presence capabilities", () => {
  const context = parseBridgeContext({
    bridge_version: 2,
    context: {
      bridge_version: 2,
      event_id: "linear:issue:42",
      request_id: "",
      kind: "decision",
      capabilities: [
        "event.open",
        "event.snooze",
        "event.acknowledge",
        "presence.acquire",
        "unknown"
      ],
      presence: {
        state: "focus",
        busy: false,
        calendar_known: true,
        matter_output_enabled: false,
        active_lease_count: 1,
        next_expiry_at: "2026-08-02T15:00:00Z",
        allowed_states: ["focus", "manual_call", "recording"],
        min_ttl_seconds: 5,
        max_ttl_seconds: 28800
      }
    }
  });

  assert.equal(context.bridgeVersion, 2);
  assert.equal(context.kind, "decision");
  assert.deepEqual([...context.capabilities], [
    "open",
    "snooze",
    "acknowledge",
    "presence.acquire"
  ]);
  assert.equal(context.presence?.state, "focus");
  assert.equal(context.presence?.busy, false);
  assert.equal(context.presence?.matterOutputEnabled, false);
  assert.equal(context.presence?.maxTtlSeconds, 28800);
});

test("derives conservative capabilities for the legacy context", () => {
  const regular = parseBridgeContext({
    context: { event_id: "calendar:42", request_id: "", kind: "meeting" }
  });
  assert.deepEqual([...regular.capabilities], ["open", "snooze", "acknowledge"]);

  const permission = parseBridgeContext({
    context: { event_id: "permission:42", request_id: "abcdef123456", kind: "permission_request" }
  });
  assert.deepEqual([...permission.capabilities], ["allow", "deny"]);
});

test("rejects malformed presence data instead of guessing", () => {
  assert.throws(
    () =>
      parseBridgeContext({
        bridge_version: 2,
        context: {
          event_id: "",
          request_id: "",
          kind: "",
          capabilities: [],
          presence: {
            state: "secret_state",
            busy: false,
            calendar_known: true,
            matter_output_enabled: false,
            active_lease_count: 0,
            next_expiry_at: "",
            allowed_states: [],
            min_ttl_seconds: 5,
            max_ttl_seconds: 28800
          }
        }
      }),
    /presence state is invalid/
  );
});

test("cockpit slots expose permission decisions without duplicate keys", () => {
  const permissionCapabilities = new Set(["allow", "deny"] as const);
  assert.equal(capabilityForCockpitSlot("snooze", permissionCapabilities), "deny");
  assert.equal(capabilityForCockpitSlot("acknowledge", permissionCapabilities), "allow");

  const eventCapabilities = new Set(["snooze", "acknowledge"] as const);
  assert.equal(capabilityForCockpitSlot("snooze", eventCapabilities), "snooze");
  assert.equal(capabilityForCockpitSlot("acknowledge", eventCapabilities), "acknowledge");
});

test("normalizes prefixed permission capabilities and keeps legacy aliases", () => {
  const prefixed = parseBridgeContext({
    bridge_version: 2,
    context: {
      event_id: "permission:42",
      request_id: "abcdef123456",
      kind: "permission_request",
      capabilities: ["permission.allow", "permission.deny"]
    }
  });
  assert.deepEqual([...prefixed.capabilities], ["allow", "deny"]);

  const legacy = parseBridgeContext({
    bridge_version: 2,
    context: {
      event_id: "permission:43",
      request_id: "abcdef123457",
      kind: "permission_request",
      capabilities: ["allow", "deny"]
    }
  });
  assert.deepEqual([...legacy.capabilities], ["allow", "deny"]);
});
