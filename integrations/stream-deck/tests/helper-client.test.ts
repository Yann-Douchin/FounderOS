import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import {
  DEPLOYMENTS_ROOT,
  FALLBACK_CONTEXT_INITIAL_BACKOFF_MILLISECONDS,
  LOCAL_SOCKET_PATH,
  MAX_LOCAL_REQUEST_BYTES,
  MAX_LOCAL_RESPONSE_BYTES,
  RUNTIME_PLIST,
  USER_HOME,
  DeployedHelperBridgeClient,
  DeployedHelperLocator,
  LocalProtocolError,
  LocalSocketAbsentError,
  LocalSocketBridgeClient,
  LocalSocketSecurityError,
  decodeLocalResponse,
  encodeLocalRequest,
  requestLocalSocket,
  verifyLocalSocketPath,
  type HelperDeployment,
  type HelperExecutor,
  type LocalBridgeRequest,
  type LocalPathFacts,
  type LocalSocketConnector,
  type PathVerifier
} from "../src/helper-client.js";
import { BridgeError } from "../src/protocol.js";

const SHA = "01b9e14306fe59ba7e8835aeda4b99a9a09a772c5f2a7de357e342c964da15d0";
const ROOT = `${DEPLOYMENTS_ROOT}/${SHA}`;
const PYTHON = "/opt/homebrew/Cellar/python@3.13/3.13.3/Frameworks/Python.framework/Versions/3.13/bin/python3.13";
const CONFIG = `${ROOT}/founderos.runtime.json`;
const HELPER = `${ROOT}/apps/founderos_input.py`;
const COMMON_ARGUMENTS = ["--config", CONFIG, "--url", "http://127.0.0.1:8765"];
const NOW = 1_775_000_000;
const NONCE = "local-test-nonce-000000000001";
const ABSENT_LOCAL_BRIDGE = new LocalSocketBridgeClient(
  LOCAL_SOCKET_PATH,
  async () => false,
  async () => assert.fail("an absent socket must not be opened")
);

test("locates only the helper belonging to the exact immutable runtime deployment", async () => {
  const verified: Array<[string, string]> = [];
  const locator = validLocator(async (filePath, kind) => {
    verified.push([filePath, kind]);
  });

  assert.deepEqual(await locator.locate(), {
    python: PYTHON,
    root: ROOT,
    helper: HELPER,
    config: CONFIG
  });
  assert.deepEqual(verified, [
    [RUNTIME_PLIST, "file"],
    [PYTHON, "executable"],
    [ROOT, "directory"],
    [`${ROOT}/apps/founderos.py`, "file"],
    [CONFIG, "file"],
    [HELPER, "file"]
  ]);
});

test("rejects extra runtime arguments", async () => {
  const plist = validPlist();
  (plist.ProgramArguments as string[]).push("--unexpected");
  const locator = locatorFor(plist);

  await rejectsAsNotConfigured(locator.locate());
});

test("rejects a runtime outside an exact lowercase SHA deployment", async () => {
  const invalidRoot = `${DEPLOYMENTS_ROOT}/current`;
  const locator = locatorFor({
    Label: "com.founderos.runtime",
    ProgramArguments: [
      PYTHON,
      `${invalidRoot}/apps/founderos.py`,
      "--config",
      `${invalidRoot}/founderos.runtime.json`
    ],
    WorkingDirectory: invalidRoot
  });

  await rejectsAsNotConfigured(locator.locate());
});

test("rejects a config path that does not share the runtime deployment", async () => {
  const plist = validPlist();
  (plist.ProgramArguments as string[])[3] = "/private/tmp/founderos.runtime.json";
  const locator = locatorFor(plist);

  await rejectsAsNotConfigured(locator.locate());
});

test("rejects a runtime interpreter whose executable name is not Python", async () => {
  const plist = validPlist();
  (plist.ProgramArguments as string[])[0] = "/bin/bash";

  await rejectsAsNotConfigured(locatorFor(plist).locate());
});

test("turns path verification and symlink failures into a generic configuration error", async () => {
  const locator = validLocator(async (filePath) => {
    if (filePath === HELPER) {
      throw new Error(`private symlink target ${path.join(USER_HOME, "Secret", "helper.py")}`);
    }
  });

  const error = await captureError(locator.locate());
  assert.ok(error instanceof BridgeError);
  assert.equal(error.code, "not_configured");
  assert.equal(error.message.includes("Secret"), false);
});

