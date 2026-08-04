export const PLUGIN_UUID = "com.yanndouchin.founderos-actions";
export const BRIDGE_URL = "http://127.0.0.1:8765";
export const HOLD_MILLISECONDS = 1200;

export const ACTION_UUIDS = {
  status: `${PLUGIN_UUID}.status`,
  open: `${PLUGIN_UUID}.open`,
  snooze: `${PLUGIN_UUID}.snooze`,
  acknowledge: `${PLUGIN_UUID}.acknowledge`,
  allow: `${PLUGIN_UUID}.allow`,
  deny: `${PLUGIN_UUID}.deny`,
  presence: `${PLUGIN_UUID}.presence`
} as const;

export type EventCapability = "open" | "snooze" | "acknowledge" | "allow" | "deny";
export type PresenceCapability =
  | "presence.acquire"
  | "presence.renew"
  | "presence.release"
  | "presence.release_all";
export type Capability = EventCapability | PresenceCapability;
export type PresenceState = "available" | "focus" | "manual_call" | "meeting" | "recording";
export type MutablePresenceState = "focus" | "manual_call" | "recording";

export interface PresenceSnapshot {
  state: PresenceState;
  busy: boolean;
  calendarKnown: boolean;
  matterOutputEnabled: boolean;
  activeLeaseCount: number;
  nextExpiryAt: string;
  allowedStates: readonly MutablePresenceState[];
  minTtlSeconds: number;
  maxTtlSeconds: number;
}

export interface BridgeContext {
  bridgeVersion: number;
  eventId: string;
  requestId: string;
  kind: string;
  capabilities: ReadonlySet<Capability>;
  presence: PresenceSnapshot | null;
}

export const EVENT_BRIDGE_KEYS: Readonly<Record<EventCapability, string>> = {
  open: "custom",
  snooze: "back",
  acknowledge: "ok",
  allow: "ok",
  deny: "back"
};

export function capabilityForCockpitSlot(
  slot: "snooze" | "acknowledge",
  capabilities: ReadonlySet<Capability>
): EventCapability {
  if (slot === "snooze") {
    return capabilities.has("deny") ? "deny" : "snooze";
  }
  return capabilities.has("allow") ? "allow" : "acknowledge";
}

export const PRESENCE_PRESETS = {
  focus50: {
    title: "Focus\n50 min",
    shortLabel: "50",
    capability: "presence.acquire",
    request: {
      action: "acquire",
      lease_id: "streamdeck.focus",
      state: "focus",
      ttl_seconds: 3000
    }
  },
  manualCallStart: {
    title: "Start\ncall",
    shortLabel: "CALL",
    capability: "presence.acquire",
    request: {
      action: "acquire",
      lease_id: "streamdeck.manual_call",
      state: "manual_call",
      ttl_seconds: 7200
    }
  },
  manualCallStop: {
    title: "End\ncall",
    shortLabel: "STOP",
    capability: "presence.release",
    request: {
      action: "release",
      lease_id: "streamdeck.manual_call"
    }
  },
  recordingStart: {
    title: "Start\nREC",
    shortLabel: "REC",
    capability: "presence.acquire",
    request: {
      action: "acquire",
      lease_id: "streamdeck.recording",
      state: "recording",
      ttl_seconds: 14400
    }
  },
  recordingStop: {
    title: "End\nREC",
    shortLabel: "STOP",
    capability: "presence.release",
    request: {
      action: "release",
      lease_id: "streamdeck.recording"
    }
  },
  recordingRenew: {
    title: "Extend\nREC",
    shortLabel: "+REC",
    capability: "presence.renew",
    request: {
      action: "renew",
      lease_id: "streamdeck.recording",
      ttl_seconds: 14400
    }
  },
  releaseManual: {
    title: "Release\npresence",
    shortLabel: "FREE",
    capability: "presence.release_all",
    request: {
      action: "release_all"
    }
  }
} as const satisfies Record<string, PresencePresetDefinition>;

export type PresencePreset = keyof typeof PRESENCE_PRESETS;

interface PresencePresetDefinition {
  title: string;
  shortLabel: string;
  capability: PresenceCapability;
  request: {
    action: "acquire" | "renew" | "release" | "release_all";
    lease_id?: string;
    state?: MutablePresenceState;
    ttl_seconds?: number;
  };
}

