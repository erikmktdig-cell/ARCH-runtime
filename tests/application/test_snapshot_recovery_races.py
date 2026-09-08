# ruff: noqa: F811
from unittest.mock import Mock

import pytest

from arch_runtime.application import SnapshotPolicy
from arch_runtime.application import snapshots as snapshot_module
from arch_runtime.errors import ConcurrentModificationError, PersistenceError, ProjectNotFoundError
from arch_runtime.ports import UnitOfWork
from arch_runtime.replay import ReplayService
from tests.application.test_recovery import RecoveryEventIds
from tests.application.test_recovery import _command as recovery_command
from tests.application.test_recovery import _service as recovery_service
from tests.application.test_snapshots import SnapshotIds
from tests.application.test_snapshots import _command as snapshot_command
from tests.application.test_snapshots import _service as snapshot_service
from tests.fakes.adapters import FrozenClock
from tests.replay.conftest import ReplaySeed, replay_seed  # noqa: F401


def test_recovery_rejects_missing_project(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    command = recovery_command(replay_seed)
    replay_seed.uow._projects.records.clear()
    with pytest.raises(ProjectNotFoundError):
        recovery_service(replay_seed, frozen_clock, RecoveryEventIds()).recover_aggregate(command)


def test_recovery_rejects_stale_precondition(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    command = recovery_command(replay_seed).model_copy(update={"expected_record_version": 99})
    with pytest.raises(ConcurrentModificationError, match="preconditions"):
        recovery_service(replay_seed, frozen_clock, RecoveryEventIds()).recover_aggregate(command)


def test_recovery_detects_stream_change_during_evaluation(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = ReplayService(unit_of_work_factory=lambda: replay_seed.uow)
    verified = replay.replay_verified_history(replay_seed.project_id)
    service = recovery_service(replay_seed, frozen_clock, RecoveryEventIds())
    monkeypatch.setattr(
        service._replay,
        "replay_verified_history",
        lambda project_id: verified.model_copy(update={"last_stream_position": 999}),
    )
    with pytest.raises(ConcurrentModificationError, match="during recovery evaluation"):
        service.recover_aggregate(recovery_command(replay_seed))


def test_snapshot_requires_available_contract_registration(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Mock()
    registry.get_by_model.return_value = None
    monkeypatch.setattr(snapshot_module, "build_builtin_contract_registry", lambda: registry)
    service = snapshot_service(replay_seed, frozen_clock, SnapshotIds(), SnapshotPolicy())
    with pytest.raises(PersistenceError, match="registration is unavailable"):
        service.create_snapshot(snapshot_command(replay_seed, force=True))


def test_snapshot_rejects_stale_precondition(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    command = snapshot_command(replay_seed).model_copy(update={"expected_record_version": 99})
    with pytest.raises(ConcurrentModificationError, match="preconditions"):
        snapshot_service(
            replay_seed, frozen_clock, SnapshotIds(), SnapshotPolicy()
        ).create_snapshot(command)


@pytest.mark.parametrize("disappeared", [True, False])
def test_snapshot_rechecks_aggregate_inside_commit(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    disappeared: bool,
) -> None:
    service = snapshot_service(replay_seed, frozen_clock, SnapshotIds(), SnapshotPolicy())
    command = snapshot_command(replay_seed, force=True)
    calls = 0

    def factory() -> UnitOfWork:
        nonlocal calls
        calls += 1
        if calls == 2:
            if disappeared:
                replay_seed.uow._projects.records.clear()
            else:
                replay_seed.uow._projects.records[replay_seed.project_id] = (
                    replay_seed.current.model_copy(update={"record_version": 99})
                )
        return replay_seed.uow

    monkeypatch.setattr(service, "_unit_of_work_factory", factory)
    with pytest.raises(ConcurrentModificationError):
        service.create_snapshot(command)
    assert replay_seed.uow._snapshots.records == {}


@pytest.mark.parametrize("disappeared", [True, False])
def test_recovery_rechecks_aggregate_inside_commit(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    disappeared: bool,
) -> None:
    service = recovery_service(replay_seed, frozen_clock, RecoveryEventIds())
    command = recovery_command(replay_seed)
    calls = 0

    def factory() -> UnitOfWork:
        nonlocal calls
        calls += 1
        if calls == 3:
            if disappeared:
                replay_seed.uow._projects.records.clear()
            else:
                replay_seed.uow._projects.records[replay_seed.project_id] = (
                    replay_seed.current.model_copy(update={"record_version": 99})
                )
        return replay_seed.uow

    monkeypatch.setattr(service, "_unit_of_work_factory", factory)
    with pytest.raises(ConcurrentModificationError):
        service.recover_aggregate(command)
    assert replay_seed.uow.idempotency.get("project.recover", command.idempotency_key) is None
