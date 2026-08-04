import { BridgeError, type BridgeClient, type CommandResult } from "./protocol.js";
import type {
  BridgeContext,
  EventCapability,
  PresencePreset
} from "./domain.js";

export type ConnectionState = "checking" | "online" | "offline" | "not_configured" | "unauthorized";

export interface PluginSnapshot {
  connection: ConnectionState;
  context: BridgeContext | null;
  updatedAt: number;
}

type Subscriber = (snapshot: PluginSnapshot) => void | Promise<void>;
type ErrorReporter = (message: string) => void;

export class FounderOSCoordinator {
  private readonly subscribers = new Set<Subscriber>();
  private snapshot: PluginSnapshot = {
    connection: "checking",
    context: null,
    updatedAt: 0
  };
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private refreshPromise: Promise<PluginSnapshot> | undefined;

  constructor(
    private readonly bridge: BridgeClient,
    private readonly pollMilliseconds = 2000,
    private readonly reportError: ErrorReporter = () => undefined
  ) {}

  get current(): PluginSnapshot {
    return this.snapshot;
  }

  subscribe(subscriber: Subscriber): () => void {
    this.subscribers.add(subscriber);
    void subscriber(this.snapshot);
    if (!this.pollTimer) {
      this.pollTimer = setInterval(() => void this.refresh(), this.pollMilliseconds);
      this.pollTimer.unref?.();
      void this.refresh();
    }
    return () => {
      this.subscribers.delete(subscriber);
      if (this.subscribers.size === 0 && this.pollTimer) {
        clearInterval(this.pollTimer);
        this.pollTimer = undefined;
      }
    };
  }

  async refresh(): Promise<PluginSnapshot> {
    if (this.refreshPromise) {
      return this.refreshPromise;
    }
    this.refreshPromise = this.performRefresh();
    try {
      return await this.refreshPromise;
    } finally {
      this.refreshPromise = undefined;
    }
  }

  async executeEvent(action: EventCapability): Promise<CommandResult> {
    try {
      const result = await this.bridge.sendEventAction(action);
      await this.refresh();
      return result;
    } catch (error) {
      await this.handleError(error);
      throw error;
    }
  }

  async executePresence(preset: PresencePreset): Promise<CommandResult> {
    try {
      const result = await this.bridge.sendPresencePreset(preset);
      await this.refresh();
      return result;
    } catch (error) {
      await this.handleError(error);
      throw error;
    }
  }

  close(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = undefined;
    }
    this.subscribers.clear();
  }

  private async performRefresh(): Promise<PluginSnapshot> {
    try {
      const context = await this.bridge.getContext();
      this.snapshot = {
        connection: "online",
        context,
        updatedAt: Date.now()
      };
    } catch (error) {
      this.snapshot = {
        connection: connectionForError(error),
        context: null,
        updatedAt: Date.now()
      };
      this.reportError(safeErrorMessage(error));
    }
    await this.publish();
    return this.snapshot;
  }

  private async handleError(error: unknown): Promise<void> {
    this.snapshot = {
      connection: connectionForError(error),
      context: null,
      updatedAt: Date.now()
    };
    this.reportError(safeErrorMessage(error));
    await this.publish();
  }

  private async publish(): Promise<void> {
    const deliveries = [...this.subscribers].map(async (subscriber) => {
      try {
        await subscriber(this.snapshot);
      } catch {
        this.reportError("Could not update a Stream Deck action");
      }
    });
    await Promise.all(deliveries);
  }
}

function connectionForError(error: unknown): ConnectionState {
  if (!(error instanceof BridgeError)) {
    return "offline";
  }
  if (error.code === "not_configured") {
    return "not_configured";
  }
  if (error.code === "unauthorized") {
    return "unauthorized";
  }
  return "offline";
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof BridgeError) {
    return `FounderOS bridge: ${error.code}`;
  }
  return "FounderOS bridge: unexpected local error";
}
