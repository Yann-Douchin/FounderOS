#!/usr/bin/env python3
"""FounderOS: show the one decision that deserves the BUSY Bar right now."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.connectors.demo import SCENARIOS  # noqa: E402
from founder_os.core.runtime import FounderOSRuntime  # noqa: E402
from founder_os.display.busybar import RecordingDisplay  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1:8080", help="BUSY Bar host or emulator host")
    parser.add_argument("--config", help="Path to a FounderOS JSON configuration")
    parser.add_argument("--demo", action="store_true", help="Use credential-free gallery events")
    parser.add_argument("--scenario", choices=SCENARIOS, default="mixed", help="Demo scenario")
    parser.add_argument("--once", action="store_true", help="Poll and draw once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Rank and print without making an HTTP display call")
    parser.add_argument("--explain", action="store_true", help="Print the winning score components")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    overrides = {"display": {"host": args.host}}
    if args.demo or args.scenario != "mixed":
        overrides["connectors"] = {
            name: {"enabled": name == "demo"}
            for name in (
                "demo", "linear", "slack", "gmail", "calendar", "linkedin",
                "claude", "chatgpt_codex", "github", "stripe", "shopify", "home_assistant",
            )
        }
        overrides["connectors"]["demo"]["scenario"] = args.scenario
    recording = RecordingDisplay() if args.dry_run else None
    runtime = FounderOSRuntime.from_path(args.config, overrides=overrides, display=recording)
    if args.once or args.dry_run:
        state = runtime.tick(force_poll=True)
        if state.selected:
            winner = state.selected
            print(f"selected: {winner.event.source} | {winner.event.title} | score={winner.score:.1f}")
            if args.explain:
                print(winner.explanation())
        else:
            print("selected: none")
        if recording and recording.frames:
            print(json.dumps(recording.frames[-1], indent=2, ensure_ascii=False))
        return 0 if not state.display_error else 2
    runtime.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
