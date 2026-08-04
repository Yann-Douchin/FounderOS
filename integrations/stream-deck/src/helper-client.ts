import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { access, lstat } from "node:fs/promises";
import { createConnection } from "node:net";
import path from "node:path";
import { promisify } from "node:util";
import { constants } from "node:fs";
import { homedir, userInfo } from "node:os";

import {
  BRIDGE_URL,
  EVENT_BRIDGE_KEYS,
  PRESENCE_PRESETS,
  parseBridgeContext,
  type BridgeContext,
  type EventCapability,
  type PresencePreset
} from "./domain.js";
import { BridgeError, type BridgeClient, type CommandResult } from "./protocol.js";

export const USER_HOME = path.resolve(homedir());
export const RUNTIME_PLIST = path.join(
  USER_HOME,
  "Library",
  "LaunchAgents",
  "com.founderos.runtime.plist"
);
export const DEPLOYMENTS_ROOT = path.join(
  USER_HOME,
  "Library",
  "Application Support",
  "FounderOS",
  "deployments"
);
export const LOCAL_SOCKET_PATH = path.join(
  USER_HOME,
  "Library",
  "Application Support",
  "FounderOS",
  "founderos-input.sock"
);
export const MAX_LOCAL_REQUEST_BYTES = 4096;
export const MAX_LOCAL_RESPONSE_BYTES = 65536;
export const FALLBACK_CONTEXT_SUCCESS_COOLDOWN_MILLISECONDS = 10000;
export const FALLBACK_CONTEXT_INITIAL_BACKOFF_MILLISECONDS = 5000;
export const FALLBACK_CONTEXT_MAX_BACKOFF_MILLISECONDS = 60000;
const LOCAL_SOCKET_TIMEOUT_MILLISECONDS = 3000;

export interface HelperDeployment {
  python: string;
  root: string;
  helper: string;
  config: string;
}

export type PlistReader = (plistPath: string) => Promise<unknown>;
export type PathVerifier = (filePath: string, kind: "file" | "directory" | "executable") => Promise<void>;
export type HelperExecutor = (
  deployment: HelperDeployment,
  arguments_: readonly string[],
  environment: Readonly<NodeJS.ProcessEnv>
) => Promise<string>;
export interface LocalPathFacts {
  readonly uid: number;
  readonly mode: number;
  isDirectory(): boolean;
  isSocket(): boolean;
  isSymbolicLink(): boolean;
}
export type LocalPathFactsReader = (filePath: string) => Promise<LocalPathFacts>;
export type LocalSocketVerifier = (socketPath: string) => Promise<boolean>;
export interface LocalBridgeRequest {
  version: 1;
  operation: "context" | "input" | "presence";
  payload?: Readonly<Record<string, unknown>>;
}
export type LocalSocketRequester = (
  socketPath: string,
  request: LocalBridgeRequest
) => Promise<Record<string, unknown>>;
export interface LocalSocketSession {
  destroy(): void;
  write(data: Buffer): void;
}
export interface LocalSocketHandlers {
  onConnect(): void;
  onData(chunk: Buffer): void;
  onEnd(): void;
  onError(error: NodeJS.ErrnoException): void;
}
export type LocalSocketConnector = (
  socketPath: string,
  handlers: LocalSocketHandlers
) => LocalSocketSession;

const executeFile = promisify(execFile);

const defaultPlistReader: PlistReader = async (plistPath) => {
  const result = await executeFile(
    "/usr/bin/plutil",
    ["-convert", "json", "-o", "-", plistPath],
    {
      encoding: "utf8",
      timeout: 2500,
      maxBuffer: 65536,
      env: safeChildEnvironment()
    }
  );
  return JSON.parse(result.stdout) as unknown;
};