test("reads context through the private socket without locating or spawning the helper", async () => {
  const harness = localOnlyClient(async (request) => {
    assert.deepEqual(request, { version: 1, operation: "context" });
    return localContextResponse(["presence.acquire"]);
  });

  const context = await harness.client.getContext();
  assert.equal(context.bridgeVersion, 2);
  assert.equal(context.presence?.state, "available");
  assert.deepEqual(harness.counters, { locator: 0, helper: 0, socket: 1 });
});

test("sends an event through the private socket without locating or spawning the helper", async () => {
  const requests: LocalBridgeRequest[] = [];
  const harness = localOnlyClient(async (request) => {
    requests.push(request);
    return request.operation === "context"
      ? localContextResponse(["event.open"])
      : { result: "OK", action: "open" };
  });

  assert.equal((await harness.client.sendEventAction("open")).action, "open");
  assert.deepEqual(requests, [
    { version: 1, operation: "context" },
    {
      version: 1,
      operation: "input",
      payload: {
        key: "custom",
        event_id: "opaque:event",
        request_id: "opaque-request",
        issued_at: NOW,
        nonce: NONCE
      }
    }
  ]);
  assert.deepEqual(harness.counters, { locator: 0, helper: 0, socket: 2 });
});

test("sends presence through the private socket without locating or spawning the helper", async () => {
  const requests: LocalBridgeRequest[] = [];
  const harness = localOnlyClient(async (request) => {
    requests.push(request);
    return request.operation === "context"
      ? localContextResponse(["presence.acquire"])
      : { result: "OK", presence: { action: "acquire" } };
  });

  assert.equal((await harness.client.sendPresencePreset("focus50")).action, "acquire");
  assert.deepEqual(requests, [
    { version: 1, operation: "context" },
    {
      version: 1,
      operation: "presence",
      payload: {
        action: "acquire",
        issued_at: NOW,
        nonce: NONCE,
        lease_id: "streamdeck.focus",
        state: "focus",
        ttl_seconds: 3000
      }
    }
  ]);
  assert.deepEqual(harness.counters, { locator: 0, helper: 0, socket: 2 });
});

test("accepts only an account-owned 0700 parent and 0600 Unix socket", async () => {
  const uid = process.getuid?.();
  assert.notEqual(uid, undefined);
  const parentPath = path.dirname(LOCAL_SOCKET_PATH);
  const validReader = async (filePath: string): Promise<LocalPathFacts> => filePath === parentPath
    ? localFacts("directory", 0o700, uid as number)
    : localFacts("socket", 0o600, uid as number);
  assert.equal(await verifyLocalSocketPath(LOCAL_SOCKET_PATH, validReader), true);

  const invalidReaders: Array<(filePath: string) => Promise<LocalPathFacts>> = [
    async (filePath) => filePath === parentPath
      ? localFacts("directory", 0o755, uid as number)
      : localFacts("socket", 0o600, uid as number),
    async (filePath) => filePath === parentPath
      ? localFacts("symlink", 0o700, uid as number)
      : localFacts("socket", 0o600, uid as number),
    async (filePath) => filePath === parentPath
      ? localFacts("directory", 0o700, uid as number)
      : localFacts("file", 0o600, uid as number),
    async (filePath) => filePath === parentPath
      ? localFacts("directory", 0o700, uid as number)
      : localFacts("socket", 0o660, uid as number),
    async (filePath) => filePath === parentPath
      ? localFacts("directory", 0o1700, uid as number)
      : localFacts("socket", 0o600, uid as number),
    async (filePath) => filePath === parentPath
      ? localFacts("directory", 0o700, uid as number)
      : localFacts("socket", 0o600, (uid as number) + 1)
  ];
  for (const reader of invalidReaders) {
    await assert.rejects(
      verifyLocalSocketPath(LOCAL_SOCKET_PATH, reader),
      LocalSocketSecurityError
    );
  }
});

test("treats only a missing private socket as eligible for helper fallback", async () => {
  const uid = process.getuid?.();
  assert.notEqual(uid, undefined);
  const parentPath = path.dirname(LOCAL_SOCKET_PATH);
  const missing = Object.assign(new Error("missing"), { code: "ENOENT" });
  assert.equal(await verifyLocalSocketPath(LOCAL_SOCKET_PATH, async (filePath) => {
    if (filePath === parentPath) {
      return localFacts("directory", 0o700, uid as number);
    }
    throw missing;
  }), false);

  const calls: HelperCall[] = [];
  const racingAbsent = new LocalSocketBridgeClient(
    LOCAL_SOCKET_PATH,
    async () => true,
    async () => { throw new LocalSocketAbsentError(); }
  );
  const client = new DeployedHelperBridgeClient(
    validLocator(),
    async (deployment, arguments_, environment) => {
      calls.push({ deployment, arguments_: [...arguments_], environment: { ...environment } });
      return JSON.stringify(contextPayload(["presence.acquire"]));
    },
    racingAbsent
  );
  assert.equal((await client.getContext()).bridgeVersion, 2);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0]?.arguments_, [...COMMON_ARGUMENTS, "--context"]);
});

