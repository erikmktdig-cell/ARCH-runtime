"""Fail-closed local release artifact and source audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_tools import ReleaseCheckError, audit_tracked_source, inspect_artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    try:
        audit_tracked_source()
        inventory = inspect_artifacts(args.dist)
    except (OSError, ReleaseCheckError) as error:
        print(f"FAIL: {error}")
        return 1
    print(json.dumps(inventory, indent=2, sort_keys=True))
    print("PASS: source and distribution release audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
