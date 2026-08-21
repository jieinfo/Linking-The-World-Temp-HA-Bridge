#!/usr/bin/env python3
"""Keep the add-on runtime copy generated from the canonical root bridge.py."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bridge.py"
ADDON_DIRECTORY = ROOT / "linking_the_world_temp_ha_bridge_addon"
# The local development bundle contains the repository under home_assistant_addon,
# while the published GitHub repository uses the add-on directory at its root.
if not ADDON_DIRECTORY.exists():
    ADDON_DIRECTORY = ROOT / "home_assistant_addon" / "linking_the_world_temp_ha_bridge_addon"
DESTINATION = ADDON_DIRECTORY / "bridge.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of updating an out-of-sync add-on copy")
    args = parser.parse_args()
    source = SOURCE.read_text(encoding="utf-8")
    destination = DESTINATION.read_text(encoding="utf-8") if DESTINATION.exists() else ""
    if source == destination:
        return
    if args.check:
        raise SystemExit("add-on bridge.py is out of sync; run python scripts/sync_addon_bridge.py")
    DESTINATION.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
