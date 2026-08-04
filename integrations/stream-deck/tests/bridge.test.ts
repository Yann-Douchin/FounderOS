import assert from "node:assert/strict";
import test from "node:test";

import {
  LoopbackBridgeClient,
  canonicalJson,
  signatureFor,
  validateLoopbackUrl
} from "../src/bridge.js";
import type { SecretProvider } from "../src/bridge.js";

const SECRET = "abcdefghijklmnopqrstuvwxyzABCDEFGH123456";

class StaticSecretProvider implements SecretProvider {
  invalidated = false;

  async getSecret(): Promise<string> {
    return SECRET;
  }

  invalidate(): void {
    this.invalidated = true;
  }
}

test("sends a request-bound open action with an exact HMAC", async () => {
  const bodies: Record<string, unknown>[] = [];
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    const headers = new Headers(init?.headers);
    if (url.endsWith("/context")) {
      assert.equal(headers.get("Authorization"), `Bearer ${SECRET}`);
      return jsonResponse({
        bridge_version: 2,
        context: {
          bridge_version: 2,
          event_id: "linear:42",
          request_id: "",
          kind: "decision",
          capabilities: ["event.open"],
          presence: null
        }
      });
    }
    const body = String(init?.body ?? "");
    assert.equal(headers.get("X-FounderOS-Signature"), signatureFor(SECRET, body));
    bodies.push(JSON.parse(body) as Record<string, unknown>);
    return jsonResponse({ result: "OK", action: "open" });
  };
  const client = new LoopbackBridgeClient(
    new StaticSecretProvider(),
    "http://127.0.0.1:8765",
    2500,
    mockFetch
  );
  const result = await client.sendEventAction("open");
  assert.equal(result.action, "open");
  assert.equal(bodies[0]?.key, "custom");
  assert.equal(bodies[0]?.event_id, "linear:42");
  assert.match(String(bodies[0]?.nonce), /^[A-Za-z0-9_-]{16,128}$/);
});

test("sends an explicit presence preset and never a toggle", async () => {
  let presenceBody: Record<string, unknown> | undefined;
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/context")) {
      return jsonResponse(versionTwoContext(["presence.acquire"]));
    }
    const body = String(init?.body ?? "");
    const headers = new Headers(init?.headers);
    assert.ok(url.endsWith("/presence/lease"));
    assert.equal(headers.get("X-FounderOS-Signature"), signatureFor(SECRET, body));
    presenceBody = JSON.parse(body) as Record<string, unknown>;
    return jsonResponse({ result: "OK", presence: { action: "acquire" } });
  };
  const client = new LoopbackBridgeClient(
    new StaticSecretProvider(),
    "http://127.0.0.1:8765",
    2500,
    mockFetch
  );
  await client.sendPresencePreset("focus50");
  assert.equal(presenceBody?.action, "acquire");
  assert.equal(presenceBody?.lease_id, "streamdeck.focus");
  assert.equal(presenceBody?.state, "focus");
  assert.equal(presenceBody?.ttl_seconds, 3000);
  assert.equal(Object.values(presenceBody ?? {}).includes("toggle"), false);
});

test("maps a prefixed permission capability to the existing signed input key", async () => {
  let sentBody: Record<string, unknown> | undefined;
  const mockFetch: typeof fetch = async (input, init) => {
    if (String(input).endsWith("/context")) {
      return jsonResponse({
        bridge_version: 2,
        context: {
          bridge_version: 2,
          event_id: "permission:42",
          request_id: "abcdef123456",
          kind: "permission_request",
          capabilities: ["permission.deny"]
        }
      });
    }
    sentBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    return jsonResponse({ result: "OK", action: "deny" });
  };
  const client = new LoopbackBridgeClient(
    new StaticSecretProvider(),
    "http://127.0.0.1:8765",
    2500,
    mockFetch
  );
  await client.sendEventAction("deny");
  assert.equal(sentBody?.key, "back");
  assert.equal(sentBody?.event_id, "permission:42");
  assert.equal(sentBody?.request_id, "abcdef123456");
});

test("release_all contains no lease, state or TTL field", async () => {
  let keys: string[] = [];
  const mockFetch: typeof fetch = async (input, init) => {
    if (String(input).endsWith("/context")) {
      return jsonResponse(versionTwoContext(["presence.release_all"]));
    }
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    keys = Object.keys(body).sort();
    return jsonResponse({ result: "OK", presence: { action: "release_all" } });
  };
  const client = new LoopbackBridgeClient(
    new StaticSecretProvider(),
    "http://127.0.0.1:8765",
    2500,
    mockFetch
  );
  await client.sendPresencePreset("releaseManual");
  assert.deepEqual(keys, ["action", "issued_at", "nonce"]);
});

test("canonical JSON matches the sorted compact FounderOS contract", () => {
  assert.equal(
    canonicalJson({ z: 2, nested: { y: true, a: "é" }, a: 1 }),
    '{"a":1,"nested":{"a":"é","y":true},"z":2}'
  );
});

test("rejects non-loopback and credential-bearing bridge URLs", () => {
  assert.throws(() => validateLoopbackUrl("https://example.com:8765"), /loopback/);
  assert.throws(() => validateLoopbackUrl("http://user:secret@127.0.0.1:8765"), /loopback/);
  assert.equal(validateLoopbackUrl("http://127.0.0.1:8765"), "http://127.0.0.1:8765");
});

function versionTwoContext(capabilities: string[]): Record<string, unknown> {
  return {
    bridge_version: 2,
    context: {
      bridge_version: 2,
      event_id: "",
      request_id: "",
      kind: "",
      capabilities,
      presence: {
        state: "available",
        busy: false,
        calendar_known: true,
        matter_output_enabled: false,
        active_lease_count: 0,
        next_expiry_at: "",
        allowed_states: ["focus", "manual_call", "recording"],
        min_ttl_seconds: 5,
        max_ttl_seconds: 28800
      }
    }
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  });
}
