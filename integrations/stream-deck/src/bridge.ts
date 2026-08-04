import { createHmac, randomBytes } from "node:crypto";

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

export interface SecretProvider {
  getSecret(): Promise<string>;
  invalidate?(): void;
}

export class LoopbackBridgeClient implements BridgeClient {
  private readonly baseUrl: string;

  constructor(
    private readonly secretProvider: SecretProvider,
    baseUrl = BRIDGE_URL,
    private readonly timeoutMilliseconds = 2500,
    private readonly fetchImplementation: typeof fetch = fetch
  ) {
    this.baseUrl = validateLoopbackUrl(baseUrl);
  }

  async getContext(): Promise<BridgeContext> {
    const secret = await this.getSecret();
    const payload = await this.requestJson("/context", {
      method: "GET",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${secret}`,
        "Cache-Control": "no-store"
      }
    });
    try {
      return parseBridgeContext(payload);
    } catch {
      throw new BridgeError("invalid_response", "FounderOS returned an invalid context");
    }
  }

  async sendEventAction(action: EventCapability): Promise<CommandResult> {
    const secret = await this.getSecret();
    const context = await this.getContext();
    if (!context.eventId || !context.capabilities.has(action)) {
      throw new BridgeError("stale_context", "The FounderOS action is not available");
    }
    const body = canonicalJson({
      event_id: context.eventId,
      issued_at: unixTimestamp(),
      key: EVENT_BRIDGE_KEYS[action],
      nonce: createNonce(),
      request_id: context.requestId
    });
    return this.sendSigned("/input", secret, body);
  }

  async sendPresencePreset(preset: PresencePreset): Promise<CommandResult> {
    const definition = PRESENCE_PRESETS[preset];
    const secret = await this.getSecret();
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
    const body = canonicalJson({
      ...request,
      issued_at: unixTimestamp(),
      nonce: createNonce()
    });
    return this.sendSigned("/presence/lease", secret, body);
  }

  private async sendSigned(path: string, secret: string, body: string): Promise<CommandResult> {
    const payload = await this.requestJson(path, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-FounderOS-Signature": signatureFor(secret, body)
      },
      body
    });
    const result = objectValue(payload);
    if (typeof result.result !== "string" || result.result !== "OK") {
      throw new BridgeError("invalid_response", "FounderOS did not confirm the action");
    }
    return {
      result: result.result,
      action: typeof result.action === "string" ? result.action : "presence"
    };
  }

  private async getSecret(): Promise<string> {
    try {
      return await this.secretProvider.getSecret();
    } catch {
      throw new BridgeError("not_configured", "FounderOS input secret is not configured");
    }
  }

  private async requestJson(path: string, init: RequestInit): Promise<unknown> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMilliseconds);
    try {
      const response = await this.fetchImplementation(`${this.baseUrl}${path}`, {
        ...init,
        redirect: "error",
        signal: controller.signal
      });
      const text = await readBoundedResponse(response, 16384);
      if (!response.ok) {
        if (response.status === 401) {
          this.secretProvider.invalidate?.();
          throw new BridgeError("unauthorized", "FounderOS rejected the local credential", response.status);
        }
        if (response.status === 409) {
          throw new BridgeError("stale_context", "FounderOS rejected a stale or inapplicable action", response.status);
        }
        throw new BridgeError("unavailable", `FounderOS returned HTTP ${response.status}`, response.status);
      }
      try {
        return JSON.parse(text) as unknown;
      } catch {
        throw new BridgeError("invalid_response", "FounderOS returned invalid JSON");
      }
    } catch (error) {
      if (error instanceof BridgeError) {
        throw error;
      }
      throw new BridgeError("offline", "FounderOS loopback bridge is offline");
    } finally {
      clearTimeout(timeout);
    }
  }
}

export function validateLoopbackUrl(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError("FounderOS bridge URL is invalid");
  }
  const loopbackHosts = new Set(["127.0.0.1", "[::1]", "localhost"]);
  if (
    parsed.protocol !== "http:" ||
    !loopbackHosts.has(parsed.hostname) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.pathname !== "/" && parsed.pathname !== "")
  ) {
    throw new TypeError("FounderOS bridge must be a credential-free loopback HTTP endpoint");
  }
  return parsed.origin;
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortJson(value));
}

export function signatureFor(secret: string, body: string): string {
  return `sha256=${createHmac("sha256", secret).update(body, "utf8").digest("hex")}`;
}

function sortJson(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortJson);
  }
  if (typeof value === "object" && value !== null) {
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item !== undefined) {
        result[key] = sortJson(item);
      }
    }
    return result;
  }
  return value;
}

async function readBoundedResponse(response: Response, maximumBytes: number): Promise<string> {
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > maximumBytes) {
    throw new BridgeError("invalid_response", "FounderOS response is too large");
  }
  return text;
}

function createNonce(): string {
  return randomBytes(24).toString("base64url");
}

function unixTimestamp(): number {
  return Math.floor(Date.now() / 1000);
}

function objectValue(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new BridgeError("invalid_response", "FounderOS returned a non-object response");
  }
  return value as Record<string, unknown>;
}
