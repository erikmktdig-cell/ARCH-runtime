"""Exercise only the installed public API from a clean environment."""

from __future__ import annotations

import sys
from pathlib import Path

from arch_kernel.contracts import Route

from arch_runtime import CreateProjectCommand, Runtime, RuntimeConfig


def main(database: Path) -> None:
    command = CreateProjectCommand.model_validate(
        {
            "idempotency_key": "release:smoke:create",
            "name": "Installed Release Smoke",
            "slug": "installed-release-smoke",
            "summary": "Verify the installed public runtime facade.",
            "owner": "release-team",
            "project_type": "platform",
            "criticality": "high",
            "default_route": Route.STANDARD,
            "objectives": ("Verify wheel isolation.",),
            "constraints": ("Use only public imports.",),
            "success_criteria": ("Reopen exact persisted evidence.",),
            "actor_id": "release:smoke",
        },
        strict=True,
    )
    with Runtime.open(RuntimeConfig(database, initialize_schema=True)) as runtime:
        created = runtime.create_project(command)
        first = runtime.get_project(created.project_id)
    with Runtime.open(database) as reopened:
        second = reopened.get_project(created.project_id)
    if first != second:
        raise RuntimeError("reopened project evidence differs from the committed result")
    print(created.project_id, second.record_version)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
