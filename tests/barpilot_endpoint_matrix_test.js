"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { Readable, Writable } = require("stream");
const emulator = require("../server");
const liveBase = String(process.argv[2] || "").replace(/\/$/, "");
const apiToken = String(process.env.BARPILOT_API_TOKEN || "");

const contract = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "barpilot-api25-contract.json"),
  "utf8"
));

const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgQIAW3Y7WQAAAABJRU5ErkJggg==",
  "base64"
);

function query(pathname, values) {
  const params = new URLSearchParams(values || {});
  return pathname + (params.size ? `?${params}` : "");
}

function requestFor(method, pathname) {
  const request = { method, headers: { "X-API-Sem-Ver": "25.0.0" } };
  if (apiToken) request.headers["X-API-Token"] = apiToken;
  const json = (body) => {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(body);
  };
  if (pathname === "/api/log_dump") request.path = query(pathname, { filename: "barpilot-contract" });
  else if (pathname === "/api/name" && method === "POST") json({ name: "BUSY-CONTRACT" });
  else if (pathname === "/api/access" && method === "POST") request.path = query(pathname, { mode: "disabled" });
  else if (pathname === "/api/display/brightness" && method === "POST") request.path = query(pathname, { value: "73" });
  else if (pathname === "/api/audio/volume" && method === "POST") request.path = query(pathname, { volume: "31", silent: "1" });
  else if (pathname === "/api/busy/snapshot" && method === "PUT") json({ snapshot: { type: "NOT_STARTED", busy_bar_settings: { theme: "busy", show_work_phase_only: false, trigger_smart_home: true } }, snapshot_timestamp_ms: Date.now() });
  else if (pathname.startsWith("/api/busy/profiles/") && method === "PUT") json({ title: "Contract", sort_order: 2, timer_settings: { type: "INFINITE" }, busy_bar_settings: { theme: "busy", show_work_phase_only: false, trigger_smart_home: true } });
  else if (pathname === "/api/input") request.path = query(pathname, { key: "ok" });
  else if (pathname === "/api/display/draw" && method === "POST") json({ application_name: "barpilot-contract", priority: 70, elements: [{ id: "pixel", type: "rectangle", x: 0, y: 0, width: 1, height: 1, border_width: 0, fill: "solid", fill_colors: ["0xFF0000FF"] }] });
  else if (pathname === "/api/display/draw" && method === "DELETE") request.path = query(pathname, { application_name: "barpilot-contract" });
  else if (pathname === "/api/audio/play" && method === "POST") json({ application_name: "barpilot-contract", stock_path: "coding" });
  else if (pathname === "/api/assets/upload" && method === "POST") { request.path = query(pathname, { application_name: "barpilot-contract", file: "pixel.png" }); request.body = ONE_PIXEL_PNG; }
  else if (pathname === "/api/assets/upload" && method === "DELETE") request.path = query(pathname, { application_name: "barpilot-contract" });
  else if (pathname === "/api/screen") request.path = query(pathname, { display: "0" });
  else if (pathname === "/api/storage/list") request.path = query(pathname, { path: "/ext/contract" });
  else if (pathname === "/api/storage/read") request.path = query(pathname, { path: "/ext/contract/read.txt" });
  else if (pathname === "/api/storage/write") { request.path = query(pathname, { path: "/ext/contract/write.txt" }); request.body = Buffer.from("written", "utf8"); }
  else if (pathname === "/api/storage/remove") request.path = query(pathname, { path: "/ext/contract/remove.txt" });
  else if (pathname === "/api/storage/mkdir") request.path = query(pathname, { path: "/ext/contract/newdir" });
  else if (pathname === "/api/storage/rename") request.path = query(pathname, { path: "/ext/contract/rename-source.txt", new_path: "/ext/contract/rename-target.txt" });
  else if (pathname === "/api/time/timestamp") request.path = query(pathname, { timestamp: "2026-08-02T12:00:00+02:00" });
  else if (pathname === "/api/time/timezone" && method === "POST") request.path = query(pathname, { timezone: "Europe/Madrid" });
  else if (pathname === "/api/wifi/connect") json({ ssid: "FounderOS Lab", password: "not-persisted", security: "WPA2", ip_config: { ip_method: "dhcp" } });
  else if (pathname === "/api/smart_home/switch" && method === "POST") json({ state: true, startup: "toggle" });
  else if (pathname === "/api/account/backend" && method === "PUT") json({ server_url: "mqtts://example.invalid", client_cert_type: "default", ignore_server_cert: false });
  else if (pathname === "/api/update") request.body = Buffer.from("emulated-firmware", "utf8");
  else if (pathname === "/api/update/changelog") request.path = query(pathname, { version: "emulator-1.3.0" });
  else if (pathname === "/api/update/install") request.path = query(pathname, { version: "emulator-1.3.0" });
  else if (pathname === "/api/update/autoupdate" && method === "POST") json({ is_enabled: true, interval_start: "02:00", interval_end: "05:00" });
  request.path = request.path || pathname;
  return request;
}

