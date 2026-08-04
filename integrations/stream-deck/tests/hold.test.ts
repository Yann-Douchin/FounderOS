import assert from "node:assert/strict";
import test from "node:test";

import { HoldGate } from "../src/hold.js";

test("a short press never commits a protected action", () => {
  let callback: (() => void) | undefined;
  let committed = false;
  const gate = new HoldGate(
    1200,
    ((next: () => void) => {
      callback = next;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout,
    (() => undefined) as typeof clearTimeout
  );
  gate.begin("key", () => {
    committed = true;
  });
  assert.equal(gate.release("key"), "cancelled");
  assert.equal(committed, false);
  assert.equal(typeof callback, "function");
});

test("a complete hold commits exactly once", () => {
  let callback: (() => void) | undefined;
  let commits = 0;
  const gate = new HoldGate(
    1200,
    ((next: () => void) => {
      callback = next;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout,
    (() => undefined) as typeof clearTimeout
  );
  assert.equal(gate.begin("key", () => commits++), true);
  assert.equal(gate.begin("key", () => commits++), false);
  callback?.();
  assert.equal(gate.release("key"), "committed");
  assert.equal(commits, 1);
});
