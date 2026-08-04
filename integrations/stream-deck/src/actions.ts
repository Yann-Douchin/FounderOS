import streamDeck, {
  action,
  type DidReceiveSettingsEvent,
  type KeyAction,
  type KeyDownEvent,
  type KeyUpEvent,
  SingletonAction,
  type WillAppearEvent,
  type WillDisappearEvent
} from "@elgato/streamdeck";

import type { PluginSnapshot } from "./coordinator.js";
import { FounderOSCoordinator } from "./coordinator.js";
import {
  ACTION_UUIDS,
  capabilityForCockpitSlot,
  HOLD_MILLISECONDS,
  PRESENCE_PRESETS,
  isPresencePreset,
  type EventCapability,
  type PresencePreset
} from "./domain.js";
import { HoldGate } from "./hold.js";
import { keyImage, statusPresentation, type Tone } from "./visual.js";

interface PresenceSettings {
  schemaVersion?: unknown;
  preset?: unknown;
}

abstract class VisibleAction extends SingletonAction {
  private readonly unsubscribe = new Map<string, () => void>();

  constructor(protected readonly coordinator: FounderOSCoordinator) {
    super();
  }

  override async onWillAppear(ev: WillAppearEvent): Promise<void> {
    if (!ev.action.isKey()) {
      return;
    }
    this.unsubscribe.get(ev.action.id)?.();
    const update = (snapshot: PluginSnapshot) => this.render(ev.action as KeyAction, snapshot, ev.payload.settings);
    this.unsubscribe.set(ev.action.id, this.coordinator.subscribe(update));
  }

  override onWillDisappear(ev: WillDisappearEvent): void {
    this.unsubscribe.get(ev.action.id)?.();
    this.unsubscribe.delete(ev.action.id);
  }

  protected abstract render(action: KeyAction, snapshot: PluginSnapshot, settings: unknown): Promise<void>;
}

@action({ UUID: ACTION_UUIDS.status })
export class StatusAction extends VisibleAction {
  override async onKeyDown(): Promise<void> {
    await this.coordinator.refresh();
  }

  protected override async render(action: KeyAction, snapshot: PluginSnapshot): Promise<void> {
    const presentation = statusPresentation(snapshot);
    await Promise.all([
      action.setTitle(presentation.title),
      action.setImage(keyImage(presentation.label, presentation.tone))
    ]);
  }
}

abstract class EventAction extends VisibleAction {
  private readonly holds = new HoldGate(HOLD_MILLISECONDS);

  constructor(
    coordinator: FounderOSCoordinator,
    private readonly definition: EventDefinition,
    private readonly requiresHold = false
  ) {
    super(coordinator);
  }

  override async onKeyDown(ev: KeyDownEvent): Promise<void> {
    const definition = this.selectedDefinition();
    if (!this.isAvailable(definition.capability)) {
      await ev.action.showAlert();
      return;
    }
    if (!this.requiresHold) {
      await this.execute(ev.action, definition.capability);
      return;
    }
    if (this.holds.begin(ev.action.id, () => void this.execute(ev.action, definition.capability))) {
      await Promise.all([
        ev.action.setTitle("Hold..."),
        ev.action.setImage(keyImage(definition.label, "amber", 0.35))
      ]);
    }
  }

  override async onKeyUp(ev: KeyUpEvent): Promise<void> {
    if (!this.requiresHold) {
      return;
    }
    this.holds.release(ev.action.id);
    await this.render(ev.action, this.coordinator.current);
  }

  override onWillDisappear(ev: WillDisappearEvent): void {
    this.holds.cancel(ev.action.id);
    super.onWillDisappear(ev);
  }

  protected override async render(action: KeyAction, snapshot: PluginSnapshot): Promise<void> {
    const definition = this.selectedDefinition(snapshot);
    const available =
      snapshot.connection === "online" && Boolean(snapshot.context?.capabilities.has(definition.capability));
    await Promise.all([
      action.setTitle(available ? definition.title : `${definition.title}\nunavailable`),
      action.setImage(keyImage(definition.label, available ? definition.tone : "gray"))
    ]);
  }

  protected selectedDefinition(snapshot = this.coordinator.current): EventDefinition {
    return this.definition;
  }

  private isAvailable(capability: EventCapability): boolean {
    const snapshot = this.coordinator.current;
    return snapshot.connection === "online" && Boolean(snapshot.context?.capabilities.has(capability));
  }

  private async execute(actionInstance: KeyAction, capability: EventCapability): Promise<void> {
    try {
      await this.coordinator.executeEvent(capability);
      await actionInstance.showOk();
    } catch {
      streamDeck.logger.warn(`FounderOS action failed: ${capability}`);
      await actionInstance.showAlert();
    }
  }
}