const defaultPathVerifier: PathVerifier = async (filePath, kind) => {
  const facts = await lstat(filePath);
  if (facts.isSymbolicLink()) {
    throw new Error("symbolic paths are not accepted");
  }
  if (kind === "directory" ? !facts.isDirectory() : !facts.isFile()) {
    throw new Error("deployment path type is invalid");
  }
  const currentUid = process.getuid?.();
  if (facts.uid !== 0 && facts.uid !== currentUid) {
    throw new Error("deployment path owner is invalid");
  }
  if ((facts.mode & 0o022) !== 0) {
    throw new Error("deployment path is writable by another account");
  }
  if (kind === "executable") {
    await access(filePath, constants.X_OK);
  }
};

const defaultHelperExecutor: HelperExecutor = async (deployment, arguments_, environment) => {
  const result = await executeFile(deployment.python, [deployment.helper, ...arguments_], {
    cwd: deployment.root,
    encoding: "utf8",
    timeout: 5500,
    maxBuffer: 65536,
    env: environment
  });
  return result.stdout;
};

export class LocalSocketAbsentError extends Error {
  constructor() {
    super("FounderOS local socket is absent");
    this.name = "LocalSocketAbsentError";
  }
}

export class LocalSocketSecurityError extends Error {
  constructor() {
    super("FounderOS local socket security validation failed");
    this.name = "LocalSocketSecurityError";
  }
}

export class LocalProtocolError extends Error {
  constructor() {
    super("FounderOS local bridge protocol failed");
    this.name = "LocalProtocolError";
  }
}

class LocalBridgeRejectedError extends Error {
  constructor(readonly reason: string) {
    super("FounderOS local bridge rejected the request");
    this.name = "LocalBridgeRejectedError";
  }
}

export class LocalSocketBridgeClient {
  constructor(
    private readonly socketPath = LOCAL_SOCKET_PATH,
    private readonly verifySocket: LocalSocketVerifier = verifyLocalSocketPath,
    private readonly requestSocket: LocalSocketRequester = requestLocalSocket
  ) {}

  async request(request: LocalBridgeRequest): Promise<Record<string, unknown>> {
    if (this.socketPath !== LOCAL_SOCKET_PATH) {
      throw new LocalSocketSecurityError();
    }
    if (!await this.verifySocket(this.socketPath)) {
      throw new LocalSocketAbsentError();
    }
    const response = await this.requestSocket(this.socketPath, request);
    const error = response.error;
    if (typeof error === "string" && error) {
      throw new LocalBridgeRejectedError(error);
    }
    return response;
  }
}

export async function verifyLocalSocketPath(
  socketPath: string,
  readFacts: LocalPathFactsReader = lstat
): Promise<boolean> {
  if (socketPath !== LOCAL_SOCKET_PATH || !path.isAbsolute(socketPath) || path.normalize(socketPath) !== socketPath) {
    throw new LocalSocketSecurityError();
  }
  const uid = process.getuid?.();
  if (uid === undefined) {
    throw new LocalSocketSecurityError();
  }
  let parent: LocalPathFacts;
  try {
    parent = await readFacts(path.dirname(socketPath));
  } catch (error) {
    if (isMissingPath(error)) {
      return false;
    }
    throw error;
  }
  if (
    parent.isSymbolicLink() ||
    !parent.isDirectory() ||
    parent.uid !== uid ||
    (parent.mode & 0o7777) !== 0o700
  ) {
    throw new LocalSocketSecurityError();
  }
  let socketFacts: LocalPathFacts;
  try {
    socketFacts = await readFacts(socketPath);
  } catch (error) {
    if (isMissingPath(error)) {
      return false;
    }
    throw error;
  }
  if (
    socketFacts.isSymbolicLink() ||
    !socketFacts.isSocket() ||
    socketFacts.uid !== uid ||
    (socketFacts.mode & 0o7777) !== 0o600
  ) {
    throw new LocalSocketSecurityError();
  }
  return true;
}

export function encodeLocalRequest(request: LocalBridgeRequest): Buffer {
  const body = Buffer.from(`${JSON.stringify(request)}\n`, "utf8");
  if (body.byteLength > MAX_LOCAL_REQUEST_BYTES) {
    throw new LocalProtocolError();
  }
  return body;
}

