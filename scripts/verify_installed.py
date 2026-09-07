"""Install release artifacts outside source trees and run public-API compatibility checks."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

KERNEL_URL = (
    "https://github.com/erikmktdig-cell/ARCH-kernel/releases/download/v0.2.0/"
    "arch_kernel-0.2.0-py3-none-any.whl"
)
KERNEL_SHA256 = "3c581c37c4890ac50ff8c1722aca7cc4e29fed85a37656d6f08e22ffb8847b5c"


def verify(artifact: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="arch-runtime-installed-") as directory:
        root = Path(directory).resolve()
        source = Path(__file__).resolve().parents[1]
        if root.is_relative_to(source) or root.is_relative_to(source.parent / "arch-kernel"):
            raise RuntimeError("installation workspace must be outside source checkouts")
        kernel = root / KERNEL_URL.rsplit("/", 1)[1]
        with urllib.request.urlopen(KERNEL_URL, timeout=120) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != KERNEL_SHA256:
            raise RuntimeError("released Kernel artifact SHA-256 mismatch")
        kernel.write_bytes(content)
        candidate = root / artifact.name
        shutil.copyfile(artifact, candidate)
        for name in ("workflow_smoke.py", "historical_fixture.py"):
            shutil.copyfile(source / "scripts" / name, root / name)
        environment = {
            k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}
        }
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(root / "venv")],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(kernel), str(candidate)],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        # -I excludes cwd and PYTHONPATH. run_path loads only this standalone smoke script.
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import runpy; runpy.run_path('workflow_smoke.py', run_name='__main__')",
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    print(verify(parser.parse_args().artifact.resolve()))
