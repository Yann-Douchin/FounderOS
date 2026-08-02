from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from founder_os.config import ConfigError, load_config
from founder_os.secrets import MemorySecretStore, SecretResolver


class ProductionConfigTests(unittest.TestCase):
    def test_linear_portfolio_requires_an_explicit_team_allowlist(self) -> None:
        with self.assertRaisesRegex(ConfigError, "team_keys"):
            load_config(overrides={
                "connectors": {"linear": {"enabled": True, "scope": "portfolio", "team_keys": []}}
            })

    def test_production_requires_a_real_connector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "active connector"):
            load_config(overrides={"runtime": {"environment": "production"}})
        with self.assertRaisesRegex(ConfigError, "demo connector"):
            load_config(
                overrides={
                    "runtime": {"environment": "production"},
                    "connectors": {"demo": {"enabled": True}},
                }
            )

    def test_remote_plain_http_requires_an_explicit_risk_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"FOUNDEROS_STATE_DIR": folder, "TEST_BUSY_TOKEN": "device-token"},
            clear=False,
        ):
            overrides = {
                "runtime": {"environment": "production"},
                "display": {
                    "host": "http://192.0.2.10:8080",
                    "api_token_env": "TEST_BUSY_TOKEN",
                },
                "connectors": {
                    "linear": {
                        "enabled": True,
                        "mode": "snapshot",
                        "snapshot_path": str(Path(folder) / "connectors" / "linear.json"),
                    }
                },
            }
            with self.assertRaisesRegex(ConfigError, "allow_insecure_http"):
                load_config(overrides=overrides)
            overrides["display"]["allow_insecure_http"] = True
            config = load_config(overrides=overrides)
            self.assertEqual(config["runtime"]["environment"], "production")

    def test_keychain_provider_requires_explicit_connector_allowlist(self) -> None:
        resolver = SecretResolver(MemorySecretStore(), accounts=[])
        with patch("founder_os.config.build_secret_resolver", return_value=resolver):
            with self.assertRaisesRegex(ConfigError, "Slack token must be listed"):
                load_config(overrides={
                    "secrets": {"provider": "macos_keychain", "accounts": []},
                    "connectors": {
                        "slack": {
                            "enabled": True,
                            "channel_ids": ["C123"],
                        }
                    },
                })

    def test_workspace_connectors_require_explicit_data_boundaries(self) -> None:
        cases = (
            ("notion", "database_ids"),
            ("drive", "folder_ids"),
            ("sheets", "spreadsheets"),
        )
        for connector, expected in cases:
            with self.subTest(connector=connector):
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(overrides={"connectors": {connector: {"enabled": True}}})

    def test_connector_allowlists_reject_query_and_path_injection(self) -> None:
        cases = (
            {"notion": {"database_ids": ["id' or true"]}},
            {"drive": {"folder_ids": ["id' in parents"]}},
            {"github": {"repositories": ["owner/repo/extra"]}},
            {"home_assistant": {"entities": ["person.yann"]}},
        )
        for connectors in cases:
            with self.subTest(connectors=connectors):
                with self.assertRaises(ConfigError):
                    load_config(overrides={"connectors": connectors})


if __name__ == "__main__":
    unittest.main()