export function decodeLocalResponse(response: Buffer): Record<string, unknown> {
  if (
    response.byteLength === 0 ||
    response.byteLength > MAX_LOCAL_RESPONSE_BYTES ||
    response.at(-1) !== 0x0a ||
    response.subarray(0, -1).includes(0x0a)
  ) {
    throw new LocalProtocolError();
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(response.subarray(0, -1));
    return objectValue(JSON.parse(text));
  } catch {
    throw new LocalProtocolError();
  }
}

export async function requestLocalSocket(
  socketPath: string,
  request: LocalBridgeRequest,
  connect: LocalSocketConnector = connectLocalSocket,
  deadlineMilliseconds = LOCAL_SOCKET_TIMEOUT_MILLISECONDS
): Promise<Record<string, unknown>> {
  const body = encodeLocalRequest(request);
  return await new Promise<Record<string, unknown>>((resolve, reject) => {
    const chunks: Buffer[] = [];
    let responseBytes = 0;
    let settled = false;
    let session: LocalSocketSession | undefined;
    let deadline: ReturnType<typeof setTimeout>;

    const finish = (error?: unknown, response?: Record<string, unknown>): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(deadline);
      session?.destroy();
      if (error !== undefined) {
        reject(error);
      } else if (response !== undefined) {
        resolve(response);
      } else {
        reject(new LocalProtocolError());
      }
    };

    deadline = setTimeout(() => {
      finish(new Error("local bridge timed out"));
    }, deadlineMilliseconds);
    try {
      session = connect(socketPath, {
        onConnect: () => {
          session?.write(body);
        },
        onData: (chunk) => {
          if (settled) {
            return;
          }
          responseBytes += chunk.byteLength;
          if (responseBytes > MAX_LOCAL_RESPONSE_BYTES) {
            finish(new LocalProtocolError());
            return;
          }
          chunks.push(chunk);
          if (chunk.includes(0x0a)) {
            try {
              finish(undefined, decodeLocalResponse(Buffer.concat(chunks, responseBytes)));
            } catch (error) {
              finish(error);
            }
          }
        },
        onEnd: () => {
          if (!settled) {
            finish(new LocalProtocolError());
          }
        },
        onError: (error) => {
          finish(isMissingPath(error) ? new LocalSocketAbsentError() : error);
        }
      });
      if (settled) {
        session.destroy();
      }
    } catch (error) {
      finish(isMissingPath(error) ? new LocalSocketAbsentError() : error);
    }
  });
}

const connectLocalSocket: LocalSocketConnector = (socketPath, handlers) => {
  const socket = createConnection(socketPath);
  socket.once("connect", handlers.onConnect);
  socket.on("data", handlers.onData);
  socket.once("end", handlers.onEnd);
  socket.once("error", handlers.onError);
  return {
    destroy: () => socket.destroy(),
    write: (data) => { socket.write(data); }
  };
};

export class DeployedHelperLocator {
  private cached: { value: HelperDeployment; expiresAt: number } | undefined;

  constructor(
    private readonly plistPath = RUNTIME_PLIST,
    private readonly readPlist: PlistReader = defaultPlistReader,
    private readonly verifyPath: PathVerifier = defaultPathVerifier,
    private readonly cacheMilliseconds = 10000
  ) {}

