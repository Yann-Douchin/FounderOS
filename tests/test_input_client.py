from __future__ import annotations

import unittest
import stat
import tempfile
import time
from pathlib import Path

from apps.founderos_input import (
    _require_loopback,
    local_socket_available,
    read_local_context,
    send_local,
)
from founder_os.interaction import InputEvent, LocalInputListener


class InputClientTests(unittest.TestCase):
    def test_secret_bearing_client_rejects_remote_destinations(self) -> None:
        for url in (
            "https://example.test:8765",
            "http://192.0.2.10:8765",
            "http://user:password@127.0.0.1:8765",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _require_loopback(url)
        _require_loopback("http://127.0.0.1:8765")
        _require_loopback("http://localhost:8765")

    def test_private_local_bridge_round_trip_never_uses_a_secret(self) -> None:
        received: list[InputEvent] = []
        presence: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="founderos-local-input-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            socket_path = root / "founderos-input.sock"
            listener = LocalInputListener(
                socket_path,
                lambda event: received.append(event) or "acknowledge",
                lambda: {
                    "bridge_version": 2,
                    "event_id": "linear:42",
                    "request_id": "",
                    "kind": "blocker",
                    "capabilities": ["event.acknowledge", "presence.acquire"],
                },
                presence_callback=lambda command: presence.append(dict(command)) or {
                    "action": command["action"],
                },
                allowed_keys={"ok", "back", "custom"},
            )
            listener.start()
            try:
                self.assertTrue(local_socket_available(socket_path))
                self.assertEqual(stat.S_IMODE(socket_path.lstat().st_mode), 0o600)
                context = read_local_context(socket_path)
                self.assertEqual(context["event_id"], "linear:42")
                event_result = send_local(
                    socket_path,
                    "input",
                    {
                        "key": "ok",
                        "event_id": "linear:42",
                        "request_id": "",
                        "issued_at": int(time.time()),
                        "nonce": "nonce-local-event-0001",
                    },
                )
                presence_result = send_local(
                    socket_path,
                    "presence",
                    {
                        "action": "acquire",
                        "lease_id": "streamdeck.focus",
                        "state": "focus",
                        "ttl_seconds": 300,
                        "issued_at": int(time.time()),
                        "nonce": "nonce-local-presence-0001",
                    },
                )
            finally:
                listener.close()
            self.assertEqual(event_result["action"], "acknowledge")
            self.assertEqual(received[0].transport, "local_socket")
            self.assertTrue(received[0].trusted)
            self.assertEqual(presence_result["presence"]["action"], "acquire")
            self.assertEqual(presence[0]["lease_id"], "streamdeck.focus")
            self.assertFalse(socket_path.exists())

    def test_local_bridge_rejects_non_private_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founderos-local-input-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            listener = LocalInputListener(
                root / "founderos-input.sock",
                lambda event: None,
                lambda: {},
                allowed_keys={"ok"},
            )
            with self.assertRaisesRegex(OSError, "private"):
                listener.start()

    def test_local_client_rejects_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founderos-local-input-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            impostor = root / "founderos-input.sock"
            impostor.touch(mode=0o600)
            with self.assertRaisesRegex(ValueError, "socket"):
                local_socket_available(impostor)

    def test_closing_an_unstarted_listener_cannot_remove_an_active_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founderos-local-input-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            socket_path = root / "founderos-input.sock"
            active = LocalInputListener(
                socket_path,
                lambda event: "acknowledge",
                lambda: {"event_id": "linear:active"},
                allowed_keys={"ok"},
            )
            inactive = LocalInputListener(
                socket_path,
                lambda event: None,
                lambda: {},
                allowed_keys={"ok"},
            )
            active.start()
            try:
                inactive.close()
                self.assertTrue(local_socket_available(socket_path))
                self.assertEqual(read_local_context(socket_path)["event_id"], "linear:active")
            finally:
                active.close()


if __name__ == "__main__":
    unittest.main()
