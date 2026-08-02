"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const zlib = require("zlib");
const sharp = require("sharp");

const PUBLIC = path.join(__dirname, "public");
const ANIMATION_ROOT = path.join(PUBLIC, "animations");
const MAX_ASSET_BYTES = 8 * 1024 * 1024;
const MAX_IMAGE_EDGE = 512;
const MAX_DECODED_PIXELS = 8 * 1024 * 1024;
const MEDIA_CACHE_LIMIT = 128;

const ATLAS = JSON.parse(
  fs.readFileSync(path.join(__dirname, "public", "fonts", "font-atlas.json"), "utf8")
);
const FONT_KEYS = {
  tiny: "tiny", small: "small", normal: "normal", condensed: "condensed",
  bold: "bold", large: "large", extra_large: "extra_large", global: "global",
  medium: "normal", big: "extra_large",
};
const ALIGN = {
  top_left: [0, 0], top_mid: [0.5, 0], top_right: [1, 0],
  mid_left: [0, 0.5], center: [0.5, 0.5], mid_right: [1, 0.5],
  bottom_left: [0, 1], bottom_mid: [0.5, 1], bottom_right: [1, 1],
};
const MONO_ICONS = {
  sun: ["...#...", ".#.#.#.", "..###..", "###.###", "..###..", ".#.#.#.", "...#..."],
  cloud: ["..###..", ".#####.", "#######", "#######", "..###..", ".......", "......."],
  heart: [".##.##.", "#######", "#######", ".#####.", "..###..", "...#...", "......."],
  check: ["......#", ".....#.", "#...#..", ".#.#...", "..#....", ".......", "......."],
  bolt: ["...##..", "..##...", ".####..", "...##..", "..##...", "..#....", ".#....."],
};