  async locate(force = false): Promise<HelperDeployment> {
    if (!force && this.cached && this.cached.expiresAt > Date.now()) {
      return this.cached.value;
    }
    try {
      if (this.plistPath !== RUNTIME_PLIST) {
        throw new Error("runtime plist path is not allowlisted");
      }
      await this.verifyPath(this.plistPath, "file");
      const plist = objectValue(await this.readPlist(this.plistPath));
      if (plist.Label !== "com.founderos.runtime") {
        throw new Error("runtime label is invalid");
      }
      const arguments_ = plist.ProgramArguments;
      if (
        !Array.isArray(arguments_) ||
        arguments_.length !== 4 ||
        !arguments_.every((item) => typeof item === "string")
      ) {
        throw new Error("runtime ProgramArguments are invalid");
      }
      const [python, runtimeProgram, configFlag, config] = arguments_ as string[];
      if (!python || !runtimeProgram || !config || configFlag !== "--config") {
        throw new Error("runtime ProgramArguments are incomplete");
      }
      if (!isAllowlistedPython(python)) {
        throw new Error("runtime Python is not allowlisted");
      }
      const root = path.dirname(path.dirname(runtimeProgram));
      if (!isImmutableDeploymentRoot(root)) {
        throw new Error("runtime deployment root is invalid");
      }
      if (
        runtimeProgram !== path.join(root, "apps", "founderos.py") ||
        config !== path.join(root, "founderos.runtime.json") ||
        plist.WorkingDirectory !== root
      ) {
        throw new Error("runtime deployment arguments do not share one immutable root");
      }
      const helper = path.join(root, "apps", "founderos_input.py");
      await Promise.all([
        this.verifyPath(python, "executable"),
        this.verifyPath(root, "directory"),
        this.verifyPath(runtimeProgram, "file"),
        this.verifyPath(config, "file"),
        this.verifyPath(helper, "file")
      ]);
      const value = Object.freeze({ python, root, helper, config });
      this.cached = { value, expiresAt: Date.now() + this.cacheMilliseconds };
      return value;
    } catch {
      throw new BridgeError("not_configured", "FounderOS deployed helper is unavailable");
    }
  }

  invalidate(): void {
    this.cached = undefined;
  }
}

export class DeployedHelperBridgeClient implements BridgeClient {
  private fallbackContextCache:
    | { until: number; value: unknown }
    | { until: number; error: BridgeError }
    | undefined;
  private fallbackContextInFlight: { epoch: number; promise: Promise<unknown> } | undefined;
  private fallbackContextEpoch = 0;
  private fallbackFailureCount = 0;

  constructor(
    private readonly locator = new DeployedHelperLocator(),
    private readonly executeHelper: HelperExecutor = defaultHelperExecutor,
    private readonly localBridge = new LocalSocketBridgeClient(),
    private readonly nowSeconds: () => number = () => Math.floor(Date.now() / 1000),
    private readonly makeNonce: () => string = () => randomBytes(24).toString("base64url"),
    private readonly nowMilliseconds: () => number = () => Date.now()
  ) {}

  async getContext(): Promise<BridgeContext> {
    const payload = await this.run(
      { version: 1, operation: "context" },
      ["--context"],
      true
    );
    try {
      const response = objectValue(payload);
      const context = response.context === undefined
        ? response
        : {
            ...objectValue(response.context),
            bridge_version: objectValue(response.context).bridge_version ?? response.bridge_version ?? 1
          };
      return parseBridgeContext({
        bridge_version: context.bridge_version ?? 1,
        context
      });
    } catch {
      throw new BridgeError("invalid_response", "FounderOS returned an invalid context");
    }
  }

  async sendEventAction(action: EventCapability): Promise<CommandResult> {
    const context = await this.getContext();
    if (!context.eventId || !context.capabilities.has(action)) {
      throw new BridgeError("stale_context", "The FounderOS action is not available");
    }
    const payload = {
      key: EVENT_BRIDGE_KEYS[action],
      event_id: context.eventId,
      request_id: context.requestId,
      ...this.freshEnvelope()
    };
    return this.commandResult(await this.run(
      { version: 1, operation: "input", payload },
      [EVENT_BRIDGE_KEYS[action]]
    ));
  }