test("deduplicates concurrent fallback and backs off after helper failure", async () => {
  let now = 10_000;
  let plistReads = 0;
  let helperCalls = 0;
  const locator = new DeployedHelperLocator(
    RUNTIME_PLIST,
    async () => {
      plistReads += 1;
      return validPlist();
    },
    async () => undefined,
    0
  );
  const client = new DeployedHelperBridgeClient(
    locator,
    async () => {
      helperCalls += 1;
      throw new Error("runtime unavailable");
    },
    ABSENT_LOCAL_BRIDGE,
    () => NOW,
    () => NONCE,
    () => now
  );

  const concurrent = await Promise.allSettled([
    client.getContext(),
    client.getContext(),
    client.getContext()
  ]);
  assert.ok(concurrent.every((result) => result.status === "rejected"));
  assert.equal(helperCalls, 1);
  assert.equal(plistReads, 1);

  await captureError(client.getContext());
  await captureError(client.getContext());
  assert.equal(helperCalls, 1);
  assert.equal(plistReads, 1);

  now += FALLBACK_CONTEXT_INITIAL_BACKOFF_MILLISECONDS;
  await captureError(client.getContext());
  assert.equal(helperCalls, 2);
  assert.equal(plistReads, 2);
});

test("caches a successful helper context between two-second polling cycles", async () => {
  let now = 50_000;
  let helperCalls = 0;
  const client = new DeployedHelperBridgeClient(
    validLocator(),
    async () => {
      helperCalls += 1;
      return JSON.stringify(contextPayload(["presence.acquire"]));
    },
    ABSENT_LOCAL_BRIDGE,
    () => NOW,
    () => NONCE,
    () => now
  );

  await client.getContext();
  now += 2_000;
  await client.getContext();
  now += 2_000;
  await client.getContext();
  assert.equal(helperCalls, 1);
});

test("invalidates cached helper context around every fallback mutation", async () => {
  const calls: string[][] = [];
  const client = new DeployedHelperBridgeClient(
    validLocator(),
    async (_deployment, arguments_) => {
      calls.push([...arguments_]);
      if (arguments_.at(-1) === "--context") {
        return JSON.stringify(contextPayload(["event.open"]));
      }
      return JSON.stringify({ result: "OK", action: "open" });
    },
    ABSENT_LOCAL_BRIDGE,
    () => NOW,
    () => NONCE,
    () => 80_000
  );

  await client.getContext();
  await client.sendEventAction("open");
  await client.getContext();
  assert.deepEqual(calls.map((arguments_) => arguments_.slice(COMMON_ARGUMENTS.length)), [
    ["--context"],
    ["custom"],
    ["--context"]
  ]);
});

test("never falls back after socket security, protocol, or bridge errors", async () => {
  for (const failure of [
    new LocalSocketSecurityError(),
    new LocalProtocolError()
  ]) {
    const harness = localOnlyClient(async () => { throw failure; });
    const error = await captureError(harness.client.getContext());
    assert.ok(error instanceof BridgeError);
    assert.equal(error.code, failure instanceof LocalSocketSecurityError ? "not_configured" : "invalid_response");
    assert.deepEqual(harness.counters, { locator: 0, helper: 0, socket: 1 });
  }

  const rejected = localOnlyClient(async () => ({ error: "stale_or_inapplicable_context" }));
  const error = await captureError(rejected.client.getContext());
  assert.ok(error instanceof BridgeError);
  assert.equal(error.code, "stale_context");
  assert.deepEqual(rejected.counters, { locator: 0, helper: 0, socket: 1 });
});

