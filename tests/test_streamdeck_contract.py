from __future__ import annotations

import json
import unittest
from pathlib import Path

from founder_os.automation import EXTERNAL_PRESENCE_STATES
from founder_os.config import DEFAULT_CONFIG
from founder_os.interaction import BRIDGE_CAPABILITIES, BRIDGE_VERSION


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "integrations" / "stream-deck" / "action-contract.json"


class StreamDeckContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_bridge_version_url_and_wire_capabilities_match_founderos(self) -> None:
        bridge = self.contract["bridge"]
        self.assertEqual(bridge["version"], BRIDGE_VERSION)
        self.assertEqual(
            bridge["baseUrl"],
            f"http://127.0.0.1:{DEFAULT_CONFIG['interaction']['listen_port']}",
        )
        self.assertEqual(set(bridge["capabilityMap"]), set(BRIDGE_CAPABILITIES.values()))
        self.assertEqual(bridge["capabilityMap"], {
            "event.open": "open",
            "event.snooze": "snooze",
            "event.acknowledge": "acknowledge",
            "permission.allow": "allow",
            "permission.deny": "deny",
            "presence.acquire": "presence.acquire",
            "presence.renew": "presence.renew",
            "presence.release": "presence.release",
            "presence.release_all": "presence.release_all",
        })

    def test_action_keys_and_holds_match_the_runtime_contract(self) -> None:
        actions = self.contract["actions"]
        interaction = DEFAULT_CONFIG["interaction"]
        self.assertEqual(actions["open"]["bridgeKey"], interaction["open_key"])
        self.assertEqual(actions["snooze"]["bridgeKey"], interaction["snooze_key"])
        self.assertEqual(actions["acknowledge"]["bridgeKey"], interaction["acknowledge_key"])
        self.assertEqual(actions["allow"]["bridgeKey"], interaction["allow_key"])
        self.assertEqual(actions["deny"]["bridgeKey"], interaction["deny_key"])
        self.assertGreaterEqual(actions["acknowledge"]["holdMilliseconds"], 1000)
        self.assertGreaterEqual(actions["allow"]["holdMilliseconds"], 1000)

    def test_presence_presets_respect_states_ttl_and_release_boundaries(self) -> None:
        presets = self.contract["actions"]["presence"]["presets"]
        indicator = DEFAULT_CONFIG["automations"]["calendar_busy_indicator"]
        minimum = float(indicator["lease_min_ttl_seconds"])
        maximum = float(indicator["lease_max_ttl_seconds"])
        for preset in presets.values():
            action = preset["action"]
            self.assertIn(action, {"acquire", "renew", "release", "release_all"})
            if action == "acquire":
                self.assertIn(preset["state"], EXTERNAL_PRESENCE_STATES)
            if action in {"acquire", "renew"}:
                self.assertGreaterEqual(float(preset["ttlSeconds"]), minimum)
                self.assertLessEqual(float(preset["ttlSeconds"]), maximum)
            if action == "release_all":
                self.assertEqual(set(preset), {"action"})
        self.assertEqual(
            self.contract["nativeProfileActions"]["end"],
            "com.yanndouchin.founderos-actions.presence#releaseManual",
        )


if __name__ == "__main__":
    unittest.main()