  async sendPresencePreset(preset: PresencePreset): Promise<CommandResult> {
    const definition = PRESENCE_PRESETS[preset];
    const context = await this.getContext();
    if (!context.capabilities.has(definition.capability)) {
      throw new BridgeError("unavailable", "FounderOS presence control is not available");
    }
    const request = definition.request;
    const ttlSeconds = "ttl_seconds" in request ? request.ttl_seconds : undefined;
    if (
      ttlSeconds !== undefined &&
      context.presence &&
      (ttlSeconds < context.presence.minTtlSeconds || ttlSeconds > context.presence.maxTtlSeconds)
    ) {
      throw new BridgeError("unavailable", "Presence TTL is outside the FounderOS limits");
    }
    const arguments_ = ["--lease-action", request.action];
    if ("lease_id" in request && request.lease_id) {
      arguments_.push("--lease-id", request.lease_id);
    }
    if ("state" in request && request.state) {
      arguments_.push("--state", request.state);
    }
    if (ttlSeconds !== undefined) {
      arguments_.push("--ttl-seconds", String(ttlSeconds));
    }
    const payload: Record<string, unknown> = {
      action: request.action,
      ...this.freshEnvelope()
    };
    if ("lease_id" in request && request.lease_id) {
      payload.lease_id = request.lease_id;
    }
    if ("state" in request && request.state) {
      payload.state = request.state;
    }
    if (ttlSeconds !== undefined) {
      payload.ttl_seconds = ttlSeconds;
    }
    return this.commandResult(await this.run(
      { version: 1, operation: "presence", payload },
      arguments_
    ));
  }

  private async run(
    localRequest: LocalBridgeRequest,
    helperArguments: readonly string[],
    cacheContextFallback = false
  ): Promise<unknown> {
    try {
      const response = await this.localBridge.request(localRequest);
      this.invalidateFallbackContext();
      return response;
    } catch (error) {
      if (!(error instanceof LocalSocketAbsentError)) {
        this.invalidateFallbackContext();
        throw bridgeErrorForLocalFailure(error);
      }
    }
    if (cacheContextFallback) {
      return await this.runCachedContextFallback(helperArguments);
    }
    this.invalidateFallbackContext();
    try {
      return await this.runHelperFallback(helperArguments);
    } finally {
      this.invalidateFallbackContext();
    }
  }

  private async runCachedContextFallback(helperArguments: readonly string[]): Promise<unknown> {
    const now = this.nowMilliseconds();
    const cached = this.fallbackContextCache;
    if (cached && now < cached.until) {
      if ("error" in cached) {
        throw cached.error;
      }
      return cached.value;
    }
    const epoch = this.fallbackContextEpoch;
    const active = this.fallbackContextInFlight;
    if (active && active.epoch === epoch) {
      return await active.promise;
    }
    let inFlight: { epoch: number; promise: Promise<unknown> };
    const promise = this.runHelperFallback(helperArguments).then(
      (value) => {
        if (this.fallbackContextEpoch === epoch) {
          this.fallbackFailureCount = 0;
          this.fallbackContextCache = {
            until: this.nowMilliseconds() + FALLBACK_CONTEXT_SUCCESS_COOLDOWN_MILLISECONDS,
            value
          };
        }
        return value;
      },
      (error: unknown) => {
        const bridgeError = error instanceof BridgeError
          ? error
          : new BridgeError("offline", "FounderOS deployed helper failed");
        if (this.fallbackContextEpoch === epoch) {
          this.fallbackFailureCount += 1;
          const exponent = Math.min(this.fallbackFailureCount - 1, 20);
          const delay = Math.min(
            FALLBACK_CONTEXT_INITIAL_BACKOFF_MILLISECONDS * (2 ** exponent),
            FALLBACK_CONTEXT_MAX_BACKOFF_MILLISECONDS
          );
          this.fallbackContextCache = {
            until: this.nowMilliseconds() + delay,
            error: bridgeError
          };
        }
        throw bridgeError;
      }
    ).finally(() => {
      if (this.fallbackContextInFlight === inFlight) {
        this.fallbackContextInFlight = undefined;
      }
    });
    inFlight = { epoch, promise };
    this.fallbackContextInFlight = inFlight;
    return await promise;
  }