const CAPABILITIES = new Set<Capability>([
  "open",
  "snooze",
  "acknowledge",
  "allow",
  "deny",
  "presence.acquire",
  "presence.renew",
  "presence.release",
  "presence.release_all"
]);
const WIRE_CAPABILITIES: Readonly<Record<string, Capability>> = {
  "event.open": "open",
  "event.snooze": "snooze",
  "event.acknowledge": "acknowledge",
  "permission.allow": "allow",
  "permission.deny": "deny",
  "presence.acquire": "presence.acquire",
  "presence.renew": "presence.renew",
  "presence.release": "presence.release",
  "presence.release_all": "presence.release_all"
};
const PRESENCE_STATES = new Set<PresenceState>([
  "available",
  "focus",
  "manual_call",
  "meeting",
  "recording"
]);
const MUTABLE_STATES = new Set<MutablePresenceState>(["focus", "manual_call", "recording"]);

export function parseBridgeContext(payload: unknown): BridgeContext {
  const outer = objectValue(payload, "bridge response");
  const context = objectValue(outer.context, "bridge context");
  const eventId = boundedString(context.event_id, 256);
  const requestId = boundedString(context.request_id, 64);
  const kind = boundedString(context.kind, 64);
  const versionValue = context.bridge_version ?? outer.bridge_version ?? 1;
  const bridgeVersion = integerInRange(versionValue, 1, 100, "bridge_version");
  const capabilities = parseCapabilities(context.capabilities, eventId, kind);
  const presence = context.presence === undefined || context.presence === null ? null : parsePresence(context.presence);
  return { bridgeVersion, eventId, requestId, kind, capabilities, presence };
}

export function isPresencePreset(value: unknown): value is PresencePreset {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(PRESENCE_PRESETS, value);
}

function parseCapabilities(value: unknown, eventId: string, kind: string): ReadonlySet<Capability> {
  if (value === undefined) {
    return legacyCapabilities(eventId, kind);
  }
  if (!Array.isArray(value)) {
    throw new TypeError("context capabilities must be an array");
  }
  const result = new Set<Capability>();
  for (const item of value) {
    if (typeof item !== "string") {
      continue;
    }
    const normalized = WIRE_CAPABILITIES[item] ?? (CAPABILITIES.has(item as Capability) ? item as Capability : undefined);
    if (normalized) {
      result.add(normalized);
    }
  }
  return result;
}

function legacyCapabilities(eventId: string, kind: string): ReadonlySet<Capability> {
  if (!eventId || kind === "connector_health") {
    return new Set<Capability>();
  }
  if (kind === "permission_request") {
    return new Set<Capability>(["allow", "deny"]);
  }
  return new Set<Capability>(["open", "snooze", "acknowledge"]);
}

function parsePresence(value: unknown): PresenceSnapshot {
  const presence = objectValue(value, "presence");
  const state = boundedString(presence.state, 32) as PresenceState;
  if (!PRESENCE_STATES.has(state)) {
    throw new TypeError("presence state is invalid");
  }
  if (
    typeof presence.busy !== "boolean" ||
    typeof presence.calendar_known !== "boolean" ||
    typeof presence.matter_output_enabled !== "boolean"
  ) {
    throw new TypeError("presence booleans are invalid");
  }
  if (!Array.isArray(presence.allowed_states)) {
    throw new TypeError("presence allowed_states must be an array");
  }
  const allowedStates = presence.allowed_states.filter(
    (item): item is MutablePresenceState => typeof item === "string" && MUTABLE_STATES.has(item as MutablePresenceState)
  );
  return {
    state,
    busy: presence.busy,
    calendarKnown: presence.calendar_known,
    matterOutputEnabled: presence.matter_output_enabled,
    activeLeaseCount: integerInRange(presence.active_lease_count, 0, 10000, "active_lease_count"),
    nextExpiryAt: boundedString(presence.next_expiry_at, 64),
    allowedStates,
    minTtlSeconds: integerInRange(presence.min_ttl_seconds, 1, 86400, "min_ttl_seconds"),
    maxTtlSeconds: integerInRange(presence.max_ttl_seconds, 1, 604800, "max_ttl_seconds")
  };
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function boundedString(value: unknown, maximum: number): string {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value !== "string" || value.length > maximum) {
    throw new TypeError("bridge string field is invalid");
  }
  return value.trim();
}

function integerInRange(value: unknown, minimum: number, maximum: number, label: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}