test("enforces local request and response byte and framing bounds", () => {
  const small = encodeLocalRequest({ version: 1, operation: "context" });
  assert.ok(small.byteLength <= MAX_LOCAL_REQUEST_BYTES);
  assert.throws(
    () => encodeLocalRequest({
      version: 1,
      operation: "input",
      payload: { padding: "x".repeat(MAX_LOCAL_REQUEST_BYTES) }
    }),
    LocalProtocolError
  );

  assert.deepEqual(decodeLocalResponse(Buffer.from('{"result":"OK"}\n')), { result: "OK" });
  assert.throws(
    () => decodeLocalResponse(Buffer.alloc(MAX_LOCAL_RESPONSE_BYTES + 1, 0x20)),
    LocalProtocolError
  );
  assert.throws(() => decodeLocalResponse(Buffer.from('{"result":"OK"}')), LocalProtocolError);
  assert.throws(() => decodeLocalResponse(Buffer.from('{}\n{}\n')), LocalProtocolError);
  assert.throws(() => decodeLocalResponse(Buffer.from([0xff, 0x0a])), LocalProtocolError);
  assert.throws(() => decodeLocalResponse(Buffer.from('[]\n')), LocalProtocolError);
});

test("enforces an absolute socket deadline despite periodic response bytes", async () => {
  let interval: ReturnType<typeof setInterval> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  let dataEvents = 0;
  let destroyed = false;
  const connector: LocalSocketConnector = (_socketPath, handlers) => {
    const session = {
      destroy: () => {
        destroyed = true;
        if (interval) {
          clearInterval(interval);
        }
        if (watchdog) {
          clearTimeout(watchdog);
        }
      },
      write: (_data: Buffer) => undefined
    };
    setTimeout(handlers.onConnect, 0);
    interval = setInterval(() => {
      dataEvents += 1;
      handlers.onData(Buffer.from(" "));
    }, 5);
    watchdog = setTimeout(() => {
      handlers.onError(new Error("deadline watchdog"));
    }, 250);
    return session;
  };

  await assert.rejects(
    requestLocalSocket(
      LOCAL_SOCKET_PATH,
      { version: 1, operation: "context" },
      connector,
      35
    ),
    (error: unknown) => error instanceof Error && error.message === "local bridge timed out"
  );
  assert.ok(dataEvents >= 1);
  assert.equal(destroyed, true);
});

test("runs context through the deployed helper with exact arguments and a minimal environment", async () => {
  const calls: HelperCall[] = [];
  const client = clientWith(async (deployment, arguments_, environment) => {
    calls.push({ deployment, arguments_: [...arguments_], environment: { ...environment } });
    return JSON.stringify(contextPayload(["presence.acquire"]));
  });
  const previousSecret = process.env.FOUNDEROS_INPUT_SECRET;
  process.env.FOUNDEROS_INPUT_SECRET = "must-not-cross-the-process-boundary";
  try {
    const context = await client.getContext();
    assert.equal(context.bridgeVersion, 2);
  } finally {
    if (previousSecret === undefined) {
      delete process.env.FOUNDEROS_INPUT_SECRET;
    } else {
      process.env.FOUNDEROS_INPUT_SECRET = previousSecret;
    }
  }

  assert.deepEqual(calls[0]?.arguments_, [...COMMON_ARGUMENTS, "--context"]);
  assert.deepEqual(Object.keys(calls[0]?.environment ?? {}).sort(), [
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "TMPDIR",
    "USER"
  ]);
  assert.equal(calls[0]?.environment.FOUNDEROS_INPUT_SECRET, undefined);
  assert.equal(calls[0]?.environment.HOME, USER_HOME);
  assert.equal(calls[0]?.environment.USER, calls[0]?.environment.LOGNAME);
  assert.ok(calls[0]?.environment.USER);
  assert.equal(calls[0]?.environment.TMPDIR, "/tmp");
});

test("maps open and permission denial to the deployed helper key contract", async () => {
  const openCalls: string[][] = [];
  const openClient = clientWith(commandExecutor(openCalls, ["event.open"], "open"));
  assert.equal((await openClient.sendEventAction("open")).action, "open");
  assert.deepEqual(openCalls, [
    [...COMMON_ARGUMENTS, "--context"],
    [...COMMON_ARGUMENTS, "custom"]
  ]);

  const denyCalls: string[][] = [];
  const denyClient = clientWith(commandExecutor(denyCalls, ["permission.deny"], "deny"));
  assert.equal((await denyClient.sendEventAction("deny")).action, "deny");
  assert.deepEqual(denyCalls[1], [...COMMON_ARGUMENTS, "back"]);
});

test("maps focus to one explicit lease acquisition", async () => {
  const calls: string[][] = [];
  const client = clientWith(commandExecutor(calls, ["presence.acquire"], "acquire"));

  await client.sendPresencePreset("focus50");
  assert.deepEqual(calls[1], [
    ...COMMON_ARGUMENTS,
    "--lease-action",
    "acquire",
    "--lease-id",
    "streamdeck.focus",
    "--state",
    "focus",
    "--ttl-seconds",
    "3000"
  ]);
});

