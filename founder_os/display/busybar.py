"""Exact HTTP transport used by FounderOS against emulator or real hardware."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from founder_os.display.capabilities import FirmwareCapabilities, capabilities_for
from founder_os.display.raster import FontAtlas, parse_color, viewport_png
from founder_os.paths import ensure_private_directory, state_root


MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class DisplayError(RuntimeError):
    pass


class DisplayConflict(DisplayError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenCapture:
    display: int
    width: int
    height: int
    mode: str
    pixels: bytes

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise IndexError("screen coordinate is outside the display")
        channels = 3 if self.mode == "RGB" else 1
        offset = (y * self.width + x) * channels
        return tuple(self.pixels[offset : offset + channels])


class Display(Protocol):
    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None: ...
    def clear(self) -> None: ...


class BusyBarDisplay:
    def __init__(
        self,
        host: str = "127.0.0.1:8080",
        *,
        application_name: str = "founderos",
        priority: int = 90,
        timeout: float = 3.0,
        api_token: str = "",
        api_semver: str = "25.0.0",
        text_rendering: str = "native",
        font_atlas_path: str = "",
        text_capability_cache_path: str = "",
    ) -> None:
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("BUSY Bar host must be an HTTP or HTTPS endpoint")
        if parsed.username or parsed.password:
            raise ValueError("BUSY Bar host must not contain credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("BUSY Bar host must not contain a path, query, or fragment")
        if not 1 <= int(priority) <= 100:
            raise ValueError("BUSY Bar priority must be between 1 and 100")
        if float(timeout) <= 0:
            raise ValueError("BUSY Bar timeout must be positive")
        self.base_url = host.rstrip("/")
        self.application_name = application_name
        self.priority = int(priority)
        self.timeout = timeout
        self.api_token = api_token.strip()
        self.api_semver = api_semver.strip()
        self.text_rendering = text_rendering.strip().lower()
        if self.text_rendering not in {"auto", "native", "raster_non_ascii"}:
            raise ValueError("text_rendering must be auto, native, or raster_non_ascii")
        self._font_atlas_path = font_atlas_path or str(
            Path(__file__).resolve().parents[2] / "public" / "fonts" / "font-atlas.json"
        )
        self._text_capability_cache_path = Path(
            text_capability_cache_path or state_root() / "display-text-capabilities.json"
        ).expanduser()
        self._resolved_text_rendering: str | None = None
        self._font_atlas: FontAtlas | None = None
        if self.text_rendering != "auto":
            self._set_resolved_text_rendering(self.text_rendering)
        self._raster_texts: dict[str, dict[str, Any]] = {}

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        self.resolve_text_rendering()
        outgoing = self._rasterize_text_elements(elements)
        self._request(
            "POST",
            "/api/display/draw",
            body={
                "application_name": self.application_name,
                "priority": self.priority,
                "elements": outgoing,
            },
        )

    def clear(self) -> None:
        self._raster_texts.clear()
        self._request(
            "DELETE",
            "/api/display/draw",
            query={"application_name": self.application_name},
        )

    def version(self) -> str:
        payload = self._request("GET", "/api/version")
        return str(payload.get("api_semver", "unknown"))

    def firmware_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/status/firmware")

    def capabilities(self) -> FirmwareCapabilities:
        status = self.firmware_status()
        return capabilities_for(
            str(status.get("version", "unknown")),
            str(status.get("api_semver") or self.version()),
        )

    @property
    def resolved_text_rendering(self) -> str:
        return self._resolved_text_rendering or self.text_rendering

    def resolve_text_rendering(self) -> str:
        if self._resolved_text_rendering is not None:
            return self._resolved_text_rendering
        try:
            status = self.firmware_status()
            firmware = str(status.get("version", "unknown"))
            api_semver = str(status.get("api_semver") or self.version())
        except DisplayError:
            self._set_resolved_text_rendering("raster_non_ascii")
            return self.resolved_text_rendering
        cache_key = hashlib.sha256(
            f"{self.base_url}\0{firmware}\0{api_semver}".encode("utf-8")
        ).hexdigest()
        cached = self._read_text_capability_cache().get(cache_key, {})
        cached_mode = str(cached.get("mode", "")) if isinstance(cached, Mapping) else ""
        if cached_mode in {"native", "raster_non_ascii"}:
            self._set_resolved_text_rendering(cached_mode)
            return self.resolved_text_rendering

        from founder_os.display.verification import verify_french_glyphs

        self._set_resolved_text_rendering("native")
        try:
            native_result = verify_french_glyphs(self, self._font_atlas_path)
        except DisplayError:
            self._set_resolved_text_rendering("raster_non_ascii")
            return self.resolved_text_rendering
        if native_result.passed:
            self._write_text_capability_cache(cache_key, "native", firmware, api_semver)
            return self.resolved_text_rendering

        self._set_resolved_text_rendering("raster_non_ascii")
        raster_result = verify_french_glyphs(self, self._font_atlas_path)
        if not raster_result.passed:
            raise DisplayError("native and raster Unicode screen verification both failed")
        self._write_text_capability_cache(cache_key, "raster_non_ascii", firmware, api_semver)
        return self.resolved_text_rendering

    def _set_resolved_text_rendering(self, mode: str) -> None:
        self._resolved_text_rendering = mode
        self._font_atlas = FontAtlas(self._font_atlas_path) if mode == "raster_non_ascii" else None
        self._raster_texts = {}

    def _read_text_capability_cache(self) -> dict[str, Any]:
        try:
            if self._text_capability_cache_path.stat().st_size > 64 * 1024:
                return {}
            payload = json.loads(self._text_capability_cache_path.read_text(encoding="utf-8"))
            entries = payload.get("entries", {}) if isinstance(payload, Mapping) else {}
            return dict(entries) if isinstance(entries, Mapping) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write_text_capability_cache(
        self,
        cache_key: str,
        mode: str,
        firmware: str,
        api_semver: str,
    ) -> None:
        entries = self._read_text_capability_cache()
        entries[cache_key] = {
            "mode": mode,
            "firmware": firmware,
            "api_semver": api_semver,
            "verified_at": int(time.time()),
        }
        ordered = sorted(
            entries.items(),
            key=lambda item: int(item[1].get("verified_at", 0)) if isinstance(item[1], Mapping) else 0,
            reverse=True,
        )[:32]
        payload = json.dumps({"schema": 1, "entries": dict(ordered)}, ensure_ascii=False, sort_keys=True)
        directory = ensure_private_directory(self._text_capability_cache_path.parent)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".display-text-capabilities-",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self._text_capability_cache_path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def screen(self, display: int = 0) -> ScreenCapture:
        if display not in {0, 1}:
            raise ValueError("display must be 0 for front or 1 for back")
        raw = self._request_bytes("GET", "/api/screen", query={"display": display})
        encoded = raw.strip()
        if encoded.startswith(b'"') and encoded.endswith(b'"'):
            try:
                encoded = json.loads(encoded.decode("utf-8")).encode("ascii")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise DisplayError("GET /api/screen returned invalid base64") from exc
        try:
            device_pixels = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DisplayError("GET /api/screen returned invalid base64") from exc
        if display == 0:
            width, height, expected = 72, 16, 72 * 16 * 3
            if len(device_pixels) != expected:
                raise DisplayError(
                    f"GET /api/screen returned {len(device_pixels)} front bytes, expected {expected}"
                )
            rgb = bytearray(expected)
            for offset in range(0, expected, 3):
                rgb[offset] = device_pixels[offset + 2]
                rgb[offset + 1] = device_pixels[offset + 1]
                rgb[offset + 2] = device_pixels[offset]
            return ScreenCapture(display=0, width=width, height=height, mode="RGB", pixels=bytes(rgb))
        expected = 80 * 80
        if len(device_pixels) != expected:
            raise DisplayError(
                f"GET /api/screen returned {len(device_pixels)} back bytes, expected {expected}"
            )
        return ScreenCapture(display=1, width=80, height=80, mode="L", pixels=device_pixels)

    def upload_asset(self, filename: str, data: bytes) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename):
            raise ValueError("asset filename must be a safe basename")
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("asset exceeds the 8 MiB upload limit")
        self._request_bytes(
            "POST",
            "/api/assets/upload",
            query={"application_name": self.application_name, "file": filename},
            data=data,
            content_type="application/octet-stream",
        )

    def _rasterize_text_elements(
        self,
        elements: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._font_atlas is None:
            return [dict(element) for element in elements]
        now = time.monotonic()
        outgoing: list[dict[str, Any]] = []
        force_ids: set[str] = set()
        for index, source in enumerate(elements):
            element = dict(source)
            element_id = str(element.get("id", index))
            is_text = element.get("type") == "text" and "text" in element
            value = str(element.get("text", ""))
            if not is_text or value.isascii():
                self._raster_texts.pop(element_id, None)
                outgoing.append(element)
                continue
            signature = (
                value,
                str(element.get("font", "normal")),
                str(element.get("color", "")),
                int(element.get("width", 0) or 0),
            )
            previous = self._raster_texts.get(element_id)
            if previous is None or previous["signature"] != signature:
                mask = self._font_atlas.rasterize(value, str(element.get("font", "normal")))
                previous = {
                    "signature": signature,
                    "element": element,
                    "mask": mask,
                    "born": now,
                    "last_offset": None,
                    "slot": 0,
                }
                self._raster_texts[element_id] = previous
            else:
                previous["element"] = element
            force_ids.add(element_id)

        for element_id, state in list(self._raster_texts.items()):
            rendered = self._render_raster_text(element_id, state, now, force=element_id in force_ids)
            if rendered is not None:
                outgoing.append(rendered)
        return outgoing

    def _render_raster_text(
        self,
        element_id: str,
        state: dict[str, Any],
        now: float,
        *,
        force: bool,
    ) -> dict[str, Any] | None:
        element = state["element"]
        mask = state["mask"]
        box_width = int(element.get("width") or mask.width)
        scrolling = bool(float(element.get("scroll_rate", 0) or 0) > 0 and mask.width > box_width)
        offset = 0
        if scrolling:
            delay = max(0.0, float(element.get("scroll_start_delay", 0) or 0) / 1000)
            elapsed = max(0.0, now - state["born"] - delay)
            speed = float(element.get("scroll_rate", 0)) / 60
            travel = mask.width + max(3, mask.space_width * 3)
            moving = travel / speed
            pause = max(0.0, float(element.get("scroll_repeat_delay", 0) or 0) / 1000)
            phase = elapsed % (moving + pause)
            offset = int(phase * speed) if phase < moving else 0
        if not force and state["last_offset"] == offset:
            return None
        state["last_offset"] = offset
        state["slot"] = 1 - int(state["slot"])
        digest = hashlib.sha256(
            (self.application_name + "\0" + element_id).encode("utf-8")
        ).hexdigest()[:12]
        filename = f"raster-{digest}-{'b' if state['slot'] else 'a'}.png"
        png = viewport_png(
            mask,
            width=box_width if scrolling else mask.width,
            offset=offset,
            color=parse_color(str(element.get("color", "0xFFFFFFFF"))),
            repeat=scrolling,
        )
        self.upload_asset(filename, png)
        image: dict[str, Any] = {
            "id": element.get("id", element_id),
            "type": "image",
            "path": filename,
            "x": int(element.get("x", 0)),
            "y": int(element.get("y", 0)),
        }
        if not scrolling and element.get("align"):
            image["align"] = element["align"]
        for field in ("display", "display_until", "timeout", "opacity"):
            if field in element:
                image[field] = element[field]
        return image

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        raw = self._request_bytes(
            method,
            path,
            query=query,
            data=data,
            content_type="application/json" if data is not None else "",
        )
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DisplayError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise DisplayError(f"{method} {path} returned a non-object JSON response")
        return result

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str = "",
    ) -> bytes:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"X-API-Sem-Ver": self.api_semver}
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_token:
            headers["X-API-Token"] = self.api_token
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DisplayError(f"{method} {path} exceeded the response limit")
        except urllib.error.HTTPError as exc:
            error_type = DisplayConflict if exc.code == 409 else DisplayError
            raise error_type(f"{method} {path} returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DisplayError(f"{method} {path} failed: {reason}") from exc
        return raw


class RecordingDisplay:
    """In-memory display used by tests and dry runs."""

    def __init__(self) -> None:
        self.frames: list[list[dict[str, Any]]] = []
        self.clear_count = 0
        self.operations: list[tuple[str, Any]] = []

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        frame = [dict(element) for element in elements]
        self.frames.append(frame)
        self.operations.append(("draw", frame))

    def clear(self) -> None:
        self.clear_count += 1
        self.operations.append(("clear", None))
