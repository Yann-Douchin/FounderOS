#!/usr/bin/env python3
"""Copy a macOS service installation out of protected source folders before Python imports it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
_ARCHIVE_PATHS = (
    "founder_os",
    "apps",
    "public/brand",
    "public/fonts",
    "public/icons",
    "public/sounds",
    "public/icons.json",
    "server.js",
)
_CLEAN_PATHS = ("founder_os", "apps", "public", "server.js", "web")


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if sys.platform != "darwin":
        print("error: the FounderOS service bootstrap is available only on macOS", file=sys.stderr)
        return 2
    config = _config_path(arguments)
    state = _state_root()
    bootstrap_root = state / "bootstrap"
    bootstrap_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    bootstrap_root.chmod(0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=bootstrap_root))
    temporary.chmod(0o700)
    try:
        _require_committed_runtime()
        _extract_git_runtime(temporary)
        _copy_entry(SOURCE_ROOT / "web" / "dist", temporary / "web" / "dist")
        staged_config = temporary / "founderos.runtime.json"
        _copy_entry(config, staged_config)
        _make_private(temporary)
        environment = dict(os.environ)
        environment["FOUNDEROS_BOOTSTRAPPED"] = "1"
        command = [
            sys.executable,
            str(temporary / "apps" / "founderosctl.py"),
            *_rewrite_config(arguments, staged_config),
        ]
        result = subprocess.run(
            command,
            cwd=temporary,
            env=environment,
            check=False,
        )
        return int(result.returncode)
    except (OSError, ValueError) as exc:
        print(f"error: macOS service bootstrap failed: {exc}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _config_path(arguments: list[str]) -> Path:
    for index, value in enumerate(arguments):
        if value == "--config":
            if index + 1 >= len(arguments):
                raise ValueError("--config requires a path")
            return Path(arguments[index + 1]).expanduser().resolve()
        if value.startswith("--config="):
            return Path(value.split("=", 1)[1]).expanduser().resolve()
    return Path("founderos.local.json").resolve()


def _rewrite_config(arguments: list[str], staged_config: Path) -> list[str]:
    rewritten: list[str] = []
    replaced = False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--config":
            rewritten.extend((value, str(staged_config)))
            index += 2
            replaced = True
            continue
        if value.startswith("--config="):
            rewritten.append(f"--config={staged_config}")
            index += 1
            replaced = True
            continue
        rewritten.append(value)
        index += 1
    if not replaced:
        rewritten[0:0] = ["--config", str(staged_config)]
    return rewritten


def _state_root() -> Path:
    configured = os.environ.get("FOUNDEROS_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "FounderOS"


def _copy_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run_copy(["/bin/cp", "-R", str(source), str(destination)])


def _require_committed_runtime() -> None:
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(SOURCE_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            *_CLEAN_PATHS,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise OSError(f"Git status exited with code {result.returncode}")
    if result.stdout.strip():
        raise ValueError("runtime source contains uncommitted tracked changes")


def _extract_git_runtime(destination: Path) -> None:
    archive = destination / ".runtime.tar"
    _run_copy([
        "/usr/bin/git",
        "-C",
        str(SOURCE_ROOT),
        "archive",
        "--format=tar",
        f"--output={archive}",
        "HEAD",
        "--",
        *_ARCHIVE_PATHS,
    ])
    try:
        _run_copy(["/usr/bin/tar", "-xf", str(archive), "-C", str(destination)])
    finally:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass


def _run_copy(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise OSError(f"system copy exited with code {result.returncode}")


def _make_private(root: Path) -> None:
    for current, directory_names, file_names in os.walk(root):
        current_path = Path(current)
        current_path.chmod(0o700)
        for name in directory_names:
            (current_path / name).chmod(0o700)
        for name in file_names:
            (current_path / name).chmod(0o600)


if __name__ == "__main__":
    raise SystemExit(main())