function stockIconIndex() {
  const result = {};
  try {
    const categories = JSON.parse(fs.readFileSync(path.join(PUBLIC, "icons.json"), "utf8"));
    for (const [category, icons] of Object.entries(categories)) {
      for (const icon of icons) {
        const relative = String(icon.path || "").replace(/^draw_tool\//, "");
        const resolved = path.resolve(PUBLIC, "icons", relative);
        if (!resolved.startsWith(path.resolve(PUBLIC, "icons") + path.sep)) continue;
        const base = String(icon.fileName || "").replace(/\.svg$/i, "");
        result[`${category}/${base}`] = resolved;
        result[base] = resolved;
        result[String(icon.id)] = resolved;
      }
    }
  } catch (_) {}
  return result;
}

const STOCK_ICONS = stockIconIndex();
const mediaCache = new Map();
const animationFrameCache = new Map();

function parseColor(value) {
  let text = String(value == null ? "FFFFFFFF" : value).trim()
    .replace(/^0x/i, "").replace(/^#/, "");
  if (text.length === 3 || text.length === 4) text = text.split("").map((c) => c + c).join("");
  if (text.length < 6) text = "FFFFFF";
  return [
    parseInt(text.slice(0, 2), 16) || 0,
    parseInt(text.slice(2, 4), 16) || 0,
    parseInt(text.slice(4, 6), 16) || 0,
    text.length >= 8 ? (parseInt(text.slice(6, 8), 16) || 0) / 255 : 1,
  ];
}

function anchor(element, width, height) {
  const factor = ALIGN[element.align] || ALIGN.top_left;
  return [
    (Number(element.x) || 0) - Math.round(width * factor[0]),
    (Number(element.y) || 0) - Math.round(height * factor[1]),
  ];
}

function rasterize(text, fontId) {
  const font = ATLAS[FONT_KEYS[fontId] || "normal"];
  const parts = [];
  let pen = 0, right = 0;
  for (const character of String(text == null ? "" : text).normalize("NFC")) {
    const glyph = font.glyphs[String(character.codePointAt(0))] || font.glyphs["63"];
    if (!glyph) continue;
    parts.push([pen, glyph]);
    right = Math.max(right, pen + glyph.ox + glyph.w);
    pen += glyph.adv;
  }
  const width = Math.max(1, pen, right), height = font.lineh;
  const mask = new Uint8Array(width * height);
  for (const [origin, glyph] of parts) {
    for (let row = 0; row < glyph.h; row++) {
      const encoded = glyph.rows[row];
      if (!encoded) continue;
      const bits = BigInt(`0x${encoded}`), total = BigInt(encoded.length * 4);
      const y = glyph.oy + row;
      if (y < 0 || y >= height) continue;
      for (let column = 0; column < glyph.w; column++) {
        if ((bits >> (total - 1n - BigInt(column))) & 1n) {
          const x = origin + glyph.ox + column;
          if (x >= 0 && x < width) mask[y * width + x] = 1;
        }
      }
    }
  }
  return { width, height, mask, advance: pen, space: (font.glyphs["32"] || { adv: 4 }).adv };
}

function paeth(a, b, c) {
  const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

function decodePng(buffer) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (!Buffer.isBuffer(buffer) || buffer.length < 33 || !buffer.subarray(0, 8).equals(signature)) return null;
  let offset = 8, width = 0, height = 0, bitDepth = 0, colorType = 0, interlace = 0;
  const compressed = [], palette = [];
  let transparency = null;
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset); offset += 4;
    const type = buffer.subarray(offset, offset + 4).toString("ascii"); offset += 4;
    if (offset + length + 4 > buffer.length) return null;
    const data = buffer.subarray(offset, offset + length); offset += length + 4;
    if (type === "IHDR") {
      if (data.length !== 13) return null;
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      bitDepth = data[8]; colorType = data[9]; interlace = data[12];
    } else if (type === "IDAT") compressed.push(data);
    else if (type === "PLTE") for (let i = 0; i + 2 < data.length; i += 3) palette.push([data[i], data[i + 1], data[i + 2]]);
    else if (type === "tRNS") transparency = Buffer.from(data);
    else if (type === "IEND") break;
  }
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (!width || !height || width > 512 || height > 512 || width * height > 262144
      || bitDepth !== 8 || !channels || interlace !== 0 || !compressed.length) return null;
  const stride = width * channels, expectedRawLength = (stride + 1) * height;
  let raw;
  try { raw = zlib.inflateSync(Buffer.concat(compressed), { maxOutputLength: expectedRawLength }); } catch (_) { return null; }
  if (raw.length !== expectedRawLength) return null;
  const rows = Buffer.alloc(stride * height);
  let sourceOffset = 0;
  for (let y = 0; y < height; y++) {
    const filter = raw[sourceOffset++];
    for (let x = 0; x < stride; x++) {
      const value = raw[sourceOffset++];
      const left = x >= channels ? rows[y * stride + x - channels] : 0;
      const up = y ? rows[(y - 1) * stride + x] : 0;
      const upperLeft = y && x >= channels ? rows[(y - 1) * stride + x - channels] : 0;
      let decoded;
      if (filter === 0) decoded = value;
      else if (filter === 1) decoded = value + left;
      else if (filter === 2) decoded = value + up;
      else if (filter === 3) decoded = value + Math.floor((left + up) / 2);
      else if (filter === 4) decoded = value + paeth(left, up, upperLeft);
      else return null;
      rows[y * stride + x] = decoded & 255;
    }
  }
  const pixels = Buffer.alloc(width * height * 4);
  for (let i = 0; i < width * height; i++) {
    const source = i * channels, target = i * 4;
    if (colorType === 0) pixels[target] = pixels[target + 1] = pixels[target + 2] = rows[source];
    else if (colorType === 2 || colorType === 6) {
      pixels[target] = rows[source]; pixels[target + 1] = rows[source + 1]; pixels[target + 2] = rows[source + 2];
    } else if (colorType === 3) {
      const color = palette[rows[source]] || [0, 0, 0];
      pixels[target] = color[0]; pixels[target + 1] = color[1]; pixels[target + 2] = color[2];
    } else if (colorType === 4) pixels[target] = pixels[target + 1] = pixels[target + 2] = rows[source];
    pixels[target + 3] = colorType === 6 ? rows[source + 3]
      : colorType === 4 ? rows[source + 1]
      : colorType === 3 && transparency && rows[source] < transparency.length ? transparency[rows[source]] : 255;
  }
  return { width, height, pixels };
}

