#!/usr/bin/env python3
"""Validate and atomically publish one authorized connector snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.models import Event, utc_now  # noqa: E402
from founder_os.paths import connector_state_root, ensure_private_directory  # noqa: E402


SOURCES = ("linear", "calendar", "slack", "gmail", "linkedin")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=SOURCES)
    parser.add_argument("--output", help="Destination, defaults to the private FounderOS state directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.load(sys.stdin)
        events = _normalize_input(payload, args.source)
        destination = Path(args.output).expanduser() if args.output else connector_state_root() / f"{args.source}.json"
        publish_snapshot(destination, args.source, events)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"snapshot rejected: {exc}", file=sys.stderr)
        return 2
    print(str(destination))
    return 0


def publish_snapshot(path: Path, source: str, events: list[Mapping[str, Any]]) -> None:
    generated_at = utc_now()
    validated = [Event.from_mapping(event, source=source).to_dict() for event in events]
    envelope = {
        "schema_version": 1,
        "source": source,
        "generated_at": generated_at.isoformat(),
        "events": validated,
    }
    directory = ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)


def _normalize_input(payload: Any, source: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        envelope_source = str(payload.get("source", source)).strip().lower()
        if envelope_source != source:
            raise ValueError(f"snapshot source {envelope_source!r} does not match {source!r}")
        rows = payload.get("events")
    else:
        rows = None
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("input must be an event array or an envelope containing an event array")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