test("release all never sends a lease, state or TTL argument", async () => {
  const calls: string[][] = [];
  const client = clientWith(commandExecutor(calls, ["presence.release_all"], "release_all"));

  await client.sendPresencePreset("releaseManual");
  assert.deepEqual(calls[1], [...COMMON_ARGUMENTS, "--lease-action", "release_all"]);
});

test("does not expose helper stdout, stderr or paths when execution fails", async () => {
  const client = clientWith(async () => {
    const error = new Error("private stderr FOUNDEROS_INPUT_SECRET=hunter2");
    Object.assign(error, { stdout: "private event contents", stderr: path.join(USER_HOME, "private") });
    throw error;
  });

  const error = await captureError(client.getContext());
  assert.ok(error instanceof BridgeError);
  assert.equal(error.code, "offline");
  assert.equal(error.message, "FounderOS deployed helper failed");
  assert.equal(JSON.stringify(error).includes("hunter2"), false);
});

interface HelperCall {
  deployment: HelperDeployment;
  arguments_: string[];
  environment: NodeJS.ProcessEnv;
}

function localOnlyClient(
  responder: (request: LocalBridgeRequest) => Promise<Record<string, unknown>>
): {
  client: DeployedHelperBridgeClient;
  counters: { locator: number; helper: number; socket: number };
} {
  const counters = { locator: 0, helper: 0, socket: 0 };
  const locator = new DeployedHelperLocator(
    RUNTIME_PLIST,
    async () => validPlist(),
    async () => {
      counters.locator += 1;
      throw new Error("the deployment locator must not run on the socket path");
    },
    0
  );
  const local = new LocalSocketBridgeClient(
    LOCAL_SOCKET_PATH,
    async () => true,
    async (_socketPath, request) => {
      counters.socket += 1;
      return await responder(request);
    }
  );
  const client = new DeployedHelperBridgeClient(
    locator,
    async () => {
      counters.helper += 1;
      throw new Error("the helper must not spawn on the socket path");
    },
    local,
    () => NOW,
    () => NONCE
  );
  return { client, counters };
}

function localFacts(
  kind: "directory" | "socket" | "file" | "symlink",
  mode: number,
  uid: number
): LocalPathFacts {
  return {
    uid,
    mode,
    isDirectory: () => kind === "directory",
    isSocket: () => kind === "socket",
    isSymbolicLink: () => kind === "symlink"
  };
}

function localContextResponse(capabilities: string[]): Record<string, unknown> {
  return {
    bridge_version: 2,
    context: contextPayload(capabilities)
  };
}

function validPlist(): Record<string, unknown> {
  return {
    Label: "com.founderos.runtime",
    ProgramArguments: [PYTHON, `${ROOT}/apps/founderos.py`, "--config", CONFIG],
    WorkingDirectory: ROOT
  };
}

function locatorFor(plist: unknown, verifyPath: PathVerifier = async () => undefined): DeployedHelperLocator {
  return new DeployedHelperLocator(RUNTIME_PLIST, async () => plist, verifyPath, 0);
}

function validLocator(verifyPath: PathVerifier = async () => undefined): DeployedHelperLocator {
  return locatorFor(validPlist(), verifyPath);
}

function clientWith(executor: HelperExecutor): DeployedHelperBridgeClient {
  return new DeployedHelperBridgeClient(validLocator(), executor, ABSENT_LOCAL_BRIDGE);
}

function commandExecutor(
  calls: string[][],
  capabilities: string[],
  action: string
): HelperExecutor {
  return async (_deployment, arguments_) => {
    calls.push([...arguments_]);
    if (arguments_.at(-1) === "--context") {
      return JSON.stringify(contextPayload(capabilities));
    }
    return JSON.stringify({ result: "OK", action });
  };
}

function contextPayload(capabilities: string[]): Record<string, unknown> {
  return {
    bridge_version: 2,
    event_id: capabilities.some((item) => item.startsWith("event.") || item.startsWith("permission."))
      ? "opaque:event"
      : "",
    request_id: "opaque-request",
    kind: capabilities.some((item) => item.startsWith("permission.")) ? "permission_request" : "decision",
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
  };
}

async function rejectsAsNotConfigured(promise: Promise<unknown>): Promise<void> {
  const error = await captureError(promise);
  assert.ok(error instanceof BridgeError);
  assert.equal(error.code, "not_configured");
}

async function captureError(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail("expected the promise to reject");
}