@action({ UUID: ACTION_UUIDS.open })
export class OpenAction extends EventAction {
  constructor(coordinator: FounderOSCoordinator) {
    super(coordinator, { capability: "open", title: "Open", label: "OPEN", tone: "blue" });
  }
}

@action({ UUID: ACTION_UUIDS.snooze })
export class SnoozeAction extends EventAction {
  constructor(coordinator: FounderOSCoordinator) {
    super(coordinator, { capability: "snooze", title: "Snooze\n15 min", label: "15", tone: "purple" });
  }

  protected override selectedDefinition(snapshot = this.coordinator.current): EventDefinition {
    const capability = capabilityForCockpitSlot(
      "snooze",
      snapshot.context?.capabilities ?? new Set()
    );
    return capability === "deny"
      ? { capability: "deny", title: "Deny", label: "NO", tone: "red" }
      : { capability: "snooze", title: "Snooze\n15 min", label: "15", tone: "purple" };
  }
}

@action({ UUID: ACTION_UUIDS.acknowledge })
export class AcknowledgeAction extends EventAction {
  constructor(coordinator: FounderOSCoordinator) {
    super(
      coordinator,
      { capability: "acknowledge", title: "Hold\nAcknowledge", label: "OK", tone: "green" },
      true
    );
  }

  protected override selectedDefinition(snapshot = this.coordinator.current): EventDefinition {
    const capability = capabilityForCockpitSlot(
      "acknowledge",
      snapshot.context?.capabilities ?? new Set()
    );
    return capability === "allow"
      ? { capability: "allow", title: "Hold\nAllow", label: "YES", tone: "green" }
      : { capability: "acknowledge", title: "Hold\nAcknowledge", label: "OK", tone: "green" };
  }
}

@action({ UUID: ACTION_UUIDS.allow })
export class AllowAction extends EventAction {
  constructor(coordinator: FounderOSCoordinator) {
    super(
      coordinator,
      { capability: "allow", title: "Hold\nAllow", label: "YES", tone: "green" },
      true
    );
  }
}

@action({ UUID: ACTION_UUIDS.deny })
export class DenyAction extends EventAction {
  constructor(coordinator: FounderOSCoordinator) {
    super(coordinator, { capability: "deny", title: "Deny", label: "NO", tone: "red" });
  }
}

interface EventDefinition {
  capability: EventCapability;
  title: string;
  label: string;
  tone: Tone;
}

@action({ UUID: ACTION_UUIDS.presence })
export class PresenceAction extends VisibleAction {
  override async onKeyDown(ev: KeyDownEvent): Promise<void> {
    const preset = presetFromSettings(ev.payload.settings);
    const definition = PRESENCE_PRESETS[preset];
    const snapshot = this.coordinator.current;
    if (
      snapshot.connection !== "online" ||
      !snapshot.context?.capabilities.has(definition.capability)
    ) {
      await ev.action.showAlert();
      return;
    }
    try {
      await this.coordinator.executePresence(preset);
      await ev.action.showOk();
    } catch {
      streamDeck.logger.warn(`FounderOS presence action failed: ${preset}`);
      await ev.action.showAlert();
    }
  }

  override async onDidReceiveSettings(ev: DidReceiveSettingsEvent): Promise<void> {
    if (!ev.action.isKey()) {
      return;
    }
    await this.render(ev.action, this.coordinator.current, ev.payload.settings);
  }

  protected override async render(action: KeyAction, snapshot: PluginSnapshot, settings: unknown): Promise<void> {
    const preset = presetFromSettings(settings);
    const definition = PRESENCE_PRESETS[preset];
    const available =
      snapshot.connection === "online" &&
      Boolean(snapshot.context?.capabilities.has(definition.capability));
    const tone = toneForPresencePreset(preset, available);
    await Promise.all([
      action.setTitle(available ? definition.title : `${definition.title}\nunavailable`),
      action.setImage(keyImage(definition.shortLabel, tone))
    ]);
  }
}

export function presetFromSettings(settings: unknown): PresencePreset {
  if (typeof settings !== "object" || settings === null || Array.isArray(settings)) {
    return "focus50";
  }
  const value = settings as PresenceSettings;
  return value.schemaVersion === 1 && isPresencePreset(value.preset) ? value.preset : "focus50";
}

function toneForPresencePreset(preset: PresencePreset, available: boolean): Tone {
  if (!available) {
    return "gray";
  }
  if (preset === "recordingStart" || preset === "manualCallStart") {
    return "red";
  }
  if (preset === "focus50") {
    return "purple";
  }
  if (preset === "recordingRenew") {
    return "amber";
  }
  return "green";
}
