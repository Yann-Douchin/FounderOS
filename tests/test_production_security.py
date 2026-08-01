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

    def test_private_defaults_do_not_point_into_the_checkout(self) -> None:
        config = load_config()
        checkout = Path.cwd().resolve()
        memory_path = Path(config["memory"]["path"]).expanduser().resolve()
        self.assertFalse(memory_path.is_relative_to(checkout))


if __name__ == "__main__":
    unittest.main()
