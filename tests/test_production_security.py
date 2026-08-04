from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from founder_os.config import ConfigError, load_config
from founder_os.interaction import SignedInputListener, encode_signed_payload, signature_for, verify_signature


class ProductionSecurityTests(unittest.TestCase):
    def test_production_rejects_untrusted_emulator_input(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                overrides={
                    "runtime": {"environment": "production"},
                    "interaction": {"enabled": True, "mode": "emulator_sse"},
                }
            )

    def test_production_rejects_snapshot_state_inside_checkout(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                overrides={
                    "runtime": {"environment": "production"},
                    "connectors": {
                        "linear": {
                            "enabled": True,
                            "mode": "snapshot",
                            "snapshot_path": ".data/connectors/linear.json",
                        }
                    },
                }
            )

    def test_production_rejects_state_root_inside_checkout(self) -> None:
        checkout_state = str(Path.cwd().resolve() / ".runtime-state")
        with patch.dict(os.environ, {"FOUNDEROS_STATE_DIR": checkout_state}):
            with self.assertRaisesRegex(ConfigError, "outside the source checkout"):
                load_config(
                    overrides={
                        "runtime": {"environment": "production"},
                        "connectors": {"linear": {"enabled": True}},
                    }
                )

    def test_signed_input_requires_hmac_freshness_and_unique_nonce(self) -> None:
        secret = "closure-test-secret-0123456789-abcdef"
        listener = SignedInputListener(
            "127.0.0.1",
            0,
            secret,
            lambda event: "acknowledge",
            lambda: {"event_id": "linear:42", "request_id": "", "kind": "blocker"},
            max_clock_skew_seconds=30,
        )
        payload = {
            "key": "ok",
            "event_id": "linear:42",
            "request_id": "",
            "issued_at": int(time.time()),
            "nonce": "nonce-production-0001",
        }
        body = encode_signed_payload(payload)
        signature = signature_for(secret, body)
        self.assertTrue(verify_signature(secret, body, signature))
        self.assertFalse(verify_signature(secret, body + b" ", signature))
        event = listener._validate_payload(payload)
        self.assertTrue(event.trusted)
        self.assertEqual(event.event_id, "linear:42")
        with self.assertRaisesRegex(ValueError, "already been used"):
            listener._validate_payload(payload)
        with self.assertRaisesRegex(ValueError, "fields"):
            listener._validate_payload({**payload, "title": "must not cross the bridge"})

    def test_signed_bridge_serves_authenticated_context_and_presence_leases(self) -> None:
        secret = "presence-test-secret-0123456789-abcdef"
        commands: list[dict] = []
        listener = SignedInputListener(
            "127.0.0.1",
            0,
            secret,
            lambda event: "acknowledge",
            lambda: {
                "bridge_version": 2,
                "event_id": "",
                "request_id": "",
                "kind": "",
                "capabilities": ["presence.acquire"],
            },
            presence_callback=lambda command: commands.append(dict(command)) or {
                "action": command["action"],
                "aggregate": {"state": "focus", "busy": False},
            },
        )
        context = listener.context_provider()
        payload = {
            "action": "acquire",
            "lease_id": "streamdeck.focus",
            "state": "focus",
            "ttl_seconds": 300,
            "issued_at": int(time.time()),
            "nonce": "nonce-presence-0001",
        }
        body = encode_signed_payload(payload)
        self.assertTrue(verify_signature(secret, body, signature_for(secret, body)))
        command = listener._validate_presence_payload(payload)
        lease_result = listener.presence_callback(command)
        self.assertEqual(context["bridge_version"], 2)
        self.assertEqual(context["capabilities"], ["presence.acquire"])
        self.assertEqual(commands[0]["lease_id"], "streamdeck.focus")
        self.assertEqual(lease_result["aggregate"]["state"], "focus")

    def test_presence_release_all_has_no_lease_identifier_and_replay_is_rejected(self) -> None:
        secret = "presence-test-secret-0123456789-abcdef"
        listener = SignedInputListener(
            "127.0.0.1",
            0,
            secret,
            lambda event: None,
            lambda: {},
        )
        payload = {
            "action": "release_all",
            "issued_at": int(time.time()),
            "nonce": "nonce-presence-all-0001",
        }
        command = listener._validate_presence_payload(payload)
        self.assertEqual(command["action"], "release_all")
        self.assertNotIn("lease_id", command)
        with self.assertRaisesRegex(ValueError, "already been used"):
            listener._validate_presence_payload(payload)

    def test_private_defaults_do_not_point_into_the_checkout(self) -> None:
        config = load_config()
        checkout = Path.cwd().resolve()
        memory_path = Path(config["memory"]["path"]).expanduser().resolve()
        self.assertFalse(memory_path.is_relative_to(checkout))

    def test_presence_ttl_and_action_consumer_settings_are_bounded(self) -> None:
        with self.assertRaisesRegex(ConfigError, "lease_max_ttl_seconds"):
            load_config(
                overrides={
                    "automations": {
                        "calendar_busy_indicator": {
                            "lease_min_ttl_seconds": 60,
                            "lease_max_ttl_seconds": 30,
                        }
                    }
                }
            )
        with self.assertRaisesRegex(ConfigError, "action_consumer_poll_seconds"):
            load_config(
                overrides={"interaction": {"action_consumer_poll_seconds": 0}}
            )

    def test_input_action_keys_are_valid_and_unambiguous(self) -> None:
        with self.assertRaisesRegex(ConfigError, "URL-safe lowercase"):
            load_config(overrides={"interaction": {"open_key": "Not Valid"}})
        with self.assertRaisesRegex(ConfigError, "acknowledge, snooze, and open"):
            load_config(
                overrides={
                    "interaction": {
                        "acknowledge_key": "ok",
                        "snooze_key": "ok",
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
