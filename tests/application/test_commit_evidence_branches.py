from datetime import UTC, datetime

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import canonicalize_json, compute_fingerprint
from pydantic import BaseModel

from arch_runtime.application.evidence import verify_project_evidence
from arch_runtime.application.idempotency import resolve_completed_result
from arch_runtime.errors import CorruptStoredRecordError
from arch_runtime.ports import IdempotencyRecord, IdempotencyStatus
from tests.fakes.adapters import stored_project_from_state


@pytest.mark.parametrize("missing", [True, False])
def test_post_commit_missing_or_mismatching_state_fails_closed(
    project_state: ProjectState,
    missing: bool,
) -> None:
    stored = stored_project_from_state(project_state)
    with pytest.raises(CorruptStoredRecordError):
        verify_project_evidence(
            None if missing else stored,
            project_state,
            expected_record_version=stored.record_version,
            expected_record_fingerprint="sha256:" + "f" * 64,
            expected_content_fingerprint=stored.content_fingerprint,
            operation="test.reload",
        )


def test_idempotency_rejects_valid_result_with_wrong_fingerprint() -> None:
    class Result(BaseModel):
        accepted: bool

    result = Result(accepted=True)
    record = IdempotencyRecord(
        operation_name="test.result",
        idempotency_key="key",
        request_fingerprint="sha256:" + "a" * 64,
        status=IdempotencyStatus.COMPLETED,
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        completed_at=datetime(2026, 9, 7, tzinfo=UTC),
        result_json=canonicalize_json(result),
        result_fingerprint=compute_fingerprint({"accepted": False}),
    )
    with pytest.raises(CorruptStoredRecordError, match="evidence is corrupt"):
        resolve_completed_result(
            record, record.request_fingerprint, Result, operation="test.result"
        )
