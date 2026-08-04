"""Shared post-commit aggregate evidence verification."""

from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import canonicalize_json

from arch_runtime.errors import CorruptStoredRecordError
from arch_runtime.ports import StoredProject


def verify_project_evidence(
    stored: StoredProject | None,
    expected_state: ProjectState,
    *,
    expected_record_version: int,
    expected_record_fingerprint: str,
    expected_content_fingerprint: str,
    operation: str,
) -> None:
    """Compare reconstructed storage evidence with an expected kernel state."""

    project_id = expected_state.metadata.project_id
    if stored is None:
        raise CorruptStoredRecordError(
            "committed project could not be reloaded",
            operation=operation,
            remediation="preserve storage and inspect the committed transaction",
            project_id=project_id,
        )
    if (
        stored.state_json != canonicalize_json(expected_state)
        or stored.record_version != expected_record_version
        or stored.record_fingerprint != expected_record_fingerprint
        or stored.content_fingerprint != expected_content_fingerprint
    ):
        raise CorruptStoredRecordError(
            "committed project evidence does not match the application result",
            operation=operation,
            remediation="preserve storage and inspect the committed transaction",
            project_id=project_id,
        )