async function callInMemory(method, pathname) {
  const request = requestFor(method, pathname);
  const body = request.body == null ? Buffer.alloc(0) : Buffer.isBuffer(request.body) ? request.body : Buffer.from(request.body);
  const incoming = Readable.from(body.length ? [body] : []);
  incoming.method = method;
  incoming.url = request.path;
  incoming.headers = Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key.toLowerCase(), value]));
  incoming.socket = { remoteAddress: "127.0.0.1", destroy() {} };
  const chunks = [];
  const outgoing = new Writable({ write(chunk, encoding, callback) { chunks.push(Buffer.from(chunk)); callback(); } });
  outgoing.statusCode = 200;
  outgoing.headers = {};
  outgoing.writeHead = (status, headers) => { outgoing.statusCode = status; outgoing.headers = headers || {}; };
  const finished = new Promise((resolve, reject) => {
    outgoing.once("finish", resolve);
    outgoing.once("error", reject);
  });
  emulator.server.emit("request", incoming, outgoing);
  await finished;
  const bytes = Buffer.concat(chunks);
  assert(outgoing.statusCode >= 200 && outgoing.statusCode < 300, `${method} ${pathname}: ${outgoing.statusCode} ${bytes.toString("utf8")}`);
  return { status: outgoing.statusCode, headers: outgoing.headers, bytes };
}

async function callLive(method, pathname) {
  const request = requestFor(method, pathname);
  const response = await fetch(liveBase + request.path, request);
  const bytes = Buffer.from(await response.arrayBuffer());
  assert(response.status >= 200 && response.status < 300, `${method} ${pathname}: ${response.status} ${bytes.toString("utf8")}`);
  return { status: response.status, headers: Object.fromEntries(response.headers), bytes };
}

const call = liveBase ? callLive : callInMemory;

async function main() {
  assert.strictEqual(contract.barpilot_endpoints.length, 53);
  assert.strictEqual(contract.barpilot_endpoints.reduce((sum, endpoint) => sum + endpoint.methods.length, 0), 69);
  const interfaceFont = await call("GET", "/fonts/Inter.ttf");
  assert(interfaceFont.bytes.length > 1000, "the emulator interface font must be served in production");
  if (liveBase) {
    const response = await fetch(liveBase + "/api/_scenario", { headers: apiToken ? { "X-API-Token": apiToken } : {} });
    assert.strictEqual(response.status, 200, "full endpoint mutation pass is allowed only against the FounderOS emulator");
    for (const [name, value] of [["read.txt", "readable"], ["remove.txt", "remove"], ["rename-source.txt", "rename"]]) {
      const request = { method: "POST", headers: { "X-API-Sem-Ver": "25.0.0" }, body: Buffer.from(value) };
      if (apiToken) request.headers["X-API-Token"] = apiToken;
      const seeded = await fetch(liveBase + query("/api/storage/write", { path: `/ext/contract/${name}` }), request);
      assert(seeded.ok, `could not seed ${name}`);
    }
  } else {
    emulator.clearFrame();
    emulator.state.storage = {
      "/ext/contract": { type: "dir", data: null },
      "/ext/contract/read.txt": { type: "file", data: Buffer.from("readable", "utf8") },
      "/ext/contract/remove.txt": { type: "file", data: Buffer.from("remove", "utf8") },
      "/ext/contract/rename-source.txt": { type: "file", data: Buffer.from("rename", "utf8") },
    };
    emulator.state.assets = {};
  }
  let operations = 0;
  for (const endpoint of contract.barpilot_endpoints) {
    for (const method of endpoint.methods) {
      const result = await call(method, endpoint.path);
      operations += 1;
      if (endpoint.path === "/api/screen") {
        const decoded = Buffer.from(result.bytes.toString("ascii"), "base64");
        assert.strictEqual(decoded.length, 72 * 16 * 3);
      }
    }
  }
  assert.strictEqual(operations, 69);
  if (!liveBase) {
    assert.strictEqual(emulator.state.name, "BUSY-CONTRACT");
    assert.strictEqual(emulator.state.brightness, 73);
    assert.strictEqual(emulator.state.volume, 31);
    assert.strictEqual(emulator.state.smart_home.switch.startup, "toggle");
    assert.strictEqual(emulator.state.update.autoupdate.is_enabled, true);
    assert.strictEqual(emulator.state.storage["/ext/contract/rename-target.txt"].data.toString("utf8"), "rename");
    assert.strictEqual(emulator.state.storage["/ext/contract/remove.txt"], undefined);
  }
  console.log("BarPilot endpoint matrix: 53 endpoints, 69 operations ok");
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