function cachePromise(key, factory) {
  if (mediaCache.has(key)) return mediaCache.get(key);
  const promise = factory().catch(() => null);
  mediaCache.set(key, promise);
  while (mediaCache.size > MEDIA_CACHE_LIMIT) mediaCache.delete(mediaCache.keys().next().value);
  return promise;
}

function safeSvg(buffer) {
  const text = buffer.toString("utf8");
  return !/(?:<!DOCTYPE|<script\b|<foreignObject\b|\b(?:href|xlink:href)\s*=\s*["'](?:https?:|file:|\/\/)|url\s*\()/i.test(text);
}

async function decodeMedia(buffer, targetHeight = 0) {
  if (!Buffer.isBuffer(buffer) || !buffer.length || buffer.length > MAX_ASSET_BYTES) return null;
  const hash = crypto.createHash("sha256").update(buffer).digest("hex");
  const key = `${hash}:${targetHeight || 0}`;
  return cachePromise(key, async () => {
    const png = !targetHeight && !buffer.includes(Buffer.from("acTL")) ? decodePng(buffer) : null;
    if (png) return { width: png.width, height: png.height, frames: [png.pixels], delays: [100], loop: 0 };
    const looksSvg = /<svg[\s>]/i.test(buffer.toString("utf8"));
    if (looksSvg && !safeSvg(buffer)) return null;
    let pipeline = sharp(buffer, {
      animated: true,
      failOn: "warning",
      limitInputPixels: MAX_DECODED_PIXELS,
      limitInputChannels: 4,
    });
    const metadata = await pipeline.metadata();
    const sourceWidth = Number(metadata.width) || 0;
    const sourceHeight = Number(metadata.pageHeight || metadata.height) || 0;
    if (!sourceWidth || !sourceHeight || sourceWidth > MAX_IMAGE_EDGE || sourceHeight > MAX_IMAGE_EDGE) return null;
    if (targetHeight > 0 && sourceHeight > targetHeight) {
      pipeline = pipeline.resize({ height: targetHeight, fit: "inside", withoutEnlargement: true, kernel: "nearest" });
    }
    const result = await pipeline.ensureAlpha().raw().toBuffer({ resolveWithObject: true });
    const pages = Math.max(1, Number(result.info.pages || metadata.pages) || 1);
    const pageHeight = Math.max(1, Number(result.info.pageHeight) || Math.floor(result.info.height / pages));
    const width = Number(result.info.width) || 0;
    if (!width || width > MAX_IMAGE_EDGE || pageHeight > MAX_IMAGE_EDGE || width * pageHeight * pages > MAX_DECODED_PIXELS) return null;
    const frameBytes = width * pageHeight * 4;
    if (result.data.length < frameBytes * pages) return null;
    const frames = [];
    for (let index = 0; index < pages; index++) {
      frames.push(Buffer.from(result.data.subarray(index * frameBytes, (index + 1) * frameBytes)));
    }
    const sourceDelays = Array.isArray(metadata.delay) ? metadata.delay : [];
    const delays = frames.map((_, index) => Math.max(10, Number(sourceDelays[index] || sourceDelays[0]) || 100));
    return { width, height: pageHeight, frames, delays, loop: Number(metadata.loop) || 0 };
  });
}

function selectedFrame(decoded, element, nowMs, startedMs) {
  if (!decoded || !decoded.frames || !decoded.frames.length) return null;
  if (decoded.frames.length === 1) return { width: decoded.width, height: decoded.height, pixels: decoded.frames[0] };
  const elapsed = Math.max(0, nowMs - startedMs);
  const total = decoded.delays.reduce((sum, delay) => sum + delay, 0) || 1;
  let phase = element.loop === false ? Math.min(elapsed, total - 1) : elapsed % total;
  let index = 0;
  while (index < decoded.frames.length - 1 && phase >= decoded.delays[index]) {
    phase -= decoded.delays[index];
    index += 1;
  }
  return { width: decoded.width, height: decoded.height, pixels: decoded.frames[index] };
}

function monoIcon(name, color) {
  const pattern = MONO_ICONS[name];
  if (!pattern) return null;
  const width = Math.max(...pattern.map((row) => row.length)), height = pattern.length;
  const pixels = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y++) for (let x = 0; x < pattern[y].length; x++) {
    if (pattern[y][x] !== "#") continue;
    const at = (y * width + x) * 4;
    pixels[at] = color[0]; pixels[at + 1] = color[1]; pixels[at + 2] = color[2]; pixels[at + 3] = 255;
  }
  return { width, height, pixels };
}

function sectionRange(meta, section) {
  const sections = Array.isArray(meta.sections) ? meta.sections : [];
  const selected = sections.find((item) => item.name === section)
    || sections.find((item) => item.name === "default");
  if (!selected) return [0, Math.max(0, Number(meta.frames) - 1)];
  return [Math.max(0, Number(selected.start) || 0), Math.max(0, Number(selected.end) || 0)];
}

function animationFile(meta, index) {
  const frameNumber = (Number(meta.start) || 0) + index;
  const number = Number(meta.pad) ? String(frameNumber).padStart(Number(meta.pad), "0") : String(frameNumber);
  return `${meta.prefix || "frame_"}${number}.png`;
}

function stockAnimationFrame(name, element, nowMs, startedMs, options) {
  const animations = options.animations || {};
  const meta = animations[name];
  if (!meta || !Number(meta.frames)) return null;
  const [first, last] = sectionRange(meta, element.section);
  const count = Math.max(1, last - first + 1), fps = Math.max(1, Number(meta.fps) || 30);
  let offset = Math.floor(Math.max(0, nowMs - startedMs) * fps / 1000);
  offset = element.loop === false ? Math.min(offset, count - 1) : offset % count;
  const frameIndex = first + offset;
  const root = path.resolve(options.animationRoot || ANIMATION_ROOT);
  const folder = path.resolve(root, name);
  if (!folder.startsWith(root + path.sep)) return null;
  const filename = path.resolve(folder, animationFile(meta, frameIndex));
  if (!filename.startsWith(folder + path.sep)) return null;
  if (!animationFrameCache.has(filename)) {
    let decoded = null;
    try { decoded = decodePng(fs.readFileSync(filename)); } catch (_) {}
    animationFrameCache.set(filename, decoded);
    while (animationFrameCache.size > 512) animationFrameCache.delete(animationFrameCache.keys().next().value);
  }
  return animationFrameCache.get(filename);
}

async function resolveImage(element, index, assets, options, nowMs, startedMs) {
  const animations = options.animations || {};
  const stockName = String(element.stock_path || element.name || "");
  if (element.type === "animation" || (stockName && animations[stockName])) {
    return stockAnimationFrame(stockName || String(element.path || ""), element, nowMs, startedMs, options);
  }
  if (element.stock_path) {
    const direct = monoIcon(String(element.stock_path), parseColor(element.color || "0xFFFFFFFF"));
    if (direct) return direct;
    const key = String(element.stock_path);
    const iconPath = STOCK_ICONS[key] || STOCK_ICONS[key.split("/").pop()];
    if (!iconPath) return null;
    const decoded = await decodeMedia(fs.readFileSync(iconPath), 14);
    return selectedFrame(decoded, element, nowMs, startedMs);
  }
  if (element.path && MONO_ICONS[element.path]) {
    return monoIcon(String(element.path), parseColor(element.color || "0xFFFFFFFF"));
  }
  const asset = element.path && assets[element.path];
  if (!asset || !Buffer.isBuffer(asset.buf)) return null;
  const decoded = await decodeMedia(asset.buf);
  return selectedFrame(decoded, element, nowMs, startedMs);
}

async function renderScreen(frame, assets, options = {}) {
  const display = Number(options.display) === 1 ? 1 : 0;
  const width = display ? 80 : 72, height = display ? 80 : 16;
  const pixels = new Float64Array(width * height * 3);
  const nowMs = Number(options.nowMs) || Date.now();
  const elementStartedAt = options.elementStartedAt || new Map();
  let clip = null;

  function put(x, y, color, opacity = 1) {
    x |= 0; y |= 0;
    if (x < 0 || x >= width || y < 0 || y >= height || (clip && (x < clip[0] || x > clip[1]))) return;
    const alpha = Math.max(0, Math.min(1, color[3] * opacity));
    const at = (y * width + x) * 3;
    pixels[at] = pixels[at] * (1 - alpha) + color[0] * alpha;
    pixels[at + 1] = pixels[at + 1] * (1 - alpha) + color[1] * alpha;
    pixels[at + 2] = pixels[at + 2] * (1 - alpha) + color[2] * alpha;
  }
  function mask(raster, x, y, color, opacity) {
    for (let row = 0; row < raster.height; row++) for (let column = 0; column < raster.width; column++) {
      if (raster.mask[row * raster.width + column]) put(x + column, y + row, color, opacity);
    }
  }
  function rectangle(element, opacity) {
    const w = Number(element.width) | 0, h = Number(element.height) | 0;
    if (w <= 0 || h <= 0) return;
    const [x, y] = anchor(element, w, h), colors = element.fill_colors || [];
    const first = colors.length ? parseColor(colors[0]) : null;
    const second = colors.length > 1 ? parseColor(colors[1]) : first;
    if (element.fill !== "none" && first) for (let row = 0; row < h; row++) for (let column = 0; column < w; column++) {
      const ratio = element.fill === "gradient_h" ? (w > 1 ? column / (w - 1) : 0)
        : element.fill === "gradient_v" ? (h > 1 ? row / (h - 1) : 0) : 0;
      const color = first.map((value, index) => value + (second[index] - value) * ratio);
      put(x + column, y + row, color, opacity);
    }
    const borderWidth = element.border_width == null ? 1 : Number(element.border_width) | 0;
    const border = parseColor(element.border_color || "0xFFFFFFFF");
    for (let edge = 0; edge < borderWidth; edge++) {
      for (let column = 0; column < w; column++) { put(x + column, y + edge, border, opacity); put(x + column, y + h - 1 - edge, border, opacity); }
      for (let row = 0; row < h; row++) { put(x + edge, y + row, border, opacity); put(x + w - 1 - edge, y + row, border, opacity); }
    }
  }
  function text(element, index, opacity) {
    let value = String(element.text == null ? "" : element.text);
    if (element.type === "countdown") {
      const timestamp = Number(element.timestamp) || 0;
      let seconds = element.direction === "time_since" ? nowMs / 1000 - timestamp : timestamp - nowMs / 1000;
      seconds = Math.max(0, seconds);
      const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), remainder = Math.floor(seconds % 60);
      value = (element.show_hours === "always" || hours > 0 ? String(hours).padStart(2, "0") + ":" : "")
        + String(minutes).padStart(2, "0") + ":" + String(remainder).padStart(2, "0");
    }
    const raster = rasterize(value, element.type === "countdown" ? "bold" : element.font);
    const color = parseColor(element.color || "0xFFFFFFFF");
    if (Number(element.scroll_rate) > 0 && raster.width > Number(element.width || width)) {
      const boxWidth = Number(element.width || (width - (Number(element.x) || 0)));
      const factor = ALIGN[element.align] || ALIGN.top_left;
      const boxX = (Number(element.x) || 0) - Math.round(boxWidth * factor[0]);
      const y = (Number(element.y) || 0) - Math.round(raster.height * factor[1]);
      const key = element && element.id != null ? `id:${String(element.id)}` : `anonymous:${index}`;
      const started = elementStartedAt.get(key) || nowMs;
      const delay = Math.max(0, Number(element.scroll_start_delay) || 0);
      const elapsed = Math.max(0, nowMs - started - delay) / 1000;
      const speed = Number(element.scroll_rate) / 60;
      const gap = Math.max(3, raster.space * 3), travel = raster.width + gap;
      const movingSeconds = travel / speed, pauseSeconds = Math.max(0, Number(element.scroll_repeat_delay) || 0) / 1000;
      const phase = elapsed % (movingSeconds + pauseSeconds);
      const offset = phase < movingSeconds ? Math.floor(phase * speed) : 0;
      clip = [boxX, boxX + boxWidth - 1];
      mask(raster, boxX - offset, y, color, opacity); mask(raster, boxX - offset + travel, y, color, opacity);
      clip = null;
      return;
    }
    const [x, y] = anchor(element, raster.width, raster.height);
    if (element.width) clip = [x, x + Number(element.width) - 1];
    mask(raster, x, y, color, opacity); clip = null;
  }
  function image(element, decoded, opacity) {
    if (!decoded) return;
    const [x, y] = anchor(element, decoded.width, decoded.height);
    for (let row = 0; row < decoded.height; row++) for (let column = 0; column < decoded.width; column++) {
      const at = (row * decoded.width + column) * 4;
      put(x + column, y + row, [decoded.pixels[at], decoded.pixels[at + 1], decoded.pixels[at + 2], decoded.pixels[at + 3] / 255], opacity);
    }
  }

  const elements = frame && Array.isArray(frame.elements) ? frame.elements : [];
  const resolvedImages = await Promise.all(elements.map(async (element, index) => {
    if (!element || typeof element !== "object") return null;
    if (!(element.type === "image" || element.type === "animation" || element.path || element.stock_path)) return null;
    const key = element.id != null ? `id:${String(element.id)}` : `anonymous:${index}`;
    const started = elementStartedAt.get(key) || Number(frame && frame.ts) || nowMs;
    return resolveImage(element, index, assets, options, nowMs, started);
  }));
  for (let index = 0; index < elements.length; index++) {
    const element = elements[index];
    if (!element || typeof element !== "object") continue;
    const target = element.display === "back" || Number(element.display) === 1 ? 1 : 0;
    if (target !== display) continue;
    if (Number(element.display_until) > 0 && nowMs / 1000 > Number(element.display_until)) continue;
    const key = element.id != null ? `id:${String(element.id)}` : `anonymous:${index}`;
    const started = elementStartedAt.get(key) || Number(frame && frame.ts) || nowMs;
    if (Number(element.timeout) > 0 && nowMs - started > Number(element.timeout) * 1000) continue;
    const opacity = element.opacity == null ? 1 : Math.max(0, Math.min(1, Number(element.opacity) / 100));
    if (element.type === "rectangle") rectangle(element, opacity);
    else if (element.type === "image" || element.type === "animation" || element.path || element.stock_path) image(element, resolvedImages[index], opacity);
    else text(element, index, opacity);
  }

  if (display === 1) {
    const output = Buffer.alloc(width * height);
    for (let index = 0; index < width * height; index++) {
      const at = index * 3;
      output[index] = Math.max(0, Math.min(255, Math.round(0.299 * pixels[at] + 0.587 * pixels[at + 1] + 0.114 * pixels[at + 2])));
    }
    return output;
  }
  const output = Buffer.alloc(width * height * 3);
  for (let index = 0; index < width * height; index++) {
    const at = index * 3;
    output[at] = Math.max(0, Math.min(255, Math.round(pixels[at + 2])));
    output[at + 1] = Math.max(0, Math.min(255, Math.round(pixels[at + 1])));
    output[at + 2] = Math.max(0, Math.min(255, Math.round(pixels[at])));
  }
  return output;
}

module.exports = { decodeMedia, decodePng, rasterize, renderScreen };
