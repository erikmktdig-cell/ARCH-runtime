from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any, cast

import pytest
from arch_kernel.contracts import EventEnvelope, ProjectState
from pydantic import ValidationError

from arch_runtime.errors import RuntimeConfigurationError, RuntimeErrorCode
from arch_runtime.ports.storage import IdempotencyRecord, IdempotencyStatus, StoredEvent
from tests.fakes.adapters import (
    FrozenClock,
    InMemoryEventStore,
    stored_project_from_state,
)

pytestmark = pytest.mark.unit


def test_idempotency_record_is_frozen_and_rejects_partial_completion(
    frozen_clock: FrozenClock,
) -> None:
    record = IdempotencyRecord(
        operation_name="project.create",
        idempotency_key="key",
        request_fingerprint="sha256:" + "c" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=frozen_clock.now(),
    )
    with pytest.raises(ValidationError):
        record.status = IdempotencyStatus.COMPLETED
    with pytest.raises(ValidationError):
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key="key",
            request_fingerprint="sha256:" + "c" * 64,
            status=IdempotencyStatus.COMPLETED,
            created_at=frozen_clock.now(),
        )


def test_storage_times_must_be_utc() -> None:
    with pytest.raises(ValidationError, match="Naive datetimes"):
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key="key",
            request_fingerprint="sha256:" + "c" * 64,
            status=IdempotencyStatus.IN_PROGRESS,
            created_at=datetime(2026, 7, 31, 12, 0),
        )


def test_stored_project_rejects_reversed_timestamps(project_state: ProjectState) -> None:
    stored = stored_project_from_state(project_state)
    values = stored.model_dump()
    values["updated_at"] = stored.created_at.replace(year=2025)

    with pytest.raises(ValidationError, match="must not precede"):
        type(stored).model_validate(values)


def test_stored_event_rejects_invalid_version_delta(event_envelope: EventEnvelope) -> None:
    store = InMemoryEventStore()
    store.append(
        event_envelope,
        version_before=1,
        version_after=2,
        previous_event_fingerprint=None,
    )
    values = store.records[0].model_dump()
    values["aggregate_version_after"] = 3

    with pytest.raises(ValidationError, match="aggregate versions"):
        StoredEvent.model_validate(values)


def test_idempotency_status_and_completion_time_must_agree(
    frozen_clock: FrozenClock,
) -> None:
    with pytest.raises(ValidationError, match="in-progress"):
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key="key",
            request_fingerprint="sha256:" + "c" * 64,
            status=IdempotencyStatus.IN_PROGRESS,
            result_fingerprint="sha256:" + "d" * 64,
            created_at=frozen_clock.now(),
        )

    with pytest.raises(ValidationError, match="must not precede"):
        IdempotencyRecord(
            operation_name="project.create",
            idempotency_key="key",
            request_fingerprint="sha256:" + "c" * 64,
            status=IdempotencyStatus.COMPLETED,
            result_fingerprint="sha256:" + "d" * 64,
            result_json=b"{}",
            created_at=frozen_clock.now(),
            completed_at=frozen_clock.now().replace(year=2025),
        )


def test_runtime_error_exposes_frozen_safe_details() -> None:
    error = RuntimeConfigurationError(
        "configuration is invalid",
        operation="runtime.configure",
        remediation="provide a supported configuration",
    )
    assert error.details.code is RuntimeErrorCode.CONFIGURATION_INVALID
    assert error.details.operation == "runtime.configure"
    with pytest.raises(FrozenInstanceError):
        cast(Any, error.details).message = "changed"
