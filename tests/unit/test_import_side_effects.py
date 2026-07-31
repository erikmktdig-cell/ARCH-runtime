import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_all_bootstrap_modules_import_without_external_side_effects(tmp_path: Path) -> None:
    script = """
import logging
import os
import pathlib
import socket
import subprocess

def forbidden(*args, **kwargs):
    raise AssertionError("external side effect attempted during import")

socket.socket = forbidden
subprocess.Popen = forbidden
subprocess.run = forbidden
pathlib.Path.write_text = forbidden
pathlib.Path.write_bytes = forbidden
before_environment = dict(os.environ)
before_handlers = tuple(logging.root.handlers)
before_files = tuple(pathlib.Path.cwd().iterdir())

import arch_runtime
import arch_runtime.application
import arch_runtime.errors
import arch_runtime.migrations
import arch_runtime.persistence
import arch_runtime.persistence.sqlite
import arch_runtime.ports
import arch_runtime.replay

assert dict(os.environ) == before_environment
assert tuple(logging.root.handlers) == before_handlers
assert tuple(pathlib.Path.cwd().iterdir()) == before_files
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
