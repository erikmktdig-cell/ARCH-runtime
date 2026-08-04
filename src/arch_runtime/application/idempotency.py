"""Shared exact-result idempotency resolution for application mutations."""

from arch_kernel.kernel import canonicalize_json, compute_fingerprint
from pydantic import BaseModel

from arch_runtime.errors import CorruptStoredRecordError, IdempotencyConflictError, PersistenceError
from arch_runtime.ports import IdempotencyRecord, IdempotencyStatus


def resolve_completed_result[ResultT: BaseModel](
    record: IdempotencyRecord | None,
    request_fingerprint: str,
    result_model: type[ResultT],
    *,
    operation: str,
) -> ResultT | None:
    """Resolve exact completed evidence or fail closed on conflict/corruption."""

    if record is None:
        return None
    if record.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            "idempotency key was used for a different request",
            operation=operation,
            remediation="use the original request or a new idempotency key",
            project_id=record.project_id,
        )
    if record.status is not IdempotencyStatus.COMPLETED:
        raise PersistenceError(
            "idempotency reservation is incomplete",
            operation=operation,
            remediation="retry after the active transaction has completed",
            project_id=record.project_id,
        )
    assert record.result_json is not None
    assert record.result_fingerprint is not None
    try:
        result = result_model.model_validate_json(record.result_json, strict=True)
    except ValueError as error:
        raise CorruptStoredRecordError(
            "stored application result is invalid",
            operation=operation,
            remediation="preserve storage and inspect idempotency evidence",
            project_id=record.project_id,
        ) from error
    if (
        canonicalize_json(result) != record.result_json
        or compute_fingerprint(result) != record.result_fingerprint
    ):
        raise CorruptStoredRecordError(
            "stored application result evidence is corrupt",
            operation=operation,
            remediation="preserve storage and inspect idempotency evidence",
            project_id=record.project_id,
        )
    return result
