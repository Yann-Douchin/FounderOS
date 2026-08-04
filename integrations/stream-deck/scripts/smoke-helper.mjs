import { DeployedHelperBridgeClient } from "../.test-build/src/helper-client.js";

const client = new DeployedHelperBridgeClient();
const CALL_TIMEOUT_MS = 7000;
const initial = await bounded(client.getContext(), "initial read");
if (!initial.presence || initial.presence.activeLeaseCount !== 0) {
  throw new Error("The smoke test requires zero initial presence leases.");
}

let focusCount = -1;
try {
  await bounded(client.sendPresencePreset("focus50"), "Focus lease creation");
  const focused = await bounded(client.getContext(), "Focus lease verification");
  focusCount = focused.presence?.activeLeaseCount ?? -1;
  if (focusCount !== 1) {
    throw new Error("The FounderOS Focus lease was not created.");
  }
} finally {
  await bounded(client.sendPresencePreset("releaseManual"), "Stream Deck lease release");
}

const finalContext = await bounded(client.getContext(), "final verification");
const finalCount = finalContext.presence?.activeLeaseCount ?? -1;
if (finalCount !== 0) {
  throw new Error("The FounderOS Focus lease was not released.");
}

console.log(JSON.stringify({
  bridgeVersion: initial.bridgeVersion,
  initialActiveLeaseCount: initial.presence.activeLeaseCount,
  focusActiveLeaseCount: focusCount,
  finalActiveLeaseCount: finalCount
}));

async function bounded(promise, label) {
  let timeout;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeout = setTimeout(
          () => reject(new Error(`${CALL_TIMEOUT_MS} ms timeout exceeded during ${label}.`)),
          CALL_TIMEOUT_MS
        );
      })
    ]);
  } finally {
    clearTimeout(timeout);
  }
}
