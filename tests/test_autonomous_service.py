from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps import founderosctl
from founder_os.connectors.base import ConnectorConfigurationError
from founder_os.connectors.linear_oauth import LinearAccessTokenProvider
from founder_os.health import HealthReporter
from founder_os.oauth import OAuthFlowError, _CallbackServer, authorize_google, authorize_linear
from founder_os.secrets import MacOSKeychainStore, MemorySecretStore, SecretError, SecretResolver
from founder_os.service import (
    EMULATOR_LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_LABEL,
    LaunchAgentStatus,
    LaunchAgentSnapshot,
    RuntimeDeployment,
    ServiceError,
    emulator_launch_agent_payload,
    install_launch_agent,
    launch_agent_payload,
    service_status,
    stage_runtime_bundle,
)


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


def make_runtime_source(root: Path) -> tuple[Path, Path]:
    repository = root / "repository"
    for directory in ("founder_os", "apps", "public", "web/dist", "node_modules/sharp"):
        (repository / directory).mkdir(parents=True, exist_ok=True)
    (repository / "founder_os" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "apps" / "founderos.py").write_text("print('ready')\n", encoding="utf-8")
    (repository / "public" / "font.txt").write_text("écran", encoding="utf-8")
    (repository / "web" / "dist" / "index.html").write_text("<main>FounderOS</main>", encoding="utf-8")
    (repository / "server.js").write_text("'use strict';\n", encoding="utf-8")
    (repository / "screen_renderer.js").write_text("'use strict';\n", encoding="utf-8")
    (repository / "package.json").write_text('{"dependencies":{"sharp":"0.35.3"}}', encoding="utf-8")
    (repository / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    (repository / "node_modules" / "sharp" / "package.json").write_text('{"name":"sharp"}', encoding="utf-8")
    config = root / "founderos.local.json"
    config.write_text('{"runtime":{"timezone":"Europe/Madrid"}}', encoding="utf-8")
    return repository, config


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

    def test_runtime_bundle_is_private_immutable_and_reused_by_digest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, config = make_runtime_source(root)
            cache = repository / "apps" / "__pycache__"
            cache.mkdir()
            (cache / "founderos.pyc").write_bytes(b"ignored")
            animation = repository / "public" / "animations" / "stock"
            animation.mkdir(parents=True)
            (animation / "frame.png").write_bytes(b"unused stock frame")
            first = stage_runtime_bundle(
                repository=repository,
                config_path=config,
                runtime_state_root=root / "state",
            )
            second = stage_runtime_bundle(
                repository=repository,
                config_path=config,
                runtime_state_root=root / "state",
            )
            root_mode = stat.S_IMODE(first.root.stat().st_mode)
            config_mode = stat.S_IMODE(first.config_path.stat().st_mode)
            source_mode = stat.S_IMODE((first.root / "apps" / "founderos.py").stat().st_mode)
            cache_exists = (first.root / "apps" / "__pycache__").exists()
            stock_animation_exists = (first.root / "public" / "animations" / "stock" / "frame.png").exists()
            screen_renderer_exists = (first.root / "screen_renderer.js").exists()
            sharp_exists = (first.root / "node_modules" / "sharp" / "package.json").exists()
            staged_config_name = first.config_path.name
        self.assertEqual(first, second)
        self.assertEqual(root_mode, 0o700)
        self.assertEqual(config_mode, 0o600)
        self.assertEqual(source_mode, 0o600)
        self.assertFalse(cache_exists)
        self.assertTrue(stock_animation_exists)
        self.assertTrue(screen_renderer_exists)
        self.assertTrue(sharp_exists)
        self.assertEqual(staged_config_name, "founderos.runtime.json")

    def test_runtime_bundle_digest_changes_with_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, config = make_runtime_source(root)
            first = stage_runtime_bundle(
                repository=repository,
                config_path=config,
                runtime_state_root=root / "state",
            )
            config.write_text('{"runtime":{"timezone":"UTC"}}', encoding="utf-8")
            second = stage_runtime_bundle(
                repository=repository,
                config_path=config,
                runtime_state_root=root / "state",
            )
        self.assertNotEqual(first.deployment_id, second.deployment_id)
        self.assertNotEqual(first.root, second.root)

    def test_runtime_bundle_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, config = make_runtime_source(root)
            (repository / "apps" / "linked.py").symlink_to(repository / "apps" / "founderos.py")
            with self.assertRaisesRegex(ServiceError, "symbolic link"):
                stage_runtime_bundle(
                    repository=repository,
                    config_path=config,
                    runtime_state_root=root / "state",
                )

    def test_launch_agent_install_restores_prior_plist_on_launchctl_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, config = make_runtime_source(root)
            destination = root / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"prior definition")
            bootstrap_attempts = 0

            def launchctl(*arguments: str, **_: object) -> None:
                nonlocal bootstrap_attempts
                if arguments[0] == "bootstrap":
                    bootstrap_attempts += 1
                if arguments[0] == "bootstrap" and bootstrap_attempts <= 3:
                    raise ServiceError("simulated bootstrap failure")

            with patch("founder_os.service.sys.platform", "darwin"), patch.object(
                Path, "home", return_value=root
            ), patch(
                "founder_os.service.launch_agent_status",
                return_value=LaunchAgentStatus(True, 123, "running"),
            ), patch("founder_os.service._run_launchctl", side_effect=launchctl), patch(
                "founder_os.service.time.sleep"
            ):
                with self.assertRaisesRegex(ServiceError, "simulated bootstrap failure"):
                    install_launch_agent(
                        repository=repository,
                        config_path=config,
                        python_executable=sys.executable,
                        runtime_state_root=root / "state",
                    )
            restored = destination.read_bytes()
        self.assertEqual(bootstrap_attempts, 4)
        self.assertEqual(restored, b"prior definition")

    def test_launch_agent_install_retries_transient_bootstrap_failures(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, config = make_runtime_source(root)
            bootstrap_attempts = 0

            def launchctl(*arguments: str, **_: object) -> None:
                nonlocal bootstrap_attempts
                if arguments[0] != "bootstrap":
                    return
                bootstrap_attempts += 1
                if bootstrap_attempts < 3:
                    raise ServiceError("simulated transient bootstrap failure")

            with patch("founder_os.service.sys.platform", "darwin"), patch.object(
                Path, "home", return_value=root
            ), patch(
                "founder_os.service.launch_agent_status",
                return_value=LaunchAgentStatus(False, None, "not loaded"),
            ), patch("founder_os.service._run_launchctl", side_effect=launchctl), patch(
                "founder_os.service.time.sleep"
            ):
                destination = install_launch_agent(
                    repository=repository,
                    config_path=config,
                    python_executable=sys.executable,
                    runtime_state_root=root / "state",
                )
            installed = destination.exists()
        self.assertEqual(bootstrap_attempts, 3)
        self.assertTrue(installed)

    def test_failed_readiness_rolls_back_runtime_then_emulator(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            deployment = RuntimeDeployment(
                root=root / "deployment",
                config_path=root / "deployment" / "founderos.runtime.json",
                deployment_id="abc",
            )
            emulator_snapshot = LaunchAgentSnapshot(
                EMULATOR_LAUNCH_AGENT_LABEL,
                root / "emulator.plist",
                b"emulator",
                True,
            )
            runtime_snapshot = LaunchAgentSnapshot(
                LAUNCH_AGENT_LABEL,
                root / "runtime.plist",
                b"runtime",
                True,
            )
            config = {
                "operations": {
                    "health_path": str(root / "health.json"),
                    "heartbeat_seconds": 15,
                },
                "display": {"host": "127.0.0.1:8080"},
            }
            args = SimpleNamespace(service_command="install", skip_emulator=False)
            restored: list[str] = []
            with patch.object(founderosctl, "_preflight"), patch.object(
                founderosctl, "stage_runtime_bundle", return_value=deployment
            ), patch.object(
                founderosctl,
                "capture_launch_agent",
                side_effect=[emulator_snapshot, runtime_snapshot],
            ), patch.object(founderosctl.shutil, "which", return_value="/usr/bin/node"), patch.object(
                founderosctl, "install_emulator_launch_agent", return_value=root / "emulator.new.plist"
            ), patch.object(founderosctl, "_wait_for_emulator"), patch.object(
                founderosctl, "install_launch_agent", return_value=root / "runtime.new.plist"
            ), patch.object(
                founderosctl,
                "_wait_for_runtime",
                side_effect=ServiceError("runtime unhealthy"),
            ), patch.object(
                founderosctl,
                "restore_launch_agent",
                side_effect=lambda snapshot: restored.append(snapshot.label),
            ), patch.object(founderosctl, "state_root", return_value=root / "state"):
                with self.assertRaisesRegex(ServiceError, "runtime unhealthy"):
                    founderosctl._service_command(args, config, root / "source.json")
        self.assertEqual(restored, [LAUNCH_AGENT_LABEL, EMULATOR_LAUNCH_AGENT_LABEL])

    def test_emulator_launch_agent_is_loopback_only_and_contains_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = root / "server.js"
            node = root / "node"
            web_index = root / "web" / "dist" / "index.html"
            renderer = root / "screen_renderer.js"
            sharp_manifest = root / "node_modules" / "sharp" / "package.json"
            web_index.parent.mkdir(parents=True)
            sharp_manifest.parent.mkdir(parents=True)
            server.write_text("", encoding="utf-8")
            renderer.write_text("", encoding="utf-8")
            sharp_manifest.write_text('{"name":"sharp"}', encoding="utf-8")
            node.write_text("#!/bin/sh\nprintf 'v24.0.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
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

    def test_emulator_service_rejects_an_unsupported_node_version(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "web" / "dist").mkdir(parents=True)
            (root / "node_modules" / "sharp").mkdir(parents=True)
            (root / "server.js").write_text("", encoding="utf-8")
            (root / "screen_renderer.js").write_text("", encoding="utf-8")
            (root / "web" / "dist" / "index.html").write_text("", encoding="utf-8")
            (root / "node_modules" / "sharp" / "package.json").write_text("{}", encoding="utf-8")
            node = root / "node"
            node.write_text("#!/bin/sh\nprintf 'v18.20.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            with self.assertRaisesRegex(ServiceError, "20.9.0 or newer"):
                emulator_launch_agent_payload(
                    repository=root,
                    node_executable=node,
                    python_executable=sys.executable,
                    runtime_state_root=root / "state",
                )

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
