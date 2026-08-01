from __future__ import annotations

import io
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from founder_os.connectors.base import ConnectorError
from founder_os.connectors.http_client import ConnectorHTTPError, request_json


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class HTTPClientTests(unittest.TestCase):
    def test_plain_http_is_limited_to_loopback(self) -> None:
        with self.assertRaisesRegex(ConnectorError, "only on loopback"):
            request_json("http://example.test/private")

    def test_response_size_is_bounded(self) -> None:
        with patch(
            "founder_os.connectors.http_client._OPENER.open",
            return_value=FakeResponse(b'{"value":"' + b"x" * 2000 + b'"}'),
        ):
            with self.assertRaisesRegex(ConnectorError, "response limit"):
                request_json("https://example.test/data", max_response_bytes=1024)

    def test_error_message_does_not_leak_query_parameters(self) -> None:
        headers = Message()
        error = urllib.error.HTTPError(
            "https://example.test/data?access_token=secret-value",
            400,
            "Bad Request",
            headers,
            io.BytesIO(b"invalid request"),
        )
        with patch("founder_os.connectors.http_client._OPENER.open", side_effect=error):
            with self.assertRaises(ConnectorHTTPError) as caught:
                request_json("https://example.test/data?access_token=secret-value", retries=0)
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertIn("https://example.test/data", str(caught.exception))

    def test_error_message_does_not_leak_remote_response_body(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test/data",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(b'api_key="private-value"'),
        )
        with patch("founder_os.connectors.http_client._OPENER.open", side_effect=error):
            with self.assertRaises(ConnectorHTTPError) as caught:
                request_json("https://example.test/data", retries=0)
        self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(str(caught.exception), "GET https://example.test/data returned HTTP 400")

    def test_retryable_status_has_a_bounded_retry(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        error = urllib.error.HTTPError(
            "https://example.test/data",
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(b"slow down"),
        )
        with patch(
            "founder_os.connectors.http_client._OPENER.open",
            side_effect=(error, FakeResponse(b'{"ok":true}')),
        ) as opened:
            self.assertEqual(request_json("https://example.test/data", retries=1), {"ok": True})
        self.assertEqual(opened.call_count, 2)

    def test_expired_total_deadline_stops_before_network_io(self) -> None:
        with patch("founder_os.connectors.http_client.time.monotonic", return_value=10.0), patch(
            "founder_os.connectors.http_client._OPENER.open"
        ) as opened:
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                request_json(
                    "https://example.test/data",
                    retries=1,
                    deadline_monotonic=9.0,
                )
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
