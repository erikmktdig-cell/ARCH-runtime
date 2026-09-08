"""Gate actual covered branches, not coverage.py's combined line/branch percentage."""

import json
import sys
from pathlib import Path


def check(path: Path) -> float:
    totals = json.loads(path.read_text(encoding="utf-8"))["totals"]
    branches = totals["num_branches"]
    if branches <= 0:
        raise ValueError("branch evidence is missing")
    percent = 100 * int(totals["covered_branches"]) / int(branches)
    if percent < 90:
        raise ValueError(f"branch coverage {percent:.2f}% is below 90%")
    return percent


if __name__ == "__main__":
    print(f"PASS: actual branch coverage {check(Path(sys.argv[1])):.2f}%")
