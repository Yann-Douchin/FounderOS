import type { BridgeContext, EventCapability, PresencePreset } from "./domain.js";

export type BridgeErrorCode =
  | "not_configured"
  | "offline"
  | "unauthorized"
  | "invalid_response"
  | "unavailable"
  | "stale_context";

export class BridgeError extends Error {
  constructor(
    readonly code: BridgeErrorCode,
    message: string,
    readonly status = 0
  ) {
    super(message);
    this.name = "BridgeError";
  }
}

export interface CommandResult {
  result: string;
  action: string;
}

export interface BridgeClient {
  getContext(): Promise<BridgeContext>;
  sendEventAction(action: EventCapability): Promise<CommandResult>;
  sendPresencePreset(preset: PresencePreset): Promise<CommandResult>;
}
