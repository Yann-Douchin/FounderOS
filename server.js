"use strict";
/*
 * BUSY Bar emulator: mock HTTP API + live display server.
 *
 * Routes match the real firmware (busybar-firmware/web_server): clear is
 * DELETE /api/display/draw, brightness is a single ?value=, uploads are raw
 * octet-stream with ?file=, status is nested, busy uses the real snapshot
 * envelope, and draws carry a 1-100 priority (409 on too-low). Auth mirrors the
 * device: only enforced for non-localhost callers when BUSY_API_TOKEN is set.
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");
const { spawn, spawnSync } = require("child_process");
const { renderScreen } = require("./screen_renderer");

const PORT = process.env.PORT ? Number(process.env.PORT) : 8080;
const HOST = process.env.BUSY_HOST || "127.0.0.1";
const TOKEN = process.env.BUSY_API_TOKEN || null;
const PYTHON = process.env.BUSY_PYTHON || "python3";
const APPS_DIR = path.join(__dirname, "apps");
const PUBLIC = path.join(__dirname, "public");
const DIST = path.join(__dirname, "web", "dist");   // built Vue app
const ANIM_DIR = path.join(PUBLIC, "animations");
const SOUNDS_DIR = path.join(PUBLIC, "sounds");
const API_SEMVER = "25.0.0";
const STATUS_ENDPOINTS = new Set([
  "/api/status/device",
  "/api/status/firmware",
  "/api/status/system",
  "/api/status/power",
]);

/* --------------------------- animation manifest -------------------------- */
function scanAnimations() {
  const out = {};
  let dirs = [];
  try { dirs = fs.readdirSync(ANIM_DIR, { withFileTypes: true }).filter((d) => d.isDirectory()); } catch (_) { return out; }
  for (const d of dirs) {
    const dir = path.join(ANIM_DIR, d.name);
    let meta = { fps: 30, color_mode: "rgb888", sections: [] };
    try { Object.assign(meta, JSON.parse(fs.readFileSync(path.join(dir, "meta.json"), "utf8"))); } catch (_) {}
    // Detect frame naming: <prefix><number>.png (frame_0.png OR coding_00000.png)
    let prefix = "frame_", pad = 0, start = 0, frames = 0;
    try {
      const nums = [];
      for (const f of fs.readdirSync(dir)) { const mm = f.match(/^(.*?)(\d+)\.png$/i); if (mm) nums.push({ p: mm[1], n: parseInt(mm[2], 10), w: mm[2].length }); }
      if (nums.length) {
        prefix = nums[0].p; frames = nums.length; start = nums.reduce((a, x) => Math.min(a, x.n), Infinity);
        pad = new Set(nums.map((x) => x.w)).size === 1 ? nums[0].w : 0;
      }
    } catch (_) {}
    const m = d.name.match(/(\d+)x(\d+)$/);
    out[d.name] = { name: d.name, fps: meta.fps || 30, frames, prefix, pad, start,
      color_mode: meta.color_mode || "rgb888", sections: Array.isArray(meta.sections) ? meta.sections : [],
      w: m ? +m[1] : 72, h: m ? +m[2] : 16 };
  }
  return out;
}
const ANIMATIONS = scanAnimations();

/* ---------------------------- sounds manifest ---------------------------- */
function scanSounds() {
  const out = {};
  let files = [];
  try { files = fs.readdirSync(SOUNDS_DIR); } catch (_) { return out; }
  for (const f of files) { if (/\.(wav|mp3|ogg)$/i.test(f)) out[path.basename(f, path.extname(f))] = f; }
  return out;
}
const SOUNDS = scanSounds();

/* --------------------------------- apps ---------------------------------- */
const APP_PARAMS = {
  "busy_status.py": [{ key: "theme", label: "Theme", type: "select", positional: true, default: "on_air",
    options: ["keep_out","dnd","meeting","on_call","lunch","back_soon","booked","flow","chill_time","on_air","coding","low_social_battery"] }],
  "ping_monitor.py": [{ key: "target", label: "Target", type: "text", flag: "--target", placeholder: "8.8.8.8" }],
  "pixel_fire.py": [{ key: "effect", label: "Effect", type: "select", positional: true, default: "fire",
    options: ["fire", "rain", "plasma"] }],
  "sound_test.py": [{ key: "sound", label: "Sound", type: "select", positional: true, default: "all",
    options: ["all", ...Object.keys(SOUNDS).sort()] }],
};

// Auto-discover an argparse app's options by parsing its own `--help` output,
// so the Apps tab can render inputs without a hand-written APP_PARAMS entry.
// Only runs for scripts that mention argparse (others might loop on --help),
// cached per file mtime; the runner passes --host itself so it is skipped.
const ARG_SKIP = new Set(["-h", "--help", "--host", "--test"]);
const argCache = {};
function argparseParams(fullPath) {
  let mtime;
  try { mtime = fs.statSync(fullPath).mtimeMs; } catch (_) { return []; }
  const hit = argCache[fullPath];
  if (hit && hit.mtime === mtime) return hit.params;
  let params = [];
  try {
    if (fs.readFileSync(fullPath, "utf8").includes("argparse")) {
      const r = spawnSync(PYTHON, [fullPath, "--help"], { timeout: 3000, encoding: "utf8" });
      if (r.status === 0 && r.stdout) params = parseHelp(r.stdout);
    }
  } catch (_) {}
  argCache[fullPath] = { mtime, params };
  return params;
}
function parseHelp(help) {
  const params = [];
  // option entries look like "  --theme {a,b,c}  help..." or "  --user USER  help..."
  // or "  --test  help..."; continuation lines are indented further.
  const re = /^[ ]{2}(--[\w-]+)(?:[ =](\{[^}]*\}|[A-Z][\w-]*))?(?:[ \t]{2,}(\S.*))?$/gm;
  let m;
  while ((m = re.exec(help)) !== null) {
    const [, flag, meta, rest] = m;
    if (ARG_SKIP.has(flag)) continue;
    const key = flag.replace(/^--/, "");
    const label = key.charAt(0).toUpperCase() + key.slice(1).replace(/-/g, " ");
    // find the help text: trailing same-line text or the indented next line
    let hint = rest || "";
    if (!hint) {
      const after = help.slice(m.index + m[0].length);
      const cont = after.match(/^\n\s{10,}(\S.*)/);
      if (cont) hint = cont[1];
    }
    const def = (hint.match(/\(default:\s*([^)]+)\)/) || [])[1];
    if (meta && meta.startsWith("{")) {
      const options = meta.slice(1, -1).split(",").map((s) => s.trim()).filter(Boolean);
      params.push({ key, label, type: "select", flag, options, default: def || options[0], help: hint });
    } else if (!meta) {
      params.push({ key, label, type: "check", flag, help: hint });
    } else {
      params.push({ key, label, type: "text", flag, placeholder: def || "", help: hint });
    }
  }
  return params;
}

