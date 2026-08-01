from __future__ import annotations

import unittest

from apps.founderos_input import _require_loopback


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


if __name__ == "__main__":
    unittest.main()
