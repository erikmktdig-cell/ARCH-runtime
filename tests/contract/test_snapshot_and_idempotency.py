from datetime import datetime

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import canonicalize_json, compute_content_fingerprint, compute_fingerprint

from arch_runtime.ports import IdempotencyStore, SnapshotStore
from arch_runtime.ports.storage import (
    IdempotencyRecord,
    IdempotencyStatus,
    StoredSnapshot,
)
from tests.fakes.adapters import (
    SCHEMA_FINGERPRINT,
    FrozenClock,
)

pytestmark = pytest.mark.contract


def test_snapshot_store_returns_highest_aggregate_version(
    project_state: ProjectState,
    snapshot_store: SnapshotStore,
) -> None:
    for version, suffix in ((1, "A"), (2, "B")):
        snapshot_store.save(
            StoredSnapshot(
                snapshot_id=f"SNP-01HZX7M3FQ1T2Q9V8Y6K4C2B1{suffix}",
                project_id=project_state.metadata.project_id,
                aggregate_version=version,
                last_stream_position=version,
                contract_name=project_state.contract_name,
                contract_version=project_state.contract_version,
                schema_fingerprint=SCHEMA_FINGERPRINT,
                record_fingerprint=compute_fingerprint(project_state),
                content_fingerprint=compute_content_fingerprint(project_state),
                state_json=canonicalize_json(project_state),
                created_at=project_state.created_at,
            )
        )

    latest = snapshot_store.get_latest(project_state.metadata.project_id)
    assert latest is not None
    assert latest.aggregate_version == 2


def test_idempotency_store_persists_canonical_logical_result(
    project_state: ProjectState,
    frozen_clock: FrozenClock,
    idempotency_store: IdempotencyStore,
) -> None:
    created_at = project_state.created_at
    reservation = IdempotencyRecord(
        operation_name="project.create",
        idempotency_key="create:001",
        project_id=project_state.metadata.project_id,
        request_fingerprint="sha256:" + "b" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=created_at,
    )
    idempotency_store.reserve(reservation)
    rejection = {"accepted": False, "code": "validation.blocked"}
    idempotency_store.complete("project.create", "create:001", rejection)

    completed = idempotency_store.get("project.create", "create:001")
    assert completed is not None
    assert completed.status is IdempotencyStatus.COMPLETED
    assert completed.result_json == canonicalize_json(rejection)
    assert completed.completed_at == frozen_clock.now()


def test_frozen_clock_is_deterministic(frozen_clock: FrozenClock) -> None:
    first: datetime = frozen_clock.now()
    assert frozen_clock.now() is first
