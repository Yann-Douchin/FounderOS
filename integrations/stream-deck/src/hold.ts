export type HoldResult = "none" | "cancelled" | "committed";
type TimerHandle = ReturnType<typeof setTimeout>;
type SetTimer = (callback: () => void, milliseconds: number) => TimerHandle;
type ClearTimer = (handle: TimerHandle) => void;

interface PendingHold {
  timer: TimerHandle;
  committed: boolean;
}

export class HoldGate {
  private readonly pending = new Map<string, PendingHold>();

  constructor(
    private readonly milliseconds: number,
    private readonly setTimer: SetTimer = setTimeout,
    private readonly clearTimer: ClearTimer = clearTimeout
  ) {
    if (!Number.isInteger(milliseconds) || milliseconds < 1000) {
      throw new RangeError("hold duration must be at least 1000 ms");
    }
  }

  begin(id: string, commit: () => void): boolean {
    if (!id || this.pending.has(id)) {
      return false;
    }
    const entry: PendingHold = {
      committed: false,
      timer: this.setTimer(() => {
        entry.committed = true;
        commit();
      }, this.milliseconds)
    };
    this.pending.set(id, entry);
    return true;
  }

  release(id: string): HoldResult {
    const entry = this.pending.get(id);
    if (!entry) {
      return "none";
    }
    this.clearTimer(entry.timer);
    this.pending.delete(id);
    return entry.committed ? "committed" : "cancelled";
  }

  cancel(id: string): void {
    this.release(id);
  }

  cancelAll(): void {
    for (const id of [...this.pending.keys()]) {
      this.cancel(id);
    }
  }
}
