from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from founder_os.connectors.base import ConnectorConfigurationError
from founder_os.connectors.google_oauth import GoogleAccessTokenProvider


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class GoogleOAuthTests(unittest.TestCase):
    def test_refresh_token_is_cached_in_memory_and_can_be_invalidated(self) -> None:
        environment = {
            "TEST_GOOGLE_REFRESH": "refresh-value",
            "TEST_GOOGLE_CLIENT": "client-value",
            "TEST_GOOGLE_SECRET": "secret-value",
        }
        config = {
            "refresh_token_env": "TEST_GOOGLE_REFRESH",
            "client_id_env": "TEST_GOOGLE_CLIENT",
            "client_secret_env": "TEST_GOOGLE_SECRET",
            "request_timeout_seconds": 2,
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "founder_os.connectors.google_oauth.request_json",
            side_effect=(
                {"access_token": "first-access", "expires_in": 3600},
                {"access_token": "second-access", "expires_in": 3600},
            ),
        ) as request:
            provider = GoogleAccessTokenProvider(config)
            self.assertEqual(provider.token(NOW), "first-access")
            self.assertEqual(provider.token(NOW), "first-access")
            self.assertEqual(request.call_count, 1)
            provider.invalidate()
            self.assertEqual(provider.token(NOW), "second-access")
            self.assertEqual(request.call_count, 2)

    def test_google_connector_requires_a_complete_credential_strategy(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConnectorConfigurationError):
            GoogleAccessTokenProvider({"access_token_env": "MISSING_ACCESS_TOKEN"})


if __name__ == "__main__":
    unittest.main()
