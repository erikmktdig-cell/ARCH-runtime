# ruff: noqa: F811
from __future__ import annotations

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import build_builtin_contract_registry
from hypothesis import given
from hypothesis import strategies as st

from arch_runtime.application import (
    CreateSnapshotCommand,
    SnapshotPolicy,
    SnapshotService,
)
from arch_runtime.errors import ReplayIntegrityError
from arch_runtime.ports import StoredSnapshot
from arch_runtime.replay import ReplayService
from tests.fakes.adapters import FrozenClock
from tests.replay.conftest import ReplaySeed, replay_seed  # noqa: F401

pytestmark = pytest.mark.unit


class SnapshotIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> str:
        self.calls += 1
        return "SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R08"


def _command(seed: ReplaySeed, *, force: bool = False) -> CreateSnapshotCommand:
    tail = seed.uow.events.read_stream(seed.project_id)[-1]
    return CreateSnapshotCommand(
        project_id=seed.project_id,
        expected_record_version=seed.current.record_version,
        expected_record_fingerprint=seed.current.record_fingerprint,
        expected_content_fingerprint=seed.current.content_fingerprint,
        expected_stream_position=tail.stream_position,
        expected_stream_fingerprint=tail.event_fingerprint,
        force=force,
    )


def _service(
    seed: ReplaySeed,
    clock: FrozenClock,
    ids: SnapshotIds,
    policy: SnapshotPolicy,
) -> SnapshotService:
    return SnapshotService(
        unit_of_work_factory=lambda: seed.uow,
        replay_service=ReplayService(unit_of_work_factory=lambda: seed.uow),
        clock=clock,
        snapshot_ids=ids,
        policy=policy,
    )


@given(
    version=st.integers(min_value=1, max_value=10_000),
    every=st.integers(min_value=1, max_value=100),
    minimum=st.integers(min_value=1, max_value=100),
)
def test_snapshot_policy_is_a_deterministic_version_function(
    version: int, every: int, minimum: int
) -> None:
    policy = SnapshotPolicy(every_n_versions=every, minimum_version=minimum)
    expected = version >= minimum and (version - minimum) % every == 0
    assert policy.should_create(version) is expected
    assert policy.should_create(version) is expected


def test_explicit_snapshot_captures_exact_verified_commit_and_is_idempotent(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    ids = SnapshotIds()
    service = _service(
        replay_seed,
        frozen_clock,
        ids,
        SnapshotPolicy(every_n_versions=1, retain_latest=2),
    )

    created = service.create_snapshot(_command(replay_seed))
    repeated = service.create_snapshot(_command(replay_seed))

    assert created.created
    assert repeated.created is False
    assert repeated.snapshot_id == created.snapshot_id
    assert ids.calls == 1
    stored = replay_seed.uow.snapshots.get_latest(replay_seed.project_id)
    assert stored is not None
    assert stored.state_json == replay_seed.current.state_json
    assert stored.last_stream_position == created.stream_position


def test_policy_skip_does_not_create_snapshot(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    ids = SnapshotIds()
    result = _service(
        replay_seed,
        frozen_clock,
        ids,
        SnapshotPolicy(every_n_versions=10, minimum_version=3),
    ).create_snapshot(_command(replay_seed))

    assert not result.policy_matched
    assert not result.created
    assert result.snapshot_id is None
    assert ids.calls == 0
    assert replay_seed.uow.snapshots.list_for_project(replay_seed.project_id) == ()


def test_retention_keeps_latest_versions_in_deterministic_order(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    registration = build_builtin_contract_registry().get_by_model(ProjectState)
    assert registration is not None
    replay_seed.uow.snapshots.save(
        StoredSnapshot(
            snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R01",
            project_id=replay_seed.project_id,
            aggregate_version=replay_seed.created.record_version,
            last_stream_position=1,
            contract_name=replay_seed.created.contract_name,
            contract_version=replay_seed.created.contract_version,
            schema_fingerprint=registration.descriptor.schema_fingerprint,
            record_fingerprint=replay_seed.created.record_fingerprint,
            content_fingerprint=replay_seed.created.content_fingerprint,
            state_json=replay_seed.created.state_json,
            created_at=replay_seed.created.updated_at,
        )
    )
    result = _service(
        replay_seed,
        frozen_clock,
        SnapshotIds(),
        SnapshotPolicy(every_n_versions=1, retain_latest=1),
    ).create_snapshot(_command(replay_seed))

    assert result.retained_snapshot_ids == (result.snapshot_id,)
    assert result.deleted_snapshot_ids == ("SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R01",)
    assert len(replay_seed.uow.snapshots.list_for_project(replay_seed.project_id)) == 1


def test_invalid_same_version_snapshot_is_never_accepted(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    from tests.replay.conftest import save_snapshot

    save_snapshot(replay_seed, valid=False)
    service = _service(
        replay_seed,
        frozen_clock,
        SnapshotIds(),
        SnapshotPolicy(every_n_versions=1),
    )

    with pytest.raises(ReplayIntegrityError, match="disagrees"):
        service.create_snapshot(_command(replay_seed))
