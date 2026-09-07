"""Build wheel and sdist twice and require byte-identical artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_tools import ROOT, ReleaseCheckError, sha256


def _build(output: Path) -> dict[str, str]:
    environment = {**os.environ, "SOURCE_DATE_EPOCH": "1785456000", "PYTHONHASHSEED": "0"}
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    artifacts = sorted(
        path for path in output.iterdir() if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    if len(artifacts) != 2:
        raise ReleaseCheckError("reproducibility build did not produce one wheel and one sdist")
    return {path.name: sha256(path) for path in artifacts}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="arch-runtime-repro-") as temp_name:
        root = Path(temp_name)
        first = _build(root / "first")
        second = _build(root / "second")
    if first != second:
        raise ReleaseCheckError(f"non-reproducible artifacts: first={first}, second={second}")
    for name, digest in first.items():
        print(f"{digest}  {name}")
    print("PASS: wheel and sdist are byte-reproducible")


if __name__ == "__main__":
    main()
