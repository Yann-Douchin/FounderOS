from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from apps import founderosctl
from founder_os.connectors.base import ConnectorConfigurationError
from founder_os.connectors.linear_oauth import LinearAccessTokenProvider
from founder_os.health import HealthReporter
from founder_os.oauth import OAuthFlowError, _CallbackServer, authorize_google, authorize_linear
from founder_os.secrets import MacOSKeychainStore, MemorySecretStore, SecretError, SecretResolver
from founder_os.service import (
    LaunchAgentStatus,
    ServiceError,
    emulator_launch_agent_payload,
    launch_agent_payload,
    service_status,
)


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class FakeKeychainAPI:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str:
        from founder_os.secrets import SecretNotFound

        try:
            return self.values[(service, account)]
        except KeyError as exc:
            raise SecretNotFound(account) from exc

    def set(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete(self, service: str, account: str) -> bool:
        return self.values.pop((service, account), None) is not None


class FakeCallback:
    redirect_uri = "http://127.0.0.1:8766/oauth/callback"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def wait_for_code(self, expected_state: str, *, timeout_seconds: float) -> str:
        self.expected_state = expected_state
        self.timeout_seconds = timeout_seconds
        return "authorization-code"


class AutonomousServiceTests(unittest.TestCase):
    def test_keychain_store_never_needs_a_secret_command_argument(self) -> None:
        api = FakeKeychainAPI()
        store = MacOSKeychainStore("com.founderos.test", api=api)
        store.set("SLACK_BOT_TOKEN", "xoxb-private")
        self.assertEqual(store.get("SLACK_BOT_TOKEN"), "xoxb-private")
        self.assertTrue(store.delete("SLACK_BOT_TOKEN"))
        self.assertFalse(store.delete("SLACK_BOT_TOKEN"))

    def test_keychain_store_enforces_its_write_allowlist(self) -> None:
        store = MacOSKeychainStore(
            "com.founderos.test",
            api=FakeKeychainAPI(),
            accounts=["SLACK_BOT_TOKEN"],
        )
        with self.assertRaisesRegex(SecretError, "not allowlisted"):
            store.set("GOOGLE_REFRESH_TOKEN", "private")

    def test_resolver_keeps_keychain_values_out_of_process_environment(self) -> None:
        environment: dict[str, str] = {}
        store = MemorySecretStore({"SLACK_BOT_TOKEN": "xoxb-private"})
        resolver = SecretResolver(store, accounts=["SLACK_BOT_TOKEN"], environ=environment)
        self.assertEqual(resolver.get("SLACK_BOT_TOKEN"), "xoxb-private")
        self.assertEqual(environment, {})

    def test_persistent_store_wins_over_a_stale_environment_token(self) -> None:
        store = MemorySecretStore({"LINEAR_REFRESH_TOKEN": "rotated-refresh"})
        resolver = SecretResolver(
            store,
            accounts=["LINEAR_REFRESH_TOKEN"],
            environ={"LINEAR_REFRESH_TOKEN": "stale-refresh"},
        )
        self.assertEqual(resolver.get("LINEAR_REFRESH_TOKEN"), "rotated-refresh")

    def test_keychain_allowlist_blocks_unapproved_environment_fallback(self) -> None:
        resolver = SecretResolver(
            MemorySecretStore(),
            accounts=["SLACK_BOT_TOKEN"],
            environ={"UNAPPROVED_TOKEN": "must-not-resolve"},
        )
        self.assertEqual(resolver.get("UNAPPROVED_TOKEN"), "")

    def test_persistent_provider_never_uses_an_ambient_secret_fallback(self) -> None:
        resolver = SecretResolver(
            MemorySecretStore(),
            accounts=["SLACK_BOT_TOKEN"],
            environ={"SLACK_BOT_TOKEN": "ambient-token"},
        )
        self.assertEqual(resolver.get("SLACK_BOT_TOKEN"), "")

    def test_linear_refresh_rotates_and_persists_before_returning_access(self) -> None:
        store = MemorySecretStore({
            "LINEAR_CLIENT_ID": "client-id",
            "LINEAR_REFRESH_TOKEN": "refresh-one",
        })
        resolver = SecretResolver(
            store,
            accounts=["LINEAR_CLIENT_ID", "LINEAR_REFRESH_TOKEN"],
            environ={},
        )
        config = {
            "auth_scheme": "bearer",
            "oauth_refresh": True,
            "refresh_token_env": "LINEAR_REFRESH_TOKEN",
            "client_id_env": "LINEAR_CLIENT_ID",
            "client_secret_env": "LINEAR_CLIENT_SECRET",
        }
        with patch(
            "founder_os.connectors.linear_oauth.request_json",
            return_value={
                "access_token": "access-one",
                "refresh_token": "refresh-two",
                "expires_in": 3600,
            },
        ) as request:
            provider = LinearAccessTokenProvider(config, secrets=resolver)
            self.assertEqual(provider.authorization(NOW), "Bearer access-one")
            self.assertEqual(provider.authorization(NOW), "Bearer access-one")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(store.get("LINEAR_REFRESH_TOKEN"), "refresh-two")

    def test_linear_refresh_rejects_a_nonpersistent_environment_strategy(self) -> None:
        resolver = SecretResolver(
            accounts=["LINEAR_CLIENT_ID", "LINEAR_REFRESH_TOKEN"],
            environ={
                "LINEAR_CLIENT_ID": "client-id",
                "LINEAR_REFRESH_TOKEN": "refresh-one",
            },
        )
        with self.assertRaisesRegex(ConnectorConfigurationError, "persistent"):
            LinearAccessTokenProvider(
                {
                    "auth_scheme": "bearer",
                    "oauth_refresh": True,
                    "refresh_token_env": "LINEAR_REFRESH_TOKEN",
                    "client_id_env": "LINEAR_CLIENT_ID",
                },
                secrets=resolver,
            )

    def test_google_oauth_stores_only_durable_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client_path = Path(folder) / "client.json"
            client_path.write_text(json.dumps({
                "installed": {
                    "client_id": "google-client",
                    "client_secret": "google-secret",
                }
            }), encoding="utf-8")
            store = MemorySecretStore()
            with patch("founder_os.oauth._CallbackServer", return_value=FakeCallback()), patch(
                "founder_os.oauth._pkce_pair", return_value=("verifier", "challenge")
            ):
                result = authorize_google(
                    client_path,
                    store,
                    browser_opener=lambda _: True,
                    token_request=lambda *_, **__: {
                        "access_token": "short-lived",
                        "refresh_token": "google-refresh",
                        "scope": (
                            "https://www.googleapis.com/auth/gmail.readonly "
                            "https://www.googleapis.com/auth/calendar.events.readonly"
                        ),
                    },
                )
        self.assertEqual(result.provider, "google")
        self.assertEqual(store.get("GOOGLE_REFRESH_TOKEN"), "google-refresh")
        self.assertNotIn("GOOGLE_ACCESS_TOKEN", store.values)

    def test_google_oauth_rejects_a_partial_scope_grant_before_storage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client_path = Path(folder) / "client.json"
            client_path.write_text(json.dumps({
                "installed": {
                    "client_id": "google-client",
                    "client_secret": "google-secret",
                }
            }), encoding="utf-8")
            store = MemorySecretStore()
            with patch("founder_os.oauth._CallbackServer", return_value=FakeCallback()):
                with self.assertRaisesRegex(OAuthFlowError, "every requested"):
                    authorize_google(
                        client_path,
                        store,
                        browser_opener=lambda _: True,
                        token_request=lambda *_, **__: {
                            "refresh_token": "google-refresh",
                            "scope": "https://www.googleapis.com/auth/gmail.readonly",
                        },
                    )
        self.assertEqual(store.values, {})

    def test_google_oauth_rejects_nonallowlisted_credential_accounts_before_authorizing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client_path = Path(folder) / "client.json"
            client_path.write_text(json.dumps({
                "installed": {
                    "client_id": "google-client",
                    "client_secret": "google-secret",
                }
            }), encoding="utf-8")
            store = MemorySecretStore(
                accounts=["GOOGLE_CLIENT_ID", "GOOGLE_REFRESH_TOKEN"],
            )
            opened: list[str] = []
            with self.assertRaisesRegex(OAuthFlowError, "GOOGLE_CLIENT_SECRET"):
                authorize_google(
                    client_path,
                    store,
                    browser_opener=lambda url: opened.append(url),
                )
        self.assertEqual(opened, [])

    def test_linear_oauth_uses_read_scope_and_pkce(self) -> None:
        store = MemorySecretStore()
        captured: dict = {}

        def opener(url: str) -> bool:
            captured["url"] = url
            return True

        def token_request(*args, **kwargs):
            captured["form"] = kwargs["form"]
            return {"access_token": "access", "refresh_token": "refresh", "scope": "read"}

        with patch("founder_os.oauth._CallbackServer", return_value=FakeCallback()), patch(
            "founder_os.oauth._pkce_pair", return_value=("verifier", "challenge")
        ):
            authorize_linear(
                "linear-client",
                store,
                browser_opener=opener,
                token_request=token_request,
            )
        self.assertIn("scope=read", captured["url"])
        self.assertEqual(captured["form"]["code_verifier"], "verifier")
        self.assertEqual(store.get("LINEAR_REFRESH_TOKEN"), "refresh")

    def test_oauth_denial_does_not_repeat_provider_description(self) -> None:
        callback = object.__new__(_CallbackServer)
        callback.result = {
            "error": "access_denied",
            "error_description": "Private administrator policy detail",
        }
        with self.assertRaises(OAuthFlowError) as raised:
            callback.wait_for_code("state", timeout_seconds=10)
        self.assertIn("access_denied", str(raised.exception))
        self.assertNotIn("Private", str(raised.exception))

    def test_health_heartbeat_contains_no_event_content_or_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.json"
            reporter = HealthReporter(path, heartbeat_seconds=1)
            reporter.publish(
                selected_source="gmail",
                event_count=2,
                connector_health={
                    "gmail": {
                        "status": "degraded",
                        "critical": True,
                        "failures": 1,
                        "last_event_count": 2,
                        "last_success_at": NOW.isoformat(),
                        "last_error": "Private customer subject",
                    }
                },
                displayed=False,
                display_error="Private display detail",
                now=NOW,
                force=True,
            )
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertNotIn("Private", raw)
        self.assertEqual(payload["selected_source"], "gmail")
        self.assertTrue(payload["connectors"]["gmail"]["error_present"])

    def test_unchanged_frame_remains_display_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.json"
            reporter = HealthReporter(path, heartbeat_seconds=1)
            reporter.publish(
                event_count=0,
                connector_health={
                    "linear": {
                        "status": "healthy",
                        "critical": True,
                        "failures": 0,
                        "last_event_count": 0,
                    }
                },
                displayed=False,
                display_error="",
                now=NOW,
                force=True,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["display"]["healthy"])
        self.assertFalse(payload["display"]["updated"])

    def test_launch_agent_contains_only_secret_names_and_safe_environment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "apps").mkdir()
            (root / "apps" / "founderos.py").write_text("", encoding="utf-8")
            config = root / "founderos.local.json"
            config.write_text("{}", encoding="utf-8")
            payload = launch_agent_payload(
                repository=root,
                config_path=config,
                python_executable=sys.executable,
                runtime_state_root=root / "state",
            )
        serialized = json.dumps(payload)
        self.assertNotIn("TOKEN", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertEqual(payload["Umask"], 0o077)
        self.assertTrue(payload["RunAtLoad"])

    def test_emulator_launch_agent_is_loopback_only_and_contains_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = root / "server.js"
            node = root / "node"
            web_index = root / "web" / "dist" / "index.html"
            web_index.parent.mkdir(parents=True)
            server.write_text("", encoding="utf-8")
            node.write_text("", encoding="utf-8")
            web_index.write_text("", encoding="utf-8")
            payload = emulator_launch_agent_payload(
                repository=root,
                node_executable=node,
                python_executable=sys.executable,
                runtime_state_root=root / "state",
            )
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["BUSY_HOST"], "127.0.0.1")
        self.assertEqual(environment["BUSY_DATA_DIR"], str((root / "state" / "emulator").resolve()))
        self.assertEqual(environment["PORT"], "8080")
        self.assertNotIn("TOKEN", json.dumps(payload))
        self.assertEqual(payload["Umask"], 0o077)

    def test_clipboard_secret_import_reads_without_printing_and_clears(self) -> None:
        read_result = type("Result", (), {"returncode": 0, "stdout": "xoxb-private\n"})()
        clear_result = type("Result", (), {"returncode": 0, "stdout": ""})()
        with patch.object(founderosctl.subprocess, "run", side_effect=[read_result, clear_result]) as run:
            self.assertEqual(founderosctl._read_clipboard_secret(), "xoxb-private")
            founderosctl._clear_clipboard()
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/pbpaste"])
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/pbcopy"])
        self.assertEqual(run.call_args_list[1].kwargs["input"], "")

    def test_emulator_service_rejects_an_unbuilt_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "server.js").write_text("", encoding="utf-8")
            node = root / "node"
            node.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ServiceError, "npm run build"):
                emulator_launch_agent_payload(
                    repository=root,
                    node_executable=node,
                    python_executable=sys.executable,
                    runtime_state_root=root / "state",
                )

    def test_service_status_rejects_a_heartbeat_from_an_old_process(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            health = Path(folder) / "health.json"
            health.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
                "pid": 111,
            }), encoding="utf-8")
            with patch(
                "founder_os.service.launch_agent_status",
                return_value=LaunchAgentStatus(True, 222, "running"),
            ):
                status = service_status(health_path=health)
        self.assertEqual(status.health, "process_mismatch")
        self.assertEqual(status.health_pid, 111)

    def test_service_status_reports_display_and_connector_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            health = Path(folder) / "health.json"
            health.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
                "pid": 222,
                "display": {"healthy": True},
                "connectors": {
                    "linear": {"status": "healthy", "critical": True},
                    "slack": {"status": "degraded", "critical": True},
                },
            }), encoding="utf-8")
            with patch(
                "founder_os.service.launch_agent_status",
                return_value=LaunchAgentStatus(True, 222, "running"),
            ):
                status = service_status(health_path=health)
        self.assertEqual(status.health, "running")
        self.assertTrue(status.display_healthy)
        self.assertFalse(status.connectors_healthy)


if __name__ == "__main__":
    unittest.main()