function scanApps() {
  const isApp = (f) => f.endsWith(".py") && f !== "busybar.py" && !f.startsWith("_");
  const describe = (fullPath, fallback) => {
    try {
      const head = fs.readFileSync(fullPath, "utf8").slice(0, 2048);
      const m = head.match(/"""[\s\n]*([^\n"]+)/);
      if (m) return m[1].trim();
    } catch (_) {}
    return fallback;
  };
  // rel is the path under apps/ used to spawn the script; slug (its basename or
  // folder name) is the display name and the APP_PARAMS key.
  const make = (rel, slug, script, prefix) => {
    const full = path.join(APPS_DIR, rel);
    const entry = { name: prefix ? `${prefix}/${slug}` : slug, file: rel,
      description: describe(full, slug), params: APP_PARAMS[script] || argparseParams(full) };
    if (prefix) entry.local = true;
    return entry;
  };
  const scan = (dir, prefix = "") => {
    let ents = [];
    try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { return []; }
    const out = [];
    for (const d of ents) {
      const rel = prefix ? `${prefix}/${d.name}` : d.name;
      // Flat single-file app: apps/local/foo.py
      if (d.isFile() && isApp(d.name)) { out.push(make(rel, d.name.replace(".py", ""), d.name, prefix)); continue; }
      // Foldered app (local only): apps/local/<slug>/<slug>.py, else app.py, else the lone .py
      if (prefix && d.isDirectory() && !d.name.startsWith("_") && !d.name.startsWith(".")) {
        let subFiles = [];
        try { subFiles = fs.readdirSync(path.join(dir, d.name)).filter(isApp); } catch (_) {}
        const script = subFiles.includes(`${d.name}.py`) ? `${d.name}.py`
          : subFiles.includes("app.py") ? "app.py"
          : subFiles.length === 1 ? subFiles[0] : null;
        if (script) out.push(make(`${rel}/${script}`, d.name, script, prefix));
      }
    }
    return out;
  };
  return scan(APPS_DIR).concat(scan(path.join(APPS_DIR, "local"), "local"));
}

let appProc = null;  // { child, name, pid, startedAt, exitCode, error, output, buf }
let appOpChain = Promise.resolve();
let appBcastTimer = null;

function appStatus() {
  if (!appProc) return { running: false, name: null, pid: null, startedAt: null, exitCode: null, error: null, output: [] };
  return { running: appProc.exitCode === undefined && !appProc.error, name: appProc.name, pid: appProc.pid || null, startedAt: appProc.startedAt, exitCode: appProc.exitCode !== undefined ? appProc.exitCode : null, error: appProc.error || null, output: appProc.output };
}

// rec-scoped so a late exit from a replaced child can't touch the current app's state
function pushLine(rec, s, line) {
  if (line.length > 300) line = line.slice(0, 300) + "…";
  rec.output.push({ t: Date.now(), s, line });
  if (rec.output.length > 50) rec.output.shift();
  if (rec !== appProc || appBcastTimer) return;
  appBcastTimer = setTimeout(() => { appBcastTimer = null; broadcast(); }, 50);
}

// Wire stdout/stderr of a child process into rec's line buffers via pushLine.
function wireStreams(child, rec) {
  function lineBuffer(stream, s) {
    child[stream].on("data", (chunk) => {
      rec.buf[s] += chunk.toString("utf8");
      let nl;
      while ((nl = rec.buf[s].indexOf("\n")) !== -1) {
        pushLine(rec, s, rec.buf[s].slice(0, nl));
        rec.buf[s] = rec.buf[s].slice(nl + 1);
      }
    });
  }
  lineBuffer("stdout", "out");
  lineBuffer("stderr", "err");
}

// Run a setup child (venv create or pip install) with its streams wired into rec.
// Resolves on exit code 0, rejects with "venv setup failed (exit N)" otherwise.
// The child is assigned to rec.child while running so stopApp() can kill it.
function runSetup(rec, cmd, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      env: Object.assign({}, process.env, { PYTHONUNBUFFERED: "1" }),
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    rec.child = child;
    wireStreams(child, rec);
    child.on("error", (err) => { rec.child = null; reject(new Error(`venv setup failed (${err.message})`)); });
    child.on("exit", (code) => {
      // flush trailing partial lines from this setup child's streams
      if (rec.buf.out) { pushLine(rec, "out", rec.buf.out); rec.buf.out = ""; }
      if (rec.buf.err) { pushLine(rec, "err", rec.buf.err); rec.buf.err = ""; }
      rec.child = null;
      const n = code !== null ? code : -1;
      if (n !== 0) reject(new Error(`venv setup failed (exit ${n})`));
      else resolve();
    });
  });
}

async function startApp(entry, userArgs) {
  const rec = { child: null, name: entry.name, pid: null, startedAt: Date.now(), exitCode: undefined, error: null, output: [], buf: { out: "", err: "" } };
  appProc = rec;
  broadcast();

  // Determine whether this is a foldered app that needs a venv.
  const fileDir = path.dirname(entry.file);
  const isFoldered = fileDir !== "." && fileDir !== "local";
  const folder = path.join(APPS_DIR, fileDir);
  const reqFile = path.join(folder, "requirements.txt");
  let pyBin = PYTHON;

  if (isFoldered && fs.existsSync(reqFile)) {
    const venvDir = path.join(folder, ".venv");
    const pyBinPath = process.platform === "win32"
      ? path.join(venvDir, "Scripts", "python.exe")
      : path.join(venvDir, "bin", "python3");
    const stamp = path.join(venvDir, ".req-sha");
    const sha = crypto.createHash("sha256").update(fs.readFileSync(reqFile)).digest("hex");

    let needSetup = true;
    if (fs.existsSync(pyBinPath)) {
      try { needSetup = fs.readFileSync(stamp, "utf8").trim() !== sha; } catch (_) {}
    }

    if (needSetup) {
      try {
        if (!fs.existsSync(pyBinPath)) {
          pushLine(rec, "out", "[setup] creating .venv …");
          await runSetup(rec, PYTHON, ["-m", "venv", venvDir]);
        }
        pushLine(rec, "out", "[setup] pip install -r requirements.txt …");
        await runSetup(rec, pyBinPath, ["-m", "pip", "install", "-r", reqFile]);
        fs.writeFileSync(stamp, sha, "utf8");
        pushLine(rec, "out", "[setup] done");
      } catch (e) {
        rec.error = e.message;
        broadcast();
        throw e;
      }
    }
    pyBin = pyBinPath;
  }

  return new Promise((resolve, reject) => {
    const child = spawn(pyBin, [path.join(APPS_DIR, entry.file), "--host", `127.0.0.1:${PORT}`, ...userArgs], {
      env: Object.assign({}, process.env, { PYTHONUNBUFFERED: "1" }),
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    rec.child = child;

    let settled = false;
    function settle(err) { if (settled) return; settled = true; if (err) reject(err); else resolve(rec.pid); }

    child.on("spawn", () => { rec.pid = child.pid; settle(null); if (rec === appProc) broadcast(); });
    child.on("error", (err) => {
      rec.error = err.message;
      settle(err);
      if (rec === appProc) broadcast();
    });
    wireStreams(child, rec);
    child.on("exit", (code) => {
      if (rec.buf.out) { pushLine(rec, "out", rec.buf.out); rec.buf.out = ""; }
      if (rec.buf.err) { pushLine(rec, "err", rec.buf.err); rec.buf.err = ""; }
      rec.exitCode = code !== null ? code : -1;
      pushLine(rec, "out", `[process exited: ${rec.exitCode}]`);
      if (rec === appProc) broadcast();
    });
  });
}

function stopApp() {
  return new Promise((resolve) => {
    if (!appProc || !appProc.child || appProc.exitCode !== undefined || appProc.error) { resolve(false); return; }
    const child = appProc.child;
    let done = false;
    child.once("exit", () => { if (!done) { done = true; resolve(true); } });
    try { process.kill(-child.pid, "SIGTERM"); } catch (_) { child.kill("SIGTERM"); }
    setTimeout(() => { if (!done) { done = true; try { process.kill(-child.pid, "SIGKILL"); } catch (_) { try { child.kill("SIGKILL"); } catch (_2) {} } resolve(true); } }, 1500);
  });
}

/* ---------------------------- persistence -------------------------------- */
function defaultDataDir() {
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "FounderOS", "emulator");
  if (process.platform === "win32") {
    const local = process.env.LOCALAPPDATA;
    return path.join(local && path.isAbsolute(local) ? local : os.homedir(), "FounderOS", "emulator");
  }
  const xdg = process.env.XDG_STATE_HOME;
  const stateRoot = xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".local", "state");
  return path.join(stateRoot, "founderos", "emulator");
}
function configuredDataDir() {
  const configured = process.env.BUSY_DATA_DIR;
  if (configured && !path.isAbsolute(configured)) throw new Error("BUSY_DATA_DIR must be absolute");
  return path.resolve(configured || defaultDataDir());
}
const DATA_DIR = configuredDataDir();
const STATE_FILE = path.join(DATA_DIR, "state.json");
let _saveTimer = null;
function saveState() {
  if (_saveTimer) return;
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    const stor = {}; for (const [k, v] of Object.entries(state.storage)) stor[k] = { type: v.type, b64: v.data ? v.data.toString("base64") : null };
    const ass = {}; for (const [k, v] of Object.entries(state.assets)) ass[k] = { b64: v.buf.toString("base64"), type: v.type };
    const device = {
      brightness: state.brightness,
      volume: state.volume,
      name: state.name,
      access: state.access,
      timezone: state.timezone,
      clock_offset_ms: state.clock_offset_ms,
      busy_snapshot: state.busy_snapshot,
      busy_profiles: state.busy_profiles,
      wifi: state.wifi,
      ble: state.ble,
      smart_home: state.smart_home,
      account: state.account,
      update: state.update,
    };
    const json = JSON.stringify({ storage: stor, assets: ass, device });
    try {
      if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
      const tmp = STATE_FILE + ".tmp";
      fs.writeFileSync(tmp, json, { encoding: "utf8", mode: 0o600 });
      fs.renameSync(tmp, STATE_FILE);
      fs.chmodSync(STATE_FILE, 0o600);
    } catch (e) { console.warn("[persist] save failed:", e.message); }
  }, 500);
}
function loadState(st) {
  try {
    if (!fs.existsSync(STATE_FILE)) return;
    const { storage, assets, device } = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    if (storage) for (const [k, v] of Object.entries(storage)) st.storage[k] = { type: v.type, data: v.b64 ? Buffer.from(v.b64, "base64") : null };
    if (assets) for (const [k, v] of Object.entries(assets)) st.assets[k] = { buf: Buffer.from(v.b64, "base64"), type: v.type };
    if (device && typeof device === "object") {
      for (const key of ["brightness", "volume", "name", "clock_offset_ms"]) {
        if (device[key] !== undefined) st[key] = device[key];
      }
      for (const key of ["access", "timezone", "busy_snapshot", "busy_profiles", "wifi", "ble", "smart_home", "account", "update"]) {
        if (device[key] && typeof device[key] === "object") st[key] = Object.assign({}, st[key], device[key]);
      }
    }
  } catch (e) { console.warn("[persist] could not load state.json, starting empty:", e.message); }
}

/* ------------------------------ device state ----------------------------- */
const BAR_SETTINGS = { theme: "busy", show_work_phase_only: false, trigger_smart_home: true };
const state = {
  frame: { application_name: null, elements: [], element_versions: {}, ts: 0, priority: 0 },
  brightness: 80,                 // number 0-100 or "auto"
  volume: 0,
  name: "BUSY-EMULATOR",
  access: { mode: TOKEN ? "key" : "disabled", key_valid: Boolean(TOKEN) },
  timezone: { name: "Europe/Madrid", offset: 7200, abbr: "CEST" },
  clock_offset_ms: 0,
  wifi: {
    state: "disconnected", ssid: "", bssid: "", channel: null, rssi: null,
    security: "WPA2", ip_config: null,
  },
  ble: { status: "disabled", address: "" },
  smart_home: {
    fabric_count: 0,
    latest_pairing_status: { value: "not_paired", timestamp: 0 },
    pairing: null,
    switch: { state: false, startup: "off" },
  },
  account: {
    linked: false, email: "", id: "", user_id: "", status: "disconnected",
    backend: { server_url: "default", client_cert_type: "default", ignore_server_cert: false },
    link: null,
  },
  update: {
    install: { is_allowed: true, action: "none", event: "none", status: "idle", detail: "", download: { received_bytes: 0, total_bytes: 0 } },
    check: { status: "idle", event: "none", available_version: "emulator-1.3.0" },
    autoupdate: { is_enabled: false, interval_start: "02:00", interval_end: "05:00" },
  },
  battery_charge: 100,
  startTime: Date.now(),
  busy_snapshot: { snapshot: { type: "NOT_STARTED", busy_bar_settings: Object.assign({}, BAR_SETTINGS) }, snapshot_timestamp_ms: Date.now() },
  busy_profiles: {
    busy:   { sort_order: 0, title: "Busy",   id: "profile-busy",   timer_settings: { type: "INFINITE" }, busy_bar_settings: Object.assign({}, BAR_SETTINGS), profile_timestamp_ms: Date.now() },
    custom: { sort_order: 1, title: "Custom", id: "profile-custom", timer_settings: { type: "SIMPLE", total_time_ms: 1200000 }, busy_bar_settings: { theme: "keep_out", show_work_phase_only: false, trigger_smart_home: true }, profile_timestamp_ms: Date.now() },
  },
  assets: {},
  storage: {},
  log: [],
};
loadState(state);
let frameSeq = 1;
let elementExpiryTimer = null;
const elementExpiries = new Map();
const elementStartedAt = new Map();

/* --------------------------- scenario simulator -------------------------- */
// Emulator-only fault injection. Ephemeral by design: never persisted.
const scenario = {
  offline_until: 0,
  power_state: "discharging",
  menu_open: false,
  physical_busy: false,
  smart_home_timer: false,
};
let offlineTimer = null, stealTimer = null;
const STEAL_APP = "_scenario.steal";
function scenarioInfo() {
  const owns = state.frame.application_name === STEAL_APP && state.frame.elements.length > 0;
  return {
    power_state: scenario.power_state,
    battery_charge: state.battery_charge,
    offline_until: scenario.offline_until,
    offline_remaining_ms: Math.max(0, scenario.offline_until - Date.now()),
    blockers: {
      menu_open: scenario.menu_open,
      physical_busy: scenario.physical_busy,
      smart_home_timer: scenario.smart_home_timer,
      api_busy: state.busy_snapshot.snapshot.type !== "NOT_STARTED",
    },
    steal: { active: owns, priority: owns ? state.frame.priority : null },
  };
}
// Priority-conflict rule shared by POST /api/display/draw and the steal scenario.
// Firmware (canvas_draw_rejected): the current owner may redraw at equal
// priority; a different app needs strictly higher priority to take over.
function elementKey(element, index) {
  return element && element.id != null ? `id:${String(element.id)}` : `anonymous:${index}`;
}
function elementExpiry(element, now) {
  const candidates = [];
  const timeout = Number(element && element.timeout);
  const displayUntil = Number(element && element.display_until);
  if (Number.isFinite(timeout) && timeout > 0) candidates.push(now + timeout * 1000);
  if (Number.isFinite(displayUntil) && displayUntil > 0) candidates.push(displayUntil * 1000);
  return candidates.length ? Math.min(...candidates) : null;
}
function clearFrame() {
  clearTimeout(elementExpiryTimer); elementExpiryTimer = null;
  elementExpiries.clear();
  elementStartedAt.clear();
  state.frame = { application_name: null, elements: [], element_versions: {}, ts: frameSeq++, priority: 0 };
}
function scheduleElementExpiry() {
  clearTimeout(elementExpiryTimer); elementExpiryTimer = null;
  if (!elementExpiries.size) return;
  const next = Math.min(...elementExpiries.values());
  elementExpiryTimer = setTimeout(() => {
    elementExpiryTimer = null;
    if (pruneExpiredElements()) broadcast();
  }, Math.max(1, next - Date.now()));
}
function pruneExpiredElements() {
  if (!state.frame.elements.length || !elementExpiries.size) return false;
  const now = Date.now();
  let changed = false;
  const kept = [];
  for (let index = 0; index < state.frame.elements.length; index++) {
    const element = state.frame.elements[index];
    const key = elementKey(element, index);
    const expiry = elementExpiries.get(key);
    if (expiry != null && expiry <= now) {
      elementExpiries.delete(key); elementStartedAt.delete(key); changed = true;
      if (state.frame.element_versions) delete state.frame.element_versions[key];
    } else kept.push(element);
  }
  if (changed) {
    if (kept.length) state.frame = Object.assign({}, state.frame, { elements: kept, ts: frameSeq++ });
    else clearFrame();
  }
  scheduleElementExpiry();
  return changed;
}
function canvasBlockReason() {
  if (scenario.physical_busy) return "physical BUSY session is active";
  if (scenario.menu_open) return "device menu is open";
  if (scenario.smart_home_timer) return "smart-home timer is active";
  if (state.busy_snapshot.snapshot.type !== "NOT_STARTED") return "BUSY session is active";
  return "";
}
function drawFrame(appName, elements, priority) {
  pruneExpiredElements();
  if (state.frame.elements.length) {
    const sameApp = appName === state.frame.application_name;
    if (sameApp ? priority < state.frame.priority : priority <= state.frame.priority) {
      return { ok: false, status: 409, reason: "Not drawn due to low priority" };
    }
  }
  const now = Date.now();
  const sameApp = state.frame.elements.length && appName === state.frame.application_name;
  if (sameApp) {
    const keys = new Set(state.frame.elements.map((element, index) => elementKey(element, index)));
    for (let index = 0; index < elements.length; index++) keys.add(elementKey(elements[index], index));
    if (keys.size > 100) return { ok: false, status: 400, reason: "Elements number limit exceeded" };
  }
  const drawRevision = frameSeq++;
  const elementVersions = sameApp ? Object.assign({}, state.frame.element_versions || {}) : {};
  let nextElements;
  if (sameApp) {
    nextElements = state.frame.elements.slice();
    const positions = new Map(nextElements.map((element, index) => [elementKey(element, index), index]));
    for (let index = 0; index < elements.length; index++) {
      const element = elements[index];
      const key = elementKey(element, index);
      if (positions.has(key)) nextElements[positions.get(key)] = element;
      else { positions.set(key, nextElements.length); nextElements.push(element); }
      elementStartedAt.set(key, now);
      elementVersions[key] = drawRevision;
      const expiry = elementExpiry(element, now);
      if (expiry == null) elementExpiries.delete(key); else elementExpiries.set(key, expiry);
    }
  } else {
    elementExpiries.clear();
    elementStartedAt.clear();
    nextElements = elements.slice();
    for (let index = 0; index < nextElements.length; index++) {
      const key = elementKey(nextElements[index], index);
      elementStartedAt.set(key, now);
      elementVersions[key] = drawRevision;
      const expiry = elementExpiry(nextElements[index], now);
      if (expiry != null) elementExpiries.set(elementKey(nextElements[index], index), expiry);
    }
  }
  if (nextElements.length > 100) return { ok: false, status: 400, reason: "Elements number limit exceeded" };
  state.frame = { application_name: appName, elements: nextElements, element_versions: elementVersions, ts: drawRevision, priority };
  scheduleElementExpiry();
  return { ok: true };
}

/* ------------------------------ SSE clients ------------------------------ */
const clients = new Set();
const statusSockets = new Set();
function uptimeStr(s) { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60; return `${String(d).padStart(2, "0")}d ${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s`; }
function snapshot() {
  pruneExpiredElements();
  return {
    frame: state.frame, brightness: state.brightness, volume: state.volume, name: state.name,
    battery_charge: state.battery_charge, uptime: Math.floor((Date.now() - state.startTime) / 1000),
    theme: state.busy_snapshot.snapshot.busy_bar_settings ? state.busy_snapshot.snapshot.busy_bar_settings.theme : null,
    log: state.log.slice(0, 18),
    app: appStatus(),
    scenario: scenarioInfo(),
  };
}
function websocketFrame(payload, opcode = 1) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(String(payload), "utf8");
  let header;
  if (body.length < 126) header = Buffer.from([0x80 | opcode, body.length]);
  else if (body.length <= 0xffff) { header = Buffer.alloc(4); header[0] = 0x80 | opcode; header[1] = 126; header.writeUInt16BE(body.length, 2); }
  else { header = Buffer.alloc(10); header[0] = 0x80 | opcode; header[1] = 127; header.writeBigUInt64BE(BigInt(body.length), 2); }
  return Buffer.concat([header, body]);
}
function statusSocketBroadcast() {
  const message = websocketFrame(JSON.stringify(snapshot()));
  for (const client of statusSockets) {
    if (!client.enabled || client.socket.destroyed) continue;
    try { client.socket.write(message); } catch (_) {}
  }
}
function broadcast() {
  const data = `event: state\ndata: ${JSON.stringify(snapshot())}\n\n`;
  for (const r of clients) { try { r.write(data); } catch (_) {} }
  statusSocketBroadcast();
}
function emit(ev, p) { const data = `event: ${ev}\ndata: ${JSON.stringify(p)}\n\n`; for (const r of clients) { try { r.write(data); } catch (_) {} } }
function logCall(method, p, note) { state.log.unshift({ t: Date.now(), method, path: p, note: note || "" }); if (state.log.length > 30) state.log.length = 30; }

/* ------------------------------- helpers -------------------------------- */
const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type, X-API-Token, X-API-Sem-Ver", "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS" };
function send(res, code, obj, headers) {
  const body = Buffer.isBuffer(obj) ? obj : Buffer.from(JSON.stringify(obj));
  res.writeHead(code, Object.assign({ "Content-Type": Buffer.isBuffer(obj) ? "application/octet-stream" : "application/json" }, CORS, headers || {}));
  res.end(body);
}
function sendScreen(res, pixels) {
  const body = Buffer.from(pixels.toString("base64"), "ascii");
  res.writeHead(200, Object.assign({ "Content-Type": "image/bmp", "Content-Length": body.length }, CORS));
  res.end(body);
}
function ok(res, extra) { send(res, 200, Object.assign({ result: "OK" }, extra || {})); }
function fail(res, code, msg) { send(res, code, { error: msg, code }); }
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0;
    req.on("data", (c) => { size += c.length; if (size > 8 * 1024 * 1024) { reject(new Error("payload too large")); req.destroy(); } chunks.push(c); });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}
async function readJson(req) { const b = await readBody(req); return b.length ? JSON.parse(b.toString("utf8")) : {}; }
function isLocal(req) { const a = req.socket.remoteAddress || ""; return a === "::1" || a.includes("127.0.0.1"); }
function authed(req) { if (!TOKEN) return true; if (isLocal(req)) return true; return req.headers["x-api-token"] === TOKEN; }
function storagePath(value, { allowRoot = false } = {}) {
  if (typeof value !== "string" || !value.startsWith("/")) return null;
  const normalized = path.posix.normalize(value);
  if (normalized !== "/ext" && !normalized.startsWith("/ext/")) return null;
  if (!allowRoot && normalized === "/ext") return null;
  return normalized;
}
function storageUsage() {
  const total = 16 * 1024 * 1024;
  const used = Object.values(state.storage).reduce((sum, item) => sum + (Buffer.isBuffer(item.data) ? item.data.length : 0), 0);
  return { used_bytes: used, free_bytes: Math.max(0, total - used), total_bytes: total };
}
function storageList(prefix) {
  const root = storagePath(prefix || "/ext", { allowRoot: true });
  if (!root) return null;
  const base = root === "/ext" ? "/ext/" : root.replace(/\/$/, "") + "/";
  const items = new Map();
  for (const [entryPath, entry] of Object.entries(state.storage)) {
    if (!entryPath.startsWith(base)) continue;
    const relative = entryPath.slice(base.length);
    if (!relative) continue;
    const name = relative.split("/")[0];
    const direct = !relative.includes("/");
    const existing = items.get(name);
    if (!existing || !direct) items.set(name, {
      type: direct ? (entry.type || "file") : "dir",
      name,
      size: direct && Buffer.isBuffer(entry.data) ? entry.data.length : 0,
    });
  }
  return Array.from(items.values());
}
const EMULATED_TIMEZONES = ["Europe/Madrid", "Europe/Amsterdam", "America/New_York", "UTC"];
function timezoneRecord(name, timestampMs = Date.now()) {
  if (!EMULATED_TIMEZONES.includes(name)) return null;
  try {
    const offsetName = new Intl.DateTimeFormat("en-US", {
      timeZone: name,
      timeZoneName: "shortOffset",
    }).formatToParts(new Date(timestampMs)).find((part) => part.type === "timeZoneName").value;
    const match = /^GMT(?:(?<sign>[+-])(?<hours>\d{1,2})(?::(?<minutes>\d{2}))?)?$/.exec(offsetName);
    if (!match) return null;
    const sign = match.groups && match.groups.sign === "-" ? -1 : 1;
    const hours = Number(match.groups && match.groups.hours || 0);
    const minutes = Number(match.groups && match.groups.minutes || 0);
    const offset = sign * (hours * 3600 + minutes * 60);
    const abbr = new Intl.DateTimeFormat("en-US", {
      timeZone: name,
      timeZoneName: "short",
    }).formatToParts(new Date(timestampMs)).find((part) => part.type === "timeZoneName").value;
    return { name, offset, abbr: name === "UTC" ? "UTC" : abbr };
  } catch (_) {
    return null;
  }
}
function formatTimestampInZone(timestampMs, timezoneName) {
  const zone = timezoneRecord(timezoneName, timestampMs);
  if (!zone) throw new Error("unknown timezone");
  const local = new Date(timestampMs + zone.offset * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  const sign = zone.offset < 0 ? "-" : "+";
  const absoluteMinutes = Math.abs(zone.offset) / 60;
  return `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`
    + `T${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}:${pad(local.getUTCSeconds())}`
    + `${sign}${pad(Math.floor(absoluteMinutes / 60))}:${pad(absoluteMinutes % 60)}`;
}
function currentDeviceTimestamp() {
  const timestamp = Date.now() + Number(state.clock_offset_ms || 0);
  return formatTimestampInZone(timestamp, state.timezone.name);
}
function persistAndBroadcast() { saveState(); broadcast(); }

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".ttf": "font/ttf", ".woff2": "font/woff2", ".json": "application/json" };
// Decode percent-escapes and resolve inside root (frame files may contain spaces).
function staticPath(root, sub) {
  let rel; try { rel = decodeURIComponent(sub); } catch (_) { return null; }
  const file = path.join(root, rel);
  return file.startsWith(root + path.sep) ? file : null;
}
function serveStatic(res, file) {
  if (!file) { fail(res, 404, "not found"); return; }
  fs.readFile(file, (err, buf) => {
    if (err) { fail(res, 404, "not found"); return; }
    const ext = path.extname(file);
    // App files must not be cached (dev); heavy immutable assets can be.
    const cache = /\.(ttf|woff2|png|svg)$/.test(ext) ? "max-age=86400" : "no-cache";
    res.writeHead(200, Object.assign({ "Content-Type": MIME[ext] || "application/octet-stream", "Cache-Control": cache }, CORS));
    res.end(buf);
  });
}

/* -------------------------------- routes -------------------------------- */
const server = http.createServer(async (req, res) => {
  let p, q;
  try { const u = new URL(req.url, "http://localhost"); p = u.pathname; q = Object.fromEntries(u.searchParams); }
  catch (_) { return fail(res, 400, "bad request"); }
  const method = req.method;
  // Scenario: a simulated USB/Wi-Fi drop gives non-emulator API traffic,
  // including preflights, a dead socket (ECONNRESET).
  if (scenario.offline_until > Date.now() && p.startsWith("/api/") && !p.startsWith("/api/_")) { req.socket.destroy(); return; }
  if (method === "OPTIONS") { send(res, 204, {}); return; }

  // static + stream (no auth); UI tab paths (emulator-only) fall back to the SPA
  if (method === "GET" && (p === "/" || p === "/index.html" || /^\/(network|firmware|settings|draw-tool|apps|scenarios)$/.test(p))) return serveStatic(res, fs.existsSync(path.join(DIST, "index.html")) ? path.join(DIST, "index.html") : path.join(PUBLIC, "index.html"));
  if (method === "GET" && p.startsWith("/static/")) return serveStatic(res, staticPath(DIST, p.replace(/^\//, "")));
  if ((method === "GET" || method === "HEAD") && p === "/favicon.png") return serveStatic(res, path.join(DIST, "favicon.png"));
  if (method === "GET" && p === "/events") {
    res.writeHead(200, Object.assign({ "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive" }, CORS));
    res.write("retry: 2000\n\n"); res.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
    clients.add(res); req.on("close", () => clients.delete(res)); return;
  }
  if (method === "GET" && (p.startsWith("/public/") || p.startsWith("/animations/") || p.startsWith("/fonts/"))) return serveStatic(res, staticPath(PUBLIC, p.replace(/^\/public\//, "").replace(/^\//, "")));
  if (method === "GET" && p === "/api/_animations") return send(res, 200, ANIMATIONS);
  if (method === "GET" && p === "/api/_sounds") return send(res, 200, SOUNDS);
  if (method === "GET" && p.startsWith("/assets/")) {
    const a = state.assets[decodeURIComponent(p.slice("/assets/".length))];
    if (!a) return fail(res, 404, "asset not found");
    res.writeHead(200, Object.assign({ "Content-Type": a.type || "application/octet-stream" }, CORS)); return res.end(a.buf);
  }
  // API-version gate (real device: 405 if X-API-Sem-Ver major != 25), version/access/transport exempt
  const sv = req.headers["x-api-sem-ver"];
  if (sv && !/\/api\/(version|access|transport)/.test(p)) {
    const major = String(sv).split(".")[0];
    if (!/^\d+$/.test(major)) return fail(res, 400, "bad X-API-Sem-Ver");
    if (major !== "25") return fail(res, 405, "Incompatible API version");
  }
  // auth gate (always-allow version/access/transport)
  if (p.startsWith("/api/") && !/\/api\/(version|access|transport)/.test(p) && !authed(req)) return fail(res, 403, "Forbidden");

  try {
    /* ---- display ---- */
    if (p === "/api/screen" && method === "GET") {
      const display = Number(q.display || 0);
      if (display !== 0 && display !== 1) return fail(res, 400, "Bad request: display must be 0 or 1");
      const pixels = await renderScreen(state.frame, state.assets, {
        display,
        elementStartedAt,
        animations: ANIMATIONS,
        animationRoot: ANIM_DIR,
      });
      logCall("GET", p, `display ${display}`); return sendScreen(res, pixels);
    }
    if (p === "/api/display/draw" && method === "POST") {
      const b = await readJson(req);
      const appName = b.application_name || b.app_id;   // accept both (community scripts use app_id)
      if (!appName) return fail(res, 400, "Bad request: application_name required");
      const elements = b.elements;
      if (!Array.isArray(elements) || !elements.length) return fail(res, 400, "Nothing to display");
      if (elements.length > 100) return fail(res, 400, "Elements number limit exceeded");
      let priority = b.priority == null ? 50 : b.priority;
      if (typeof priority !== "number" || priority < 1 || priority > 100) return fail(res, 400, "Bad request: priority 1-100");
      // Firmware resolves image paths inside the drawing app's asset namespace
      // (busylib docs: upload filename="logo.png", then draw path="logo.png").
      // Rewrite bare paths to the namespaced asset key; full keys keep working.
      for (const el of elements) {
        if (el && el.type === "image" && el.path && state.assets[`${appName}/${el.path}`]) el.path = `${appName}/${el.path}`;
      }
      const blocked = canvasBlockReason();
      if (blocked) return fail(res, 409, `Not drawn: ${blocked}`);
      const result = drawFrame(appName, elements, priority);
      if (!result.ok) return fail(res, result.status, result.reason);
      if (b.led_notification_color) emit("led", { color: b.led_notification_color });
      logCall("POST", p, `${appName} · ${elements.length} el · pri ${priority}`); broadcast(); return ok(res);
    }
    if (p === "/api/display/draw" && method === "DELETE") {
      const app = q.application_name;
      if (!app || state.frame.application_name === app || !state.frame.elements.length) {
        clearFrame();
      }
      logCall("DELETE", p, app || "all"); broadcast(); return ok(res);
    }
    if (p === "/api/display/brightness") {
      if (method === "GET") { logCall("GET", p); return send(res, 200, { value: state.brightness === "auto" ? "auto" : String(state.brightness) }); }
      if (method === "POST") {
        const v = q.value;
        if (v === "auto") state.brightness = "auto";
        else { const n = Number(v); if (!(n >= 0 && n <= 100)) return fail(res, 400, "Bad request: value 0-100 or auto"); state.brightness = n; }
        saveState(); logCall("POST", p, `value ${v}`); broadcast(); return ok(res);
      }
    }

    /* ---- audio ---- */
    if (p === "/api/audio/play" && method === "POST") {
      const b = await readJson(req);
      if (!b.application_name) return fail(res, 400, "Missing application_name");
      if (b.path && b.stock_path) return fail(res, 400, "Both path and stock_path are defined");
      if (!b.path && !b.stock_path) return fail(res, 400, "Missing path or stock_path");
      logCall("POST", p, b.stock_path || b.path || "");
      let url = null;
      // firmware resolves the basename after the last "/" incl. extension; also accept the bare name (emulator-only)
      if (b.stock_path) { const base = path.basename(b.stock_path); for (const k of [b.stock_path, base, base.replace(/\.(wav|mp3|ogg)$/i, "")]) if (SOUNDS[k]) { url = "/public/sounds/" + SOUNDS[k]; break; } }
      if (!url && b.path) {
        const nk = `${b.application_name}/${b.path}`;  // firmware resolves bare paths inside the app's asset namespace
        if (state.assets[nk]) url = "/assets/" + nk;
        else if (state.assets[b.path]) url = "/assets/" + b.path;
        else if (state.storage[b.path]) url = "/api/storage/read?path=" + encodeURIComponent(b.path);
      }
      // firmware 404s an unplayable file; no stock sounds are bundled, so unresolved paths 200 + beep fallback (emulator-only)
      emit("beep", { url, path: b.path || null, stock_path: b.stock_path || null }); return ok(res);
    }
    if (p === "/api/audio/play" && method === "DELETE") { logCall("DELETE", p, "stop"); emit("beep", { stop: true }); return ok(res); }
    if (p === "/api/audio/volume") {
      if (method === "GET") { logCall("GET", p); return send(res, 200, { volume: state.volume }); }
      if (method === "POST") { const n = Number(q.volume); if (!(n >= 0 && n <= 100)) return fail(res, 400, "Bad request: volume 0-100"); state.volume = n; saveState(); logCall("POST", p, `vol ${n}`); broadcast(); return ok(res); }
    }

    /* ---- assets (raw octet-stream, ?file=) ---- */
    if (p === "/api/assets/upload" && method === "POST") {
      const app = q.application_name, file = q.file;
      if (!app || !file) return fail(res, 400, "application_name and file required");
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(app) || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(file)) {
        return fail(res, 400, "application_name and file must be safe names");
      }
      let buf;
      const ct = req.headers["content-type"] || "";
      if (ct.includes("application/json")) { const b = await readJson(req); buf = Buffer.from(b.data || "", "base64"); }
      else buf = await readBody(req);
      const ext = (file.match(/\.([a-z0-9]+)$/i) || [])[1];
      const type = { png: "image/png", gif: "image/gif", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", svg: "image/svg+xml",
        wav: "audio/wav", mp3: "audio/mpeg", ogg: "audio/ogg" }[(ext || "").toLowerCase()] || "application/octet-stream";
      state.assets[`${app}/${file}`] = { buf, type };
      saveState(); logCall("POST", p, `${app}/${file} · ${buf.length}b`); return ok(res);
    }
    if (p === "/api/assets/upload" && method === "DELETE") {
      const app = q.application_name; if (!app) return fail(res, 400, "application_name required");
      let n = 0; for (const k of Object.keys(state.assets)) if (k.startsWith(app + "/")) { delete state.assets[k]; n++; }
      if (!n) return fail(res, 404, "Assets not found");
      saveState(); logCall("DELETE", p, app); return ok(res);
    }

    /* ---- storage (?path=, raw bodies) ---- */
    if (p === "/api/storage/write" && method === "POST") {
      const target = storagePath(q.path);
      if (!target) return fail(res, 400, "valid /ext path required");
      const data = await readBody(req);
      if (storageUsage().used_bytes - (state.storage[target] && state.storage[target].data ? state.storage[target].data.length : 0) + data.length > storageUsage().total_bytes) {
        return fail(res, 507, "storage capacity exceeded");
      }
      state.storage[target] = { type: "file", data };
      saveState(); logCall("POST", p, target); return ok(res, { size: data.length });
    }
    if (p === "/api/storage/read" && method === "GET") {
      const target = storagePath(q.path), item = target && state.storage[target];
      if (!item || item.type !== "file") return fail(res, 404, "file not found");
      logCall("GET", p, target); return send(res, 200, Buffer.isBuffer(item.data) ? item.data : Buffer.alloc(0));
    }
    if (p === "/api/storage/list" && method === "GET") {
      const items = storageList(q.path || "/ext");
      if (!items) return fail(res, 400, "valid /ext path required");
      logCall("GET", p, q.path || "/ext"); return send(res, 200, { list: items });
    }
    if (p === "/api/storage/remove" && method === "DELETE") {
      const target = storagePath(q.path);
      if (!target) return fail(res, 400, "valid /ext path required");
      let removed = 0;
      for (const key of Object.keys(state.storage)) {
        if (key === target || key.startsWith(target + "/")) { delete state.storage[key]; removed += 1; }
      }
      if (!removed) return fail(res, 404, "path not found");
      saveState(); logCall("DELETE", p, target); return ok(res, { removed });
    }
    if (p === "/api/storage/mkdir" && method === "POST") {
      const target = storagePath(q.path);
      if (!target) return fail(res, 400, "valid /ext path required");
      state.storage[target] = { type: "dir", data: null };
      saveState(); logCall("POST", p, target); return ok(res);
    }
    if (p === "/api/storage/rename" && method === "POST") {
      const source = storagePath(q.path), destination = storagePath(q.new_path);
      if (!source || !destination) return fail(res, 400, "valid source and destination paths required");
      const matches = Object.keys(state.storage).filter((key) => key === source || key.startsWith(source + "/"));
      if (!matches.length) return fail(res, 404, "source path not found");
      for (const key of matches) state.storage[destination + key.slice(source.length)] = state.storage[key];
      for (const key of matches) delete state.storage[key];
      saveState(); logCall("POST", p, `${source} to ${destination}`); return ok(res, { renamed: matches.length });
    }
    if (p === "/api/storage/status" && method === "GET") return send(res, 200, storageUsage());

    /* ---- busy timer ---- */
    if (p === "/api/busy/snapshot") {
      if (method === "GET") { logCall("GET", p); return send(res, 200, state.busy_snapshot); }
      if (method === "PUT") {
        const b = await readJson(req); const snap = b.snapshot || {};
        const type = snap.type; const TYPES = ["NOT_STARTED", "INFINITE", "SIMPLE", "INTERVAL"];
        if (!TYPES.includes(type)) return fail(res, 400, "Bad request: snapshot.type");
        const kept = { type, busy_bar_settings: snap.busy_bar_settings || Object.assign({}, BAR_SETTINGS) };
        for (const k of ["card_id", "is_paused", "time_left_ms", "current_interval", "current_interval_time_total_ms", "current_interval_time_left_ms", "interval_settings"]) if (snap[k] !== undefined) kept[k] = snap[k];
        state.busy_snapshot = { snapshot: kept, snapshot_timestamp_ms: b.snapshot_timestamp_ms || Date.now() };
        if (type !== "NOT_STARTED" && state.frame.elements.length) clearFrame();
        saveState(); logCall("PUT", p, type); broadcast(); return ok(res);
      }
    }
    const mProf = p.match(/^\/api\/busy\/profiles\/(busy|custom)$/);
    if (mProf) {
      const slot = mProf[1];
      if (method === "GET") { logCall("GET", p); return send(res, 200, state.busy_profiles[slot]); }
      if (method === "PUT") { const b = await readJson(req); state.busy_profiles[slot] = Object.assign({}, state.busy_profiles[slot], b, { profile_timestamp_ms: Date.now() }); saveState(); logCall("PUT", p, slot); return ok(res); }
    }

    /* ---- device ---- */
    if (p === "/api/name") {
      if (method === "GET") { logCall("GET", p); return send(res, 200, { name: state.name }); }
      if (method === "POST") { const b = await readJson(req); if (typeof b.name !== "string") return fail(res, 400, "name required"); state.name = b.name; saveState(); logCall("POST", p, state.name); broadcast(); return ok(res); }
    }
    if (p === "/api/time" && method === "GET") { logCall("GET", p); return send(res, 200, { timestamp: currentDeviceTimestamp() }); }
    if (p === "/api/time/timestamp" && method === "POST") {
      const timestamp = Date.parse(String(q.timestamp || ""));
      if (!Number.isFinite(timestamp)) return fail(res, 400, "valid ISO 8601 timestamp required");
      state.clock_offset_ms = timestamp - Date.now();
      saveState(); logCall("POST", p, q.timestamp); return ok(res);
    }
    if (p === "/api/time/timezone") {
      if (method === "GET") {
        const current = timezoneRecord(
          state.timezone.name,
          Date.now() + Number(state.clock_offset_ms || 0),
        );
        if (current) state.timezone = current;
        return send(res, 200, state.timezone);
      }
      if (method === "POST") {
        const zone = timezoneRecord(q.timezone);
        if (!zone) return fail(res, 400, "unknown timezone");
        state.timezone = zone; saveState(); logCall("POST", p, zone.name); return ok(res);
      }
    }
    if (p === "/api/time/tzlist" && method === "GET") {
      const timestamp = Date.now() + Number(state.clock_offset_ms || 0);
      return send(res, 200, { list: EMULATED_TIMEZONES.map((name) => timezoneRecord(name, timestamp)) });
    }

    if (p === "/api/status" || STATUS_ENDPOINTS.has(p)) {
      const up = Math.floor((Date.now() - state.startTime) / 1000);
      const groups = {
        device: {
          serial_number: "EMU00000000", otp_model: "BUSY Bar Emulator",
          usb_mac: "02:00:00:00:00:01", wifi_mac: "02:00:00:00:00:02", ble_mac: "02:00:00:00:00:03",
          otp_valid: true, firmware_security: "none", otp_timestamp: 1785628800,
        },
        firmware: { version: "emulator-1.2.0", target: "emu", branch: "dev", build_date: "2026-08-02", commit_hash: "emulator", api_semver: API_SEMVER },
        system: { api_semver: API_SEMVER, uptime: uptimeStr(up), boot_time: Math.floor(state.startTime / 1000), auto_update_enabled: state.update.autoupdate.is_enabled },
        power: { state: scenario.power_state, battery_charge: state.battery_charge,
          battery_voltage: Math.round(3500 + state.battery_charge * 7),
          battery_current: scenario.power_state === "charging" ? 350 : scenario.power_state === "charged" ? 0 : -120,
          usb_voltage: scenario.power_state === "discharging" ? 0 : 5000 },
      };
      const sub = p.slice("/api/status/".length);
      logCall("GET", p);
      if (p === "/api/status") return send(res, 200, groups);
      if (groups[sub]) return send(res, 200, groups[sub]);
      return fail(res, 404, "no such status group");
    }
    if (p === "/api/version" && method === "GET") { logCall("GET", p); return send(res, 200, { api_semver: API_SEMVER }); }
    if (p === "/api/transport" && method === "GET") { return send(res, 200, { type: isLocal(req) ? "usb" : "wifi" }); }
    if (p === "/api/access") {
      if (method === "GET") return send(res, 200, state.access);
      if (method === "POST") {
        if (!["enabled", "disabled", "key"].includes(q.mode)) return fail(res, 400, "mode must be enabled, disabled, or key");
        if (q.mode === "key" && !/^[0-9]{4,10}$/.test(String(q.key || ""))) return fail(res, 400, "key must contain 4 to 10 digits");
        state.access = { mode: q.mode, key_valid: q.mode !== "key" || Boolean(TOKEN) };
        saveState(); logCall("POST", p, q.mode); return ok(res, { effective_after_restart: true });
      }
    }
    if (p === "/api/input" && method === "POST") { const KEYS = ["up", "down", "ok", "back", "start", "busy", "custom", "off", "apps", "settings"]; if (!KEYS.includes(q.key)) return fail(res, 400, "bad key"); logCall("POST", p, q.key); emit("input", { key: q.key }); return ok(res); }
    if (p === "/api/log_dump" && method === "POST") {
      const base = String(q.filename || "dump").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80) || "dump";
      const target = `/ext/logs/${base}.txt`;
      const lines = state.log.slice().reverse().map((entry) => `${new Date(entry.t).toISOString()} ${entry.method} ${entry.path} ${entry.note || ""}`);
      state.storage["/ext/logs"] = { type: "dir", data: null };
      state.storage[target] = { type: "file", data: Buffer.from(lines.join("\n") + "\n", "utf8") };
      saveState(); logCall("POST", p, base); return ok(res, { path: target });
    }

    /* ---- connectivity and device services ---- */
    if (p === "/api/wifi/status" && method === "GET") return send(res, 200, state.wifi);
    if (p === "/api/wifi/networks" && method === "GET") {
      const networks = [
        { ssid: "FounderOS Lab", security: "WPA2", rssi: -38, channel: 6, bssid: "02:00:00:00:10:01" },
        { ssid: "BUSY Guest", security: "WPA2", rssi: -62, channel: 11, bssid: "02:00:00:00:10:02" },
      ];
      return send(res, 200, { count: networks.length, networks });
    }
    if (p === "/api/wifi/connect" && method === "POST") {
      const body = await readJson(req);
      const ssid = String(body.ssid || "").trim();
      if (!ssid || ssid.length > 32) return fail(res, 400, "ssid must contain 1 to 32 characters");
      const security = String(body.security || "WPA2");
      if (!["open", "WEP", "WPA", "WPA2", "WPA3"].includes(security)) return fail(res, 400, "unsupported Wi-Fi security mode");
      const requestedIp = body.ip_config && typeof body.ip_config === "object" ? body.ip_config : { ip_method: "dhcp" };
      const methodName = String(requestedIp.ip_method || "dhcp");
      if (!["dhcp", "static"].includes(methodName)) return fail(res, 400, "ip_method must be dhcp or static");
      if (methodName === "static" && !requestedIp.address) return fail(res, 400, "static address required");
      state.wifi = {
        state: "connected", ssid, bssid: "02:00:00:00:10:01", channel: 6, rssi: -38, security,
        ip_config: methodName === "static"
          ? { ip_method: "static", ip_type: "ipv4", address: String(requestedIp.address), mask: String(requestedIp.mask || "255.255.255.0"), gateway: String(requestedIp.gateway || "") }
          : { ip_method: "dhcp", ip_type: "ipv4", address: "192.0.2.25", mask: "255.255.255.0", gateway: "192.0.2.1" },
      };
      persistAndBroadcast(); logCall("POST", p, ssid); return ok(res);
    }
    if (p === "/api/wifi/disconnect" && method === "POST") {
      state.wifi = { state: "disconnected", ssid: "", bssid: "", channel: null, rssi: null, security: "WPA2", ip_config: null };
      persistAndBroadcast(); logCall("POST", p, "disconnected"); return ok(res);
    }

    if (p === "/api/ble/status" && method === "GET") return send(res, 200, state.ble);
    if (p === "/api/ble/enable" && method === "POST") {
      state.ble.status = state.ble.address ? "connectable" : "enabled";
      persistAndBroadcast(); logCall("POST", p, state.ble.status); return ok(res);
    }
    if (p === "/api/ble/disable" && method === "POST") {
      state.ble.status = "disabled";
      persistAndBroadcast(); logCall("POST", p, "disabled"); return ok(res);
    }
    if (p === "/api/ble/pairing" && method === "DELETE") {
      state.ble.address = "";
      if (state.ble.status !== "disabled") state.ble.status = "enabled";
      persistAndBroadcast(); logCall("DELETE", p, "pairing removed"); return ok(res);
    }

    if (p === "/api/smart_home/pairing" && method === "GET") {
      if (state.smart_home.pairing && state.smart_home.pairing.available_until <= Date.now()) {
        state.smart_home.pairing = null;
        state.smart_home.latest_pairing_status = { value: "expired", timestamp: Math.floor(Date.now() / 1000) };
      }
      return send(res, 200, {
        fabric_count: state.smart_home.fabric_count,
        latest_pairing_status: state.smart_home.latest_pairing_status,
      });
    }
    if (p === "/api/smart_home/pairing" && method === "POST") {
      const availableUntil = Date.now() + 15 * 60 * 1000;
      state.smart_home.pairing = {
        manual_code: "34970112332",
        qr_code: "MT:Y.K9042C00KA0648G00",
        available_until: availableUntil,
      };
      state.smart_home.latest_pairing_status = { value: "window_open", timestamp: Math.floor(Date.now() / 1000) };
      persistAndBroadcast(); logCall("POST", p, "window open"); return send(res, 200, state.smart_home.pairing);
    }
    if (p === "/api/smart_home/pairing" && method === "DELETE") {
      state.smart_home.fabric_count = 0;
      state.smart_home.pairing = null;
      state.smart_home.latest_pairing_status = { value: "erased", timestamp: Math.floor(Date.now() / 1000) };
      persistAndBroadcast(); logCall("DELETE", p, "all pairings removed"); return ok(res);
    }
    if (p === "/api/smart_home/switch" && method === "GET") return send(res, 200, state.smart_home.switch);
    if (p === "/api/smart_home/switch" && method === "POST") {
      const body = await readJson(req);
      if (typeof body.state !== "boolean") return fail(res, 400, "boolean state required");
      const startup = body.startup === undefined ? state.smart_home.switch.startup : String(body.startup);
      if (!["on", "off", "toggle", "last"].includes(startup)) return fail(res, 400, "startup must be on, off, toggle, or last");
      state.smart_home.switch = { state: body.state, startup };
      persistAndBroadcast(); logCall("POST", p, body.state ? "on" : "off"); return ok(res);
    }

    if (p === "/api/account/info" && method === "GET") {
      return send(res, 200, state.account.linked
        ? { linked: true, email: state.account.email, id: state.account.id, user_id: state.account.user_id }
        : { linked: false });
    }
    if (p === "/api/account/status" && method === "GET") return send(res, 200, { status: state.account.status });
    if (p === "/api/account/backend" && method === "GET") return send(res, 200, state.account.backend);
    if (p === "/api/account/backend" && method === "PUT") {
      const body = await readJson(req);
      const serverUrl = String(body.server_url || "default").trim();
      const certType = String(body.client_cert_type || "default").trim();
      if (!serverUrl || serverUrl.length > 512 || !certType || certType.length > 64) return fail(res, 400, "invalid backend configuration");
      state.account.backend = { server_url: serverUrl, client_cert_type: certType, ignore_server_cert: Boolean(body.ignore_server_cert) };
      persistAndBroadcast(); logCall("PUT", p, serverUrl); return ok(res);
    }
    if (p === "/api/account/link" && method === "POST") {
      state.account.link = { code: "734921", expires_at: Math.floor(Date.now() / 1000) + 600 };
      state.account.status = "link_pending";
      persistAndBroadcast(); logCall("POST", p, "link pending"); return send(res, 200, state.account.link);
    }
    if (p === "/api/account" && method === "DELETE") {
      state.account.linked = false; state.account.email = ""; state.account.id = ""; state.account.user_id = "";
      state.account.status = "disconnected"; state.account.link = null;
      persistAndBroadcast(); logCall("DELETE", p, "unlinked"); return ok(res);
    }

    if (p === "/api/update/status" && method === "GET") {
      return send(res, 200, { install: state.update.install, check: state.update.check });
    }
    if (p === "/api/update/check" && method === "POST") {
      state.update.check = { status: "completed", event: "update_available", available_version: "emulator-1.3.0" };
      persistAndBroadcast(); logCall("POST", p, "completed"); return ok(res);
    }
    if (p === "/api/update/changelog" && method === "GET") {
      const version = String(q.version || state.update.check.available_version || "emulator-1.3.0");
      return send(res, 200, { version, changelog: "Emulator compatibility release with the complete BarPilot API 25 contract." });
    }
    if (p === "/api/update/install" && method === "POST") {
      const version = String(q.version || state.update.check.available_version || "").trim();
      if (!version) return fail(res, 400, "version required");
      state.update.install = { is_allowed: true, action: "install", event: "requested", status: "completed", detail: version, download: { received_bytes: 1, total_bytes: 1 } };
      persistAndBroadcast(); logCall("POST", p, version); return ok(res);
    }
    if (p === "/api/update/abort_download" && method === "POST") {
      state.update.install = { is_allowed: true, action: "none", event: "aborted", status: "idle", detail: "", download: { received_bytes: 0, total_bytes: 0 } };
      persistAndBroadcast(); logCall("POST", p, "aborted"); return ok(res);
    }
    if (p === "/api/update/autoupdate" && method === "GET") return send(res, 200, state.update.autoupdate);
    if (p === "/api/update/autoupdate" && method === "POST") {
      const body = await readJson(req);
      if (typeof body.is_enabled !== "boolean") return fail(res, 400, "boolean is_enabled required");
      const validTime = (value) => /^([01]\d|2[0-3]):[0-5]\d$/.test(String(value));
      const start = body.interval_start || state.update.autoupdate.interval_start;
      const end = body.interval_end || state.update.autoupdate.interval_end;
      if (!validTime(start) || !validTime(end)) return fail(res, 400, "intervals must use HH:MM");
      state.update.autoupdate = { is_enabled: body.is_enabled, interval_start: start, interval_end: end };
      persistAndBroadcast(); logCall("POST", p, body.is_enabled ? "enabled" : "disabled"); return ok(res);
    }
    if (p === "/api/update" && method === "POST") {
      const firmware = await readBody(req);
      if (!firmware.length) return fail(res, 400, "firmware payload required");
      state.update.install = { is_allowed: true, action: "upload", event: "completed", status: "completed", detail: `${firmware.length} bytes`, download: { received_bytes: firmware.length, total_bytes: firmware.length } };
      persistAndBroadcast(); logCall("POST", p, `${firmware.length} bytes`); return ok(res);
    }

    /* ---- emulator: app runner ---- */
    if (p === "/api/_apps" && method === "GET") { return send(res, 200, { apps: scanApps(), app: appStatus() }); }
    if (p === "/api/_apps/start" && method === "POST") {
      const b = await readJson(req);
      const apps = scanApps();
      const entry = apps.find((a) => a.name === b.name);
      if (!entry) return fail(res, 404, `unknown app: ${b.name}`);
      const userArgs = b.args !== undefined ? b.args : [];
      if (!Array.isArray(userArgs)) return fail(res, 400, "args must be an array");
      if (userArgs.length > 8) return fail(res, 400, "args: max 8 entries");
      for (const a of userArgs) {
        if (typeof a !== "string") return fail(res, 400, "args entries must be strings");
        if (a.length > 64) return fail(res, 400, "args entry too long (max 64)");
        if (a.startsWith("--host")) return fail(res, 400, "args may not contain --host");
      }
      logCall("POST", p, `start ${entry.name}`);
      let pid;
      try {
        pid = await new Promise((resolve, reject) => {
          appOpChain = appOpChain.then(async () => {
            await stopApp();
            // Launcher-only: Run means "put this app on screen now", so release the
            // display first (same as a bare DELETE /api/display/draw). The draw API's
            // arbitration itself stays firmware-faithful for apps run outside the UI.
            if (state.frame.elements.length) { clearFrame(); broadcast(); }
            try { resolve(await startApp(entry, userArgs)); } catch (e) { reject(e); }
          });
        });
      } catch (e) { logCall("POST", p, `error ${e.message}`); return fail(res, 500, e.message); }
      return ok(res, { pid });
    }
    if (p === "/api/_apps/stop" && method === "POST") {
      logCall("POST", p, "stop");
      const stopped = await new Promise((resolve) => { appOpChain = appOpChain.then(async () => { resolve(await stopApp()); }); });
      return ok(res, { stopped });
    }

    /* ---- emulator: scenario simulator ---- */
    if (p === "/api/_scenario" && method === "GET") { return send(res, 200, scenarioInfo()); }
    if (p === "/api/_scenario/power" && method === "POST") {
      const b = await readJson(req);
      if (b.battery_charge === undefined && b.state === undefined) return fail(res, 400, "Bad request: battery_charge or state required");
      if (b.battery_charge !== undefined) {
        const n = Number(b.battery_charge);
        if (!Number.isFinite(n) || n < 0 || n > 100) return fail(res, 400, "Bad request: battery_charge 0-100");
        state.battery_charge = Math.round(n);
      }
      if (b.state !== undefined) {
        if (!["charging", "discharging", "charged"].includes(b.state)) return fail(res, 400, "Bad request: state charging|discharging|charged");
        scenario.power_state = b.state;
      }
      logCall("POST", p, `${scenario.power_state} · ${state.battery_charge}%`); broadcast(); return ok(res);
    }
    if (p === "/api/_scenario/offline" && method === "POST") {
      if (scenario.offline_until > Date.now()) {
        clearTimeout(offlineTimer); offlineTimer = null; scenario.offline_until = 0;
        logCall("POST", p, "restored"); broadcast(); return ok(res, { offline_until: 0 });
      }
      const b = await readJson(req);
      const n = Number(b.duration_ms);
      if (!Number.isFinite(n) || n < 100 || n > 600000) return fail(res, 400, "Bad request: duration_ms 100-600000");
      scenario.offline_until = Date.now() + n;
      offlineTimer = setTimeout(() => { offlineTimer = null; scenario.offline_until = 0; broadcast(); }, n);
      logCall("POST", p, `offline ${n}ms`); broadcast(); return ok(res, { offline_until: scenario.offline_until });
    }
    if (p === "/api/_scenario/steal" && method === "POST") {
      const b = await readJson(req);
      let priority = b.priority == null ? 99 : b.priority;
      if (typeof priority !== "number" || priority < 1 || priority > 100) return fail(res, 400, "Bad request: priority 1-100");
      let duration = null;
      if (b.duration_ms != null) {
        const n = Number(b.duration_ms);
        if (!Number.isFinite(n) || n < 100 || n > 600000) return fail(res, 400, "Bad request: duration_ms 100-600000");
        duration = n;
      }
      const elements = [
        { id: "s1", type: "rectangle", x: 0, y: 0, width: 72, height: 16, border_width: 1, border_color: "0xFF3C3CFF", fill: "none", display: "front" },
        { id: "s2", type: "text", text: `PRIORITY ${priority}`, x: 36, y: 8, font: "small", color: "0xFF3C3CFF", align: "center", display: "front" },
      ];
      const result = drawFrame(STEAL_APP, elements, priority);
      if (!result.ok) return fail(res, result.status, result.reason);
      clearTimeout(stealTimer); stealTimer = null;
      if (duration != null) {
        stealTimer = setTimeout(() => { stealTimer = null; if (state.frame.application_name === STEAL_APP) { clearFrame(); broadcast(); } }, duration);
      }
      logCall("POST", p, `pri ${priority}${duration ? ` · ${duration}ms` : ""}`); broadcast(); return ok(res, { priority });
    }
    if (p === "/api/_scenario/blocker" && method === "POST") {
      const b = await readJson(req);
      const fields = { menu: "menu_open", physical_busy: "physical_busy", smart_home: "smart_home_timer" };
      const field = fields[b.type];
      if (!field || typeof b.active !== "boolean") return fail(res, 400, "Bad request: type menu|physical_busy|smart_home and boolean active required");
      scenario[field] = b.active;
      if (b.active && b.type !== "menu" && state.frame.elements.length) clearFrame();
      logCall("POST", p, `${b.type} ${b.active ? "on" : "off"}`); broadcast(); return ok(res, { blockers: scenarioInfo().blockers });
    }
    if (p === "/api/_scenario/reset" && method === "POST") {
      clearTimeout(offlineTimer); offlineTimer = null; scenario.offline_until = 0;
      clearTimeout(stealTimer); stealTimer = null;
      if (state.frame.application_name === STEAL_APP) clearFrame();
      scenario.menu_open = false; scenario.physical_busy = false; scenario.smart_home_timer = false;
      state.busy_snapshot = { snapshot: { type: "NOT_STARTED", busy_bar_settings: Object.assign({}, BAR_SETTINGS) }, snapshot_timestamp_ms: Date.now() };
      scenario.power_state = "discharging"; state.battery_charge = 100;
      logCall("POST", p, "reset"); broadcast(); return ok(res);
    }

    fail(res, 404, `no route for ${method} ${p}`);
  } catch (err) { fail(res, 400, err.message || "bad request"); }
});

function consumeWebsocketFrames(client, chunk) {
  client.buffer = Buffer.concat([client.buffer, chunk]);
  while (client.buffer.length >= 2) {
    const first = client.buffer[0], second = client.buffer[1];
    const opcode = first & 0x0f, masked = Boolean(second & 0x80);
    if (!masked) { client.socket.destroy(); return; }
    let length = second & 0x7f, offset = 2;
    if (length === 126) { if (client.buffer.length < 4) return; length = client.buffer.readUInt16BE(2); offset = 4; }
    else if (length === 127) {
      if (client.buffer.length < 10) return;
      const large = client.buffer.readBigUInt64BE(2); if (large > 65536n) { client.socket.destroy(); return; }
      length = Number(large); offset = 10;
    }
    const maskLength = masked ? 4 : 0;
    if (client.buffer.length < offset + maskLength + length) return;
    const mask = masked ? client.buffer.subarray(offset, offset + 4) : null;
    offset += maskLength;
    const payload = Buffer.from(client.buffer.subarray(offset, offset + length));
    client.buffer = client.buffer.subarray(offset + length);
    if (mask) for (let index = 0; index < payload.length; index++) payload[index] ^= mask[index % 4];
    if (opcode === 8) { try { client.socket.write(websocketFrame(payload, 8)); } catch (_) {} client.socket.end(); return; }
    if (opcode === 9) { try { client.socket.write(websocketFrame(payload, 10)); } catch (_) {} continue; }
    if (opcode !== 1) continue;
    try {
      const message = JSON.parse(payload.toString("utf8"));
      if (typeof message.enable === "boolean") {
        client.enabled = message.enable;
        if (client.enabled) client.socket.write(websocketFrame(JSON.stringify(snapshot())));
      }
    } catch (_) {}
  }
}

server.on("upgrade", (req, socket) => {
  let pathname;
  try { pathname = new URL(req.url, "http://localhost").pathname; } catch (_) { socket.destroy(); return; }
  if (pathname !== "/api/status/ws" || String(req.headers.upgrade || "").toLowerCase() !== "websocket") { socket.destroy(); return; }
  if (TOKEN && !isLocal(req) && req.headers["x-api-token"] !== TOKEN) {
    socket.write("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); socket.destroy(); return;
  }
  const key = req.headers["sec-websocket-key"];
  if (!key) { socket.destroy(); return; }
  const accept = crypto.createHash("sha1").update(String(key) + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  socket.write(
    "HTTP/1.1 101 Switching Protocols\r\n"
    + "Upgrade: websocket\r\n"
    + "Connection: Upgrade\r\n"
    + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
  );
  const client = { socket, enabled: false, buffer: Buffer.alloc(0) };
  statusSockets.add(client);
  socket.on("data", (chunk) => consumeWebsocketFrames(client, chunk));
  socket.on("close", () => statusSockets.delete(client));
  socket.on("error", () => statusSockets.delete(client));
});

const statusSocketTimer = setInterval(statusSocketBroadcast, 1000);
statusSocketTimer.unref();

function killChild() {
  if (!appProc || !appProc.child) return;
  const pid = appProc.child.pid;
  if (!pid) return;
  // Use spawnSync("kill") so the signal is delivered synchronously before we exit.
  try { spawnSync("kill", ["-9", String(-pid)]); } catch (_) {}
  try { spawnSync("kill", ["-9", String(pid)]); } catch (_) {}
}
function startServer() {
  process.on("SIGINT", () => { killChild(); process.exit(0); });
  process.on("SIGTERM", () => { killChild(); process.exit(0); });
  server.listen(PORT, HOST, () => {
    console.log(`\n  BUSY Bar emulator running`);
    console.log(`  ├─ display : http://${HOST}:${PORT}/`);
    console.log(`  ├─ API base: http://${HOST}:${PORT}/api  (api_semver ${API_SEMVER})`);
    console.log(`  └─ ${Object.keys(ANIMATIONS).length} device animation(s)${TOKEN ? " · X-API-Token required for non-localhost" : ""}\n`);
  });
  return server;
}

if (require.main === module) startServer();

module.exports = {
  API_SEMVER,
  canvasBlockReason,
  clearFrame,
  consumeWebsocketFrames,
  drawFrame,
  formatTimestampInZone,
  elementExpiries,
  elementStartedAt,
  pruneExpiredElements,
  scenario,
  server,
  startServer,
  state,
  timezoneRecord,
  websocketFrame,
};
