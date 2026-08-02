"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const sharp = require("sharp");
const emulator = require("../server");
const { decodeMedia, decodePng, renderScreen } = require("../screen_renderer");

(async () => {

function rectangle(id, x, y, color, extra = {}) {
  return Object.assign({
    id, type: "rectangle", x, y, width: 1, height: 1,
    border_width: 0, fill: "solid", fill_colors: [color],
  }, extra);
}

emulator.clearFrame();
let result = emulator.drawFrame("founderos", [
  rectangle("bg", 0, 0, "0x000000FF"),
  { id: "title", type: "text", text: "Décision", x: 2, y: 2, font: "global", color: "0xFF0000FF" },
], 90);
assert.strictEqual(result.ok, true);
assert.strictEqual(emulator.state.frame.elements.length, 2);

result = emulator.drawFrame("founderos", [rectangle("pulse", 71, 15, "0x00FF00FF")], 90);
assert.strictEqual(result.ok, true);
assert.deepStrictEqual(emulator.state.frame.elements.map((element) => element.id), ["bg", "title", "pulse"]);
assert.strictEqual(emulator.state.frame.elements.find((element) => element.id === "title").text, "Décision");

assert.strictEqual(emulator.drawFrame("founderos", [rectangle("low", 1, 1, "0xFFFFFFFF")], 89).status, 409);
assert.strictEqual(emulator.drawFrame("other", [rectangle("equal", 1, 1, "0xFFFFFFFF")], 90).status, 409);
assert.strictEqual(emulator.drawFrame("other", [rectangle("high", 1, 1, "0xFFFFFFFF")], 91).ok, true);

assert.strictEqual(
  emulator.timezoneRecord("Europe/Madrid", Date.parse("2026-01-15T12:00:00Z")).offset,
  3600
);
assert.strictEqual(
  emulator.timezoneRecord("Europe/Madrid", Date.parse("2026-07-15T12:00:00Z")).offset,
  7200
);
assert.strictEqual(
  emulator.formatTimestampInZone(Date.parse("2026-01-15T12:34:56Z"), "Europe/Madrid"),
  "2026-01-15T13:34:56+01:00"
);

emulator.scenario.physical_busy = true;
assert.match(emulator.canvasBlockReason(), /physical BUSY/);
emulator.scenario.physical_busy = false;
emulator.scenario.menu_open = true;
assert.match(emulator.canvasBlockReason(), /menu/);
emulator.scenario.menu_open = false;
emulator.scenario.smart_home_timer = true;
assert.match(emulator.canvasBlockReason(), /smart-home/);
emulator.scenario.smart_home_timer = false;
emulator.state.busy_snapshot.snapshot.type = "SIMPLE";
assert.match(emulator.canvasBlockReason(), /BUSY session/);
emulator.state.busy_snapshot.snapshot.type = "NOT_STARTED";

emulator.clearFrame();
emulator.drawFrame("founderos", [rectangle("short", 0, 0, "0xFFFFFFFF", { timeout: 1 })], 90);
emulator.elementExpiries.set("id:short", Date.now() - 1);
assert.strictEqual(emulator.pruneExpiredElements(), true);
assert.strictEqual(emulator.state.frame.elements.length, 0);

const frame = {
  elements: [
    rectangle("red", 0, 0, "0xFF0000FF"),
    { id: "accent", type: "text", text: "É", x: 2, y: 1, font: "global", color: "0xFF0000FF" },
  ],
};
const front = await renderScreen(frame, {}, { display: 0 });
assert.strictEqual(front.length, 72 * 16 * 3);
assert.deepStrictEqual(Array.from(front.subarray(0, 3)), [0, 0, 255]);
assert(front.some((value, index) => index % 3 === 2 && value === 255));
const back = await renderScreen(frame, {}, { display: 1 });
assert.strictEqual(back.length, 80 * 80);
const decodedPng = decodePng(fs.readFileSync("public/animations/transition_flash_72x16/frame_0.png"));
assert(decodedPng);
assert.strictEqual(decodedPng.width, 72);
assert.strictEqual(decodedPng.height, 16);

async function encoded(format, color) {
  const pipeline = sharp({ create: { width: 2, height: 2, channels: 4, background: color } });
  return pipeline[format]().toBuffer();
}
async function animatedGif() {
  const raw = Buffer.alloc(2 * 4 * 4);
  for (let y = 0; y < 4; y++) for (let x = 0; x < 2; x++) {
    const at = (y * 2 + x) * 4;
    raw[at] = y < 2 ? 0 : 255;
    raw[at + 1] = y < 2 ? 255 : 0;
    raw[at + 2] = 0;
    raw[at + 3] = 255;
  }
  return sharp(raw, { raw: { width: 2, height: 4, channels: 4, pageHeight: 2 } })
    .gif({ delay: [100, 100], loop: 0 }).toBuffer();
}
function rgbAt(buffer, x, y = 0) {
  const offset = (y * 72 + x) * 3;
  return [buffer[offset + 2], buffer[offset + 1], buffer[offset]];
}
function boxIsLit(buffer, x, y, width, height) {
  for (let row = y; row < y + height; row++) for (let column = x; column < x + width; column++) {
    if (rgbAt(buffer, column, row).some((value) => value > 0)) return true;
  }
  return false;
}
const mediaAssets = {
  "media/red.jpg": { type: "image/jpeg", buf: await encoded("jpeg", { r: 255, g: 0, b: 0, alpha: 1 }) },
  "media/green.gif": { type: "image/gif", buf: await animatedGif() },
  "media/blue.webp": { type: "image/webp", buf: await encoded("webp", { r: 0, g: 0, b: 255, alpha: 1 }) },
  "media/yellow.svg": { type: "image/svg+xml", buf: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="#ffff00"/></svg>') },
};
assert.strictEqual(
  await decodeMedia(Buffer.from(
    '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><script>alert(1)</script></svg>'
  )),
  null
);
assert.strictEqual(
  await decodeMedia(Buffer.from(
    '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><image href="https://example.invalid/pixel.png"/></svg>'
  )),
  null
);
const mediaFrame = { elements: [
  { id: "jpeg", type: "image", path: "media/red.jpg", x: 0, y: 0 },
  { id: "gif", type: "image", path: "media/green.gif", x: 3, y: 0 },
  { id: "webp", type: "image", path: "media/blue.webp", x: 6, y: 0 },
  { id: "svg", type: "image", path: "media/yellow.svg", x: 9, y: 0 },
  { id: "mono", type: "image", stock_path: "check", color: "0xFFFFFFFF", x: 14, y: 0 },
  { id: "stock", type: "image", stock_path: "faces/emoji-grinning", x: 24, y: 0 },
] };
const mediaScreen = await renderScreen(mediaFrame, mediaAssets, { display: 0 });
assert(rgbAt(mediaScreen, 0)[0] > 220);
assert(rgbAt(mediaScreen, 3)[1] > 220);
assert(rgbAt(mediaScreen, 6)[2] > 220);
assert(rgbAt(mediaScreen, 9)[0] > 220 && rgbAt(mediaScreen, 9)[1] > 220);
assert(boxIsLit(mediaScreen, 14, 0, 8, 8));
assert(boxIsLit(mediaScreen, 24, 0, 14, 14));
const animatedScreen = await renderScreen({ elements: [
  { id: "animated-gif", type: "image", path: "media/green.gif", x: 0, y: 0, loop: true },
] }, mediaAssets, { display: 0, nowMs: 1150, elementStartedAt: new Map([["id:animated-gif", 1000]]) });
assert(rgbAt(animatedScreen, 0)[0] > 220);

const animationRoot = fs.mkdtempSync(path.join(os.tmpdir(), "founderos-animation-"));
try {
  const folder = path.join(animationRoot, "contract_2x2");
  fs.mkdirSync(folder);
  fs.writeFileSync(path.join(folder, "frame_0.png"), await encoded("png", { r: 255, g: 0, b: 255, alpha: 1 }));
  const animationScreen = await renderScreen({ elements: [
    { id: "animation", type: "animation", stock_path: "contract_2x2", x: 0, y: 0, loop: true },
  ] }, {}, {
    display: 0,
    animations: { contract_2x2: { frames: 1, fps: 30, start: 0, prefix: "frame_", pad: 0, sections: [] } },
    animationRoot,
  });
  const animationPixel = rgbAt(animationScreen, 0);
  assert(animationPixel[0] > 220 && animationPixel[2] > 220);
} finally {
  fs.rmSync(animationRoot, { recursive: true, force: true });
}

const writes = [];
const fakeSocket = { destroyed: false, write: (data) => writes.push(Buffer.from(data)), end: () => {}, destroy: () => {} };
const websocketClient = { socket: fakeSocket, enabled: false, buffer: Buffer.alloc(0) };
const command = Buffer.from('{"enable":true}', "utf8"), mask = Buffer.from([1, 2, 3, 4]);
const masked = Buffer.from(command.map((byte, index) => byte ^ mask[index % 4]));
emulator.consumeWebsocketFrames(
  websocketClient,
  Buffer.concat([Buffer.from([0x81, 0x80 | command.length]), mask, masked])
);
assert.strictEqual(websocketClient.enabled, true);
assert.strictEqual(writes.length, 1);
assert.strictEqual(writes[0][0], 0x81);

emulator.clearFrame();
console.log("emulator contract: ok");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
