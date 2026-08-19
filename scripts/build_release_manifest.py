"""Build final external release evidence after the exact commit is known."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_tools import inspect_artifacts, release_manifest, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tests", type=int, required=True)
    parser.add_argument("--coverage", type=float, required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ci", type=json.loads, required=True)
    args = parser.parse_args()
    manifest = release_manifest(
        commit=args.commit,
        tests=args.tests,
        branch_coverage=args.coverage,
        artifacts=inspect_artifacts(args.dist),
        ci=args.ci,
    )
    write_json(args.output, manifest)


if __name__ == "__main__":
    main()
