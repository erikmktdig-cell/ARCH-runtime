"""Build final external release evidence after the exact commit is known."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_tools import (
    ROOT,
    ReleaseCheckError,
    inspect_artifacts,
    release_manifest,
    write_json,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tests", type=int, required=True)
    parser.add_argument("--coverage", type=float, required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ci", type=json.loads, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if args.commit != head or dirty:
        raise ReleaseCheckError("manifest requires the exact clean final release checkout")
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    required = {
        "quality_gates",
        "clean_install_matrix",
        "workflow_vertical_slice",
        "migration_replay",
        "reproducibility",
        "security",
        "governance",
        "skipped",
        "known_limitations",
        "release_hardening_commits",
    }
    if not isinstance(evidence, dict) or not required <= evidence.keys():
        raise ReleaseCheckError("missing final release evidence")
    manifest = release_manifest(
        commit=args.commit,
        tests=args.tests,
        branch_coverage=args.coverage,
        artifacts=inspect_artifacts(args.dist),
        ci=args.ci,
    )
    if evidence.keys() & manifest.keys():
        raise ReleaseCheckError("external evidence cannot override artifact identity")
    manifest.update(evidence)
    write_json(args.output, manifest)


if __name__ == "__main__":
    main()