  private invalidateFallbackContext(): void {
    this.fallbackContextEpoch += 1;
    this.fallbackContextCache = undefined;
    this.fallbackFailureCount = 0;
  }

  private async runHelperFallback(helperArguments: readonly string[]): Promise<unknown> {
    try {
      const deployment = await this.locator.locate();
      const stdout = await this.executeHelper(
        deployment,
        ["--config", deployment.config, "--url", BRIDGE_URL, ...helperArguments],
        safeChildEnvironment()
      );
      if (Buffer.byteLength(stdout, "utf8") > MAX_LOCAL_RESPONSE_BYTES) {
        throw new Error("helper response is too large");
      }
      return JSON.parse(stdout) as unknown;
    } catch (error) {
      if (error instanceof BridgeError) {
        throw error;
      }
      this.locator.invalidate();
      throw new BridgeError("offline", "FounderOS deployed helper failed");
    }
  }

  private freshEnvelope(): { issued_at: number; nonce: string } {
    const issuedAt = this.nowSeconds();
    const nonce = this.makeNonce();
    if (!Number.isInteger(issuedAt) || !/^[A-Za-z0-9_-]{16,128}$/.test(nonce)) {
      throw new BridgeError("offline", "FounderOS request envelope generation failed");
    }
    return { issued_at: issuedAt, nonce };
  }

  private commandResult(payload: unknown): CommandResult {
    try {
      const result = objectValue(payload);
      if (result.result !== "OK") {
        throw new Error("FounderOS did not confirm the command");
      }
      const presence = typeof result.presence === "object" && result.presence !== null
        ? result.presence as Record<string, unknown>
        : undefined;
      return {
        result: "OK",
        action: typeof result.action === "string"
          ? result.action
          : typeof presence?.action === "string"
            ? presence.action
            : "presence"
      };
    } catch {
      throw new BridgeError("invalid_response", "FounderOS helper returned an invalid result");
    }
  }
}

function bridgeErrorForLocalFailure(error: unknown): BridgeError {
  if (error instanceof BridgeError) {
    return error;
  }
  if (error instanceof LocalSocketSecurityError) {
    return new BridgeError("not_configured", "FounderOS local socket failed security validation");
  }
  if (error instanceof LocalProtocolError) {
    return new BridgeError("invalid_response", "FounderOS local bridge returned an invalid response");
  }
  if (error instanceof LocalBridgeRejectedError) {
    if (error.reason === "stale_or_inapplicable_context") {
      return new BridgeError("stale_context", "The FounderOS action is no longer available");
    }
    if (error.reason === "presence_unavailable") {
      return new BridgeError("unavailable", "FounderOS presence control is not available");
    }
    if (error.reason === "request_failed") {
      return new BridgeError("offline", "FounderOS local bridge failed");
    }
    return new BridgeError("invalid_response", "FounderOS local bridge rejected an invalid request");
  }
  return new BridgeError("offline", "FounderOS local bridge failed");
}

function isMissingPath(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT";
}

function isImmutableDeploymentRoot(value: string): boolean {
  const prefix = `${DEPLOYMENTS_ROOT}/`;
  return value.startsWith(prefix) && /^[a-f0-9]{64}$/.test(value.slice(prefix.length));
}

function isAllowlistedPython(value: string): boolean {
  if (!path.isAbsolute(value) || path.normalize(value) !== value) {
    return false;
  }
  return /^python(?:3(?:\.\d+)?)?$/.test(path.basename(value));
}

function safeChildEnvironment(): NodeJS.ProcessEnv {
  const account = userInfo().username;
  return {
    HOME: USER_HOME,
    USER: account,
    LOGNAME: account,
    PATH: "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    LANG: "en_US.UTF-8",
    TMPDIR: "/tmp"
  };
}

function objectValue(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError("expected an object");
  }
  return value as Record<string, unknown>;
}
