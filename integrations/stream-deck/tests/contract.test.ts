import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { ACTION_UUIDS, PLUGIN_UUID, PRESENCE_PRESETS } from "../src/domain.js";

test("manifest and machine-readable profile contract use the same stable UUIDs", async () => {
  const manifest = JSON.parse(
    await readFile("com.yanndouchin.founderos-actions.sdPlugin/manifest.json", "utf8")
  ) as { UUID: string; Actions: Array<{ UUID: string }> };
  const contract = JSON.parse(await readFile("action-contract.json", "utf8")) as {
    plugin: { uuid: string };
    bridge: { version: number; capabilityMap: Record<string, string> };
    actions: Record<string, { uuid: string; presets?: Record<string, unknown> }>;
  };
  assert.equal(manifest.UUID, PLUGIN_UUID);
  assert.equal(contract.plugin.uuid, PLUGIN_UUID);
  assert.equal(contract.bridge.version, 2);
  assert.deepEqual(contract.bridge.capabilityMap, {
    "event.open": "open",
    "event.snooze": "snooze",
    "event.acknowledge": "acknowledge",
    "permission.allow": "allow",
    "permission.deny": "deny",
    "presence.acquire": "presence.acquire",
    "presence.renew": "presence.renew",
    "presence.release": "presence.release",
    "presence.release_all": "presence.release_all"
  });
  assert.deepEqual(
    new Set(manifest.Actions.map((item) => item.UUID)),
    new Set(Object.values(ACTION_UUIDS))
  );
  for (const [name, uuid] of Object.entries(ACTION_UUIDS)) {
    assert.equal(contract.actions[name]?.uuid, uuid);
  }
  assert.deepEqual(
    new Set(Object.keys(contract.actions.presence?.presets ?? {})),
    new Set(Object.keys(PRESENCE_PRESETS))
  );
});
