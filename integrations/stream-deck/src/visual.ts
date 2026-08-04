import type { ConnectionState, PluginSnapshot } from "./coordinator.js";
import type { PresenceState } from "./domain.js";

export type Tone = "blue" | "green" | "amber" | "red" | "gray" | "purple";

const COLORS: Record<Tone, { background: string; foreground: string; accent: string }> = {
  blue: { background: "#10243D", foreground: "#FFFFFF", accent: "#4EA1FF" },
  green: { background: "#0D2F25", foreground: "#FFFFFF", accent: "#39D98A" },
  amber: { background: "#3B2B0D", foreground: "#FFFFFF", accent: "#FFBD3E" },
  red: { background: "#3D1518", foreground: "#FFFFFF", accent: "#FF5A64" },
  gray: { background: "#20242B", foreground: "#ADB5C2", accent: "#626A76" },
  purple: { background: "#25173D", foreground: "#FFFFFF", accent: "#A678FF" }
};

export function keyImage(label: string, tone: Tone, progress = 1): string {
  const colors = COLORS[tone];
  const safeLabel = escapeXml(label.slice(0, 6));
  const width = Math.max(0, Math.min(1, progress)) * 112;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">
<rect width="144" height="144" rx="22" fill="${colors.background}"/>
<rect x="16" y="18" width="112" height="8" rx="4" fill="#FFFFFF" opacity="0.12"/>
<rect x="16" y="18" width="${width.toFixed(1)}" height="8" rx="4" fill="${colors.accent}"/>
<circle cx="72" cy="70" r="35" fill="none" stroke="${colors.accent}" stroke-width="7"/>
<text x="72" y="78" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="${safeLabel.length > 4 ? 20 : 26}" font-weight="700" fill="${colors.foreground}">${safeLabel}</text>
</svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg, "utf8").toString("base64")}`;
}

export function statusPresentation(snapshot: PluginSnapshot): { title: string; label: string; tone: Tone } {
  if (snapshot.connection !== "online") {
    return connectionPresentation(snapshot.connection);
  }
  const context = snapshot.context;
  if (!context) {
    return { title: "FounderOS\nOffline", label: "FOS", tone: "red" };
  }
  if (context.kind === "permission_request") {
    return { title: "Approval\npending", label: "AUTH", tone: "amber" };
  }
  if (context.eventId) {
    return {
      title: kindLabel(context.kind),
      label: "FOS",
      tone: context.kind === "blocked" ? "red" : context.kind === "validation" ? "amber" : "blue"
    };
  }
  if (context.presence && context.presence.state !== "available") {
    return presencePresentation(context.presence.state);
  }
  return { title: "FounderOS\nReady", label: "FOS", tone: "green" };
}

function connectionPresentation(connection: ConnectionState): { title: string; label: string; tone: Tone } {
  switch (connection) {
    case "checking":
      return { title: "FounderOS\nConnecting", label: "...", tone: "gray" };
    case "not_configured":
      return { title: "Setup\nneeded", label: "KEY", tone: "amber" };
    case "unauthorized":
      return { title: "Access\ndenied", label: "KEY", tone: "red" };
    default:
      return { title: "FounderOS\nOffline", label: "FOS", tone: "red" };
  }
}

function presencePresentation(state: PresenceState): { title: string; label: string; tone: Tone } {
  switch (state) {
    case "focus":
      return { title: "Focus\nmode", label: "50", tone: "purple" };
    case "manual_call":
      return { title: "Call\nactive", label: "CALL", tone: "red" };
    case "meeting":
      return { title: "Meeting\nactive", label: "MEET", tone: "red" };
    case "recording":
      return { title: "Recording\nactive", label: "REC", tone: "red" };
    default:
      return { title: "FounderOS\nReady", label: "FOS", tone: "green" };
  }
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    waiting: "Waiting",
    blocked: "Blocked",
    decision: "Decision",
    meeting: "Meeting",
    validation: "Validation",
    success: "Success",
    connector_health: "Connector"
  };
  return labels[kind] ?? "Active\npriority";
}

function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    const replacements: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&apos;"
    };
    return replacements[character] ?? "";
  });
}
