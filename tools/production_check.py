#!/usr/bin/env python3
"""Fail closed when a FounderOS production invariant is missing."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from founder_os.config import load_config  # noqa: E402


class CheckFailure(RuntimeError):
    pass


def main() -> int:
    checks = [
        check_required_files,
        check_json_documents,
        check_runtime_dependencies,
        check_production_config,
        check_autonomous_service,
        check_private_state,
        check_hook_state_paths,
        check_hook_runtime,
        check_font_atlas,
        check_bar_pilot_contract,
        check_gallery_captures,
        check_english_interface,
        check_character_integrity,
        check_git_exclusions,
    ]
    try:
        for check in checks:
            check()
            print(f"PASS {check.__name__}")
    except CheckFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("FounderOS production invariants satisfied")
    return 0


def check_required_files() -> None:
    required = (
        ".github/workflows/ci.yml",
        "SECURITY.md",
        "docs/founderos/PRODUCTION-CLOSURE.md",
        "docs/founderos/BARPILOT-COMPATIBILITY.md",
        "founderos.production.example.json",
        "founderos.macos.example.json",
        "apps/founderos_install.zsh",
        "apps/founderosctl.py",
        "deploy/slack-app-manifest.yaml",
        "founder_os/secrets.py",
        "founder_os/service.py",
        "founder_os/interaction.py",
        "founder_os/core/scheduler.py",
        "founder_os/closure/engine.py",
        "founder_os/closure/ledger.py",
        "founder_os/closure/models.py",
        "founder_os/connectors/notion.py",
        "founder_os/connectors/drive.py",
        "founder_os/connectors/github.py",
        "founder_os/connectors/observability.py",
        "founder_os/connectors/commerce.py",
        "founder_os/connectors/superhuman.py",
        "web/src/components/tabs/Obligations.vue",
        "founder_os/display/capabilities.py",
        "founder_os/display/verification.py",
        "package-lock.json",
        "screen_renderer.js",
        "tools/barpilot_compat.py",
        "tests/fixtures/barpilot-api25-contract.json",
        "tests/barpilot_endpoint_matrix_test.js",
        "tools/capture_emulator.py",
        "web/src/lib/background-clock.js",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise CheckFailure(f"required production files are missing: {missing}")


def check_json_documents() -> None:
    for name in (
        "founderos.example.json",
        "founderos.production.example.json",
        "founderos.macos.example.json",
        ".codex/hooks.json",
    ):
        try:
            payload = json.loads((ROOT / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckFailure(f"invalid JSON in {name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CheckFailure(f"{name} must contain a JSON object")


def check_runtime_dependencies() -> None:
    try:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckFailure(f"invalid root Node dependency metadata: {exc}") from exc
    if package.get("dependencies", {}).get("sharp") != "0.35.3":
        raise CheckFailure("the production screen decoder must pin Sharp 0.35.3")
    if package.get("engines", {}).get("node") != ">=20.9.0":
        raise CheckFailure("the root package must require Node.js 20.9.0 or newer")
    locked_root = lock.get("packages", {}).get("", {})
    if locked_root.get("dependencies", {}).get("sharp") != "0.35.3":
        raise CheckFailure("the lockfile does not pin the production screen decoder")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    if "Install locked emulator dependencies" not in workflow or "run: npm ci" not in workflow:
        raise CheckFailure("CI does not install the locked emulator dependencies")
    if (
        "barpilot-conformance:" not in workflow
        or "5c4afe96e178982d7e5f95a9dfea0cf761804d80/barpilot.html" not in workflow
        or "--source-only" not in workflow
    ):
        raise CheckFailure("CI does not prove the endpoint contract against pinned BarPilot source")


def check_bar_pilot_contract() -> None:
    contract_path = ROOT / "tests" / "fixtures" / "barpilot-api25-contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckFailure(f"invalid BarPilot contract: {exc}") from exc
    reference = contract.get("reference", {})
    if reference.get("commit") != "5c4afe96e178982d7e5f95a9dfea0cf761804d80":
        raise CheckFailure("BarPilot reference commit is not pinned")
    digest = str(reference.get("sha256", ""))
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise CheckFailure("BarPilot source hash is invalid")
    endpoints = contract.get("barpilot_endpoints", [])
    if len(endpoints) != 53 or len({endpoint.get("path") for endpoint in endpoints}) != 53:
        raise CheckFailure("BarPilot contract must govern exactly 53 unique endpoints")
    operation_count = sum(len(endpoint.get("methods", [])) for endpoint in endpoints)
    if operation_count != 69:
        raise CheckFailure(f"BarPilot contract must govern 69 HTTP operations, found {operation_count}")
    incomplete = [
        endpoint.get("path")
        for endpoint in endpoints
        if endpoint.get("support") not in {"emulated", "stateful"}
        or not endpoint.get("behavior")
    ]
    if incomplete:
        raise CheckFailure(f"BarPilot endpoints lack explicit behavior: {incomplete}")
    if contract.get("status_websocket", {}).get("path") != "/api/status/ws":
        raise CheckFailure("BarPilot status WebSocket is not governed")


def check_production_config() -> None:
    with tempfile.TemporaryDirectory() as folder:
        environment = dict(os.environ)
        environment.update(
            {
                "FOUNDEROS_STATE_DIR": folder,
                "LINEAR_TEAM_KEY": "BUSY",
                "SLACK_CHANNEL_ID": "C00000000",
                "SLACK_MENTION_MARKER": "<@U00000000>",
                "SLACK_SELF_USER_ID": "U00000000",
            }
        )
        previous = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(environment)
            config = load_config(ROOT / "founderos.production.example.json")
        finally:
            os.environ.clear()
            os.environ.update(previous)
    if config["runtime"]["environment"] != "production":
        raise CheckFailure("production example does not enable production validation")
    if config["display"]["text_rendering"] != "auto":
        raise CheckFailure("production display must auto-verify Unicode rendering")
    linear = config["connectors"]["linear"]
    if linear.get("scope") != "portfolio" or not linear.get("team_keys"):
        raise CheckFailure("production Linear must use an allowlisted portfolio scope")
    if not config["closure"].get("enabled"):
        raise CheckFailure("the production example does not enable obligation governance")
    required_live = {"linear", "calendar", "slack", "gmail"}
    enabled = {
        name for name, value in config["connectors"].items()
        if isinstance(value, dict) and value.get("enabled")
    }
    if not required_live.issubset(enabled):
        raise CheckFailure("the production example does not activate the four primary live connectors")
    if config["closure"].get("rank_raw_events"):
        raise CheckFailure("production must rank governed obligations instead of duplicating raw events")


def check_autonomous_service() -> None:
    controller = (ROOT / "apps" / "founderosctl.py").read_text(encoding="utf-8")
    if "--skip-preflight" in controller:
        raise CheckFailure("autonomous service installation can bypass the live connector preflight")
    bootstrap = (ROOT / "apps" / "founderos_install.zsh").read_text(encoding="utf-8")
    required_bootstrap_controls = (
        "/usr/bin/git",
        "archive",
        "HEAD",
        "FOUNDEROS_BOOTSTRAPPED=1",
        "mktemp -d",
        "web/dist",
        "public/animations",
        "screen_renderer.js",
        "node_modules",
        "founderos.runtime.json",
    )
    missing_bootstrap_controls = [
        control for control in required_bootstrap_controls if control not in bootstrap
    ]
    if missing_bootstrap_controls:
        raise CheckFailure(
            f"private service bootstrap controls are missing: {missing_bootstrap_controls}"
        )
    if "founderos_bootstrap.py" in controller or not (
        "founderos_install.zsh" in controller and "os.execv" in controller
    ):
        raise CheckFailure("service install does not delegate out of the protected checkout")
    server = (ROOT / "server.js").read_text(encoding="utf-8")
    if 'process.env.BUSY_HOST || "127.0.0.1"' not in server or "server.listen(PORT, HOST" not in server:
        raise CheckFailure("the emulator is not loopback-only by default")
    if "BUSY_DATA_DIR must be absolute" not in server or "configured || defaultDataDir()" not in server:
        raise CheckFailure("the emulator state does not default outside the checkout")
    service = (ROOT / "founder_os" / "service.py").read_text(encoding="utf-8")
    if '"BUSY_HOST": "127.0.0.1"' not in service or '"BUSY_DATA_DIR": str(state / "emulator")' not in service:
        raise CheckFailure("the supervised emulator lacks loopback or private-state isolation")
    if (
        'health_state = "process_mismatch"' not in service
        or "_wait_for_runtime(config" not in controller
        or "_wait_for_emulator(config" not in controller
    ):
        raise CheckFailure("service installation does not verify its supervised processes")
    if not all(
        control in service
        for control in (
            "stage_runtime_bundle",
            "restore_launch_agent",
            "_bootstrap_launch_agent",
            "_LAUNCHCTL_BOOTSTRAP_ATTEMPTS = 3",
        )
    ) or not all(
        control in controller
        for control in (
            "capture_launch_agent",
            "restore_launch_agent",
            "stage_runtime_bundle",
        )
    ):
        raise CheckFailure("service deployment lacks private staging, retry, or rollback controls")


def check_private_state() -> None:
    config = load_config()
    memory_path = Path(config["memory"]["path"]).expanduser().resolve()
    if memory_path.is_relative_to(ROOT.resolve()):
        raise CheckFailure("default memory path is inside the checkout")


def check_hook_state_paths() -> None:
    codex = (ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8")
    claude = (ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
    if ".data/agents" in codex or ".data/agents" in claude:
        raise CheckFailure("agent hooks still write private state inside the checkout")
    if "PermissionRequest" not in codex or "PermissionRequest" not in claude:
        raise CheckFailure("Claude and Codex PermissionRequest hooks must both be present")
    bridge_cli = (ROOT / "apps" / "agent_bridge.py").read_text(encoding="utf-8")
    if 'commands.add_parser("decide"' in bridge_cli:
        raise CheckFailure("agent bridge CLI can bypass the trusted input boundary")


def check_hook_runtime() -> None:
    interpreter = Path("/usr/bin/python3")
    if not interpreter.is_file():
        return
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.run(
        [str(interpreter), str(ROOT / "apps" / "agent_permission_hook.py"), "--help"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise CheckFailure(f"project hook cannot run with /usr/bin/python3: {process.stderr.strip()}")


def check_font_atlas() -> None:
    atlas = json.loads((ROOT / "public" / "fonts" / "font-atlas.json").read_text(encoding="utf-8"))
    glyphs = (atlas.get("global") or {}).get("glyphs") or {}
    required = "ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒàâäçéèêëîïôöùûüÿœ’«»"
    missing = [character for character in required if str(ord(character)) not in glyphs]
    if missing:
        raise CheckFailure(f"global font atlas is missing French glyphs: {missing}")
    clipped = [character for character in required if int(glyphs[str(ord(character))].get("oy", 0)) < 0]
    if clipped:
        raise CheckFailure(f"French glyph accents would be clipped above the font line: {clipped}")


def check_gallery_captures() -> None:
    names = (
        "linear-blocker.png", "calendar.png", "gmail.png", "slack.png", "clear.png",
        "agent-permission.png", "agent-usage.png",
    )
    for name in names:
        path = ROOT / "docs" / "founderos" / "captures" / name
        try:
            header = path.read_bytes()[:24]
        except OSError as exc:
            raise CheckFailure(f"gallery capture is missing: {name}") from exc
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            raise CheckFailure(f"gallery capture is not a valid PNG: {name}")
        width, height = struct.unpack(">II", header[16:24])
        if (width, height) != (720, 160):
            raise CheckFailure(f"gallery capture has wrong dimensions: {name} is {width}x{height}")


def check_english_interface() -> None:
    paths = (
        "apps/agent_permission_hook.py",
        "founder_os/agents/bridge.py",
        "founder_os/connectors/agents.py",
        "founder_os/connectors/calendar.py",
        "founder_os/connectors/demo.py",
        "founder_os/connectors/gmail.py",
        "founder_os/connectors/linear.py",
        "founder_os/core/scheduler.py",
    )
    text = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in paths)
    forbidden = (
        "Autorisation requise",
        "Utilisation ",
        "indisponible",
        "Connexion ",
        "Données ",
        "Sans objet",
        "Expéditeur inconnu",
        "Rendez-vous",
        "PRÉPA ",
        "Non assigné",
        "secret masqué",
        "Refusé depuis",
        "décision à valider",
    )
    found = [phrase for phrase in forbidden if phrase in text]
    if found:
        raise CheckFailure(f"French interface fallback text remains: {found}")


def check_character_integrity() -> None:
    suffixes = {".py", ".md", ".json", ".yml", ".yaml", ".js", ".vue"}
    ignored_parts = {"node_modules", ".git", "dist", "public"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes or ignored_parts.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CheckFailure(f"non-UTF-8 user-facing source: {path.relative_to(ROOT)}") from exc
        if chr(0xFFFD) in text:
            raise CheckFailure(f"replacement character found in {path.relative_to(ROOT)}")
        if unicodedata.normalize("NFC", text) != text:
            raise CheckFailure(f"non-NFC text found in {path.relative_to(ROOT)}")


def check_git_exclusions() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if (
        ".data/" not in ignore
        or "founderos.local.json" not in ignore
        or "founderos.*.local.json" not in ignore
    ):
        raise CheckFailure("private local data exclusions are incomplete")
    process = subprocess.run(
        ["git", "ls-files", ".data"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode == 0 and process.stdout.strip():
        raise CheckFailure("private .data files are tracked by Git")


if __name__ == "__main__":
    raise SystemExit(main())
