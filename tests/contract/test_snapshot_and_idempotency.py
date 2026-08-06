from datetime import datetime

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import (
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
)

from arch_runtime.ports import IdempotencyStore, SnapshotStore
from arch_runtime.ports.storage import (
    IdempotencyRecord,
    IdempotencyStatus,
    StoredSnapshot,
)
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.contract


def test_snapshot_store_returns_highest_aggregate_version(
    project_state: ProjectState,
    snapshot_store: SnapshotStore,
) -> None:
    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    assert registration is not None
    for version, suffix in ((1, "A"), (2, "B")):
        state = project_state.model_copy(update={"record_version": version})
        snapshot_store.save(
            StoredSnapshot(
                snapshot_id=f"SNP-01HZX7M3FQ1T2Q9V8Y6K4C2B1{suffix}",
                project_id=project_state.metadata.project_id,
                aggregate_version=version,
                last_stream_position=version,
                contract_name=project_state.contract_name,
                contract_version=project_state.contract_version,
                schema_fingerprint=registration.descriptor.schema_fingerprint,
                record_fingerprint=compute_fingerprint(state),
                content_fingerprint=compute_content_fingerprint(state),
                state_json=canonicalize_json(state),
                created_at=project_state.created_at,
            )
        )

    latest = snapshot_store.get_latest(project_state.metadata.project_id)
    assert latest is not None
    assert latest.aggregate_version == 2
    listed = snapshot_store.list_for_project(project_state.metadata.project_id)
    assert tuple(item.aggregate_version for item in listed) == (2, 1)

    snapshot_store.delete(project_state.metadata.project_id, listed[-1].snapshot_id)
    assert tuple(
        item.aggregate_version
        for item in snapshot_store.list_for_project(project_state.metadata.project_id)
    ) == (2,)


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
