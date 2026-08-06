# ruff: noqa: F811
from __future__ import annotations

from copy import deepcopy

import pytest
from arch_kernel.contracts import EventId, ProjectState

from arch_runtime.application import RecoverAggregateCommand, RecoveryService
from arch_runtime.errors import IdempotencyConflictError, PersistenceError, ReplayIntegrityError
from arch_runtime.replay import ReplayService, SnapshotDisposition
from tests.fakes.adapters import FrozenClock
from tests.replay.conftest import ReplaySeed, replay_seed, save_snapshot  # noqa: F401

pytestmark = pytest.mark.unit


class RecoveryEventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R08")


def _service(seed: ReplaySeed, clock: FrozenClock, ids: RecoveryEventIds) -> RecoveryService:
    replay = ReplayService(unit_of_work_factory=lambda: seed.uow)
    return RecoveryService(
        unit_of_work_factory=lambda: seed.uow,
        replay_service=replay,
        clock=clock,
        event_ids=ids,
    )


def _diverge(seed: ReplaySeed) -> None:
    current = seed.uow.projects.get(seed.project_id)
    assert current is not None
    state = ProjectState.model_validate_json(current.state_json, strict=True)
    metadata = state.metadata.model_copy(update={"name": "Divergent materialized name"})
    divergent = state.model_copy(update={"metadata": metadata})
    seed.uow.projects.save(
        divergent,
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
    )


def _command(seed: ReplaySeed, *, key: str = "project:recover:001") -> RecoverAggregateCommand:
    current = seed.uow.projects.get(seed.project_id)
    assert current is not None
    return RecoverAggregateCommand(
        idempotency_key=key,
        project_id=seed.project_id,
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
        expected_content_fingerprint=current.content_fingerprint,
        reason="Restore the materialized aggregate from verified history.",
        actor_id="operator:recovery",
    )


def test_explicit_recovery_replaces_divergent_aggregate_and_appends_audit_event(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _diverge(replay_seed)
    before_events = len(replay_seed.uow.events.read_stream(replay_seed.project_id))
    ids = RecoveryEventIds()

    result = _service(replay_seed, frozen_clock, ids).recover_aggregate(_command(replay_seed))

    assert result.recovered
    assert result.event_id is not None
    assert len(replay_seed.uow.events.read_stream(replay_seed.project_id)) == before_events + 1
    assert replay_seed.uow.events.read_stream(replay_seed.project_id)[-1].event_type == (
        "project.aggregate.recovered"
    )
    current = replay_seed.uow.projects.get(replay_seed.project_id)
    assert current is not None
    assert current.record_fingerprint == result.after_record_fingerprint
    assert ids.calls == 1


def test_recovery_is_exactly_idempotent(replay_seed: ReplaySeed, frozen_clock: FrozenClock) -> None:
    _diverge(replay_seed)
    command = _command(replay_seed)
    ids = RecoveryEventIds()
    service = _service(replay_seed, frozen_clock, ids)

    first = service.recover_aggregate(command)
    repeated = service.recover_aggregate(command)

    assert repeated == first
    assert ids.calls == 1
    conflicting = command.model_copy(update={"reason": "Different recovery intent."})
    with pytest.raises(IdempotencyConflictError):
        service.recover_aggregate(conflicting)


def test_correct_aggregate_is_noop_with_completed_idempotency_and_no_event(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    before_events = deepcopy(replay_seed.uow._events.records)
    ids = RecoveryEventIds()

    result = _service(replay_seed, frozen_clock, ids).recover_aggregate(_command(replay_seed))

    assert not result.recovered
    assert result.event_id is None
    assert replay_seed.uow._events.records == before_events
    assert ids.calls == 0
    record = replay_seed.uow.idempotency.get("project.recover", "project:recover:001")
    assert record is not None
    assert record.status.value == "completed"


def test_invalid_stream_prohibits_recovery_without_any_write(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _diverge(replay_seed)
    tail = replay_seed.uow._events.records[-1]
    replay_seed.uow._events.records[-1] = tail.model_copy(
        update={"previous_event_fingerprint": "sha256:" + "f" * 64}
    )
    before = (
        deepcopy(replay_seed.uow._projects.records),
        deepcopy(replay_seed.uow._idempotency.records),
    )

    with pytest.raises(ReplayIntegrityError):
        _service(replay_seed, frozen_clock, RecoveryEventIds()).recover_aggregate(
            _command(replay_seed)
        )

    assert (replay_seed.uow._projects.records, replay_seed.uow._idempotency.records) == before


def test_invalid_snapshot_falls_back_to_full_replay_before_recovery(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    save_snapshot(replay_seed, valid=False)
    _diverge(replay_seed)

    result = _service(replay_seed, frozen_clock, RecoveryEventIds()).recover_aggregate(
        _command(replay_seed)
    )

    assert result.recovered
    assert result.replay_result.snapshot_disposition is SnapshotDisposition.BYPASSED_INVALID


@pytest.mark.parametrize("boundary", ["project", "event", "idempotency"])
def test_recovery_faults_roll_back_all_surfaces(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _diverge(replay_seed)
    before = (
        deepcopy(replay_seed.uow._projects.records),
        deepcopy(replay_seed.uow._events.records),
        deepcopy(replay_seed.uow._idempotency.records),
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise PersistenceError(
            "injected recovery boundary failure",
            operation=f"test.{boundary}",
            remediation="test",
            project_id=replay_seed.project_id,
        )

    target = {
        "project": replay_seed.uow._projects,
        "event": replay_seed.uow._events,
        "idempotency": replay_seed.uow._idempotency,
    }[boundary]
    method = {"project": "save", "event": "append", "idempotency": "complete"}[boundary]
    monkeypatch.setattr(target, method, fail)

    with pytest.raises(PersistenceError):
        _service(replay_seed, frozen_clock, RecoveryEventIds()).recover_aggregate(
            _command(replay_seed)
        )

    assert (
        replay_seed.uow._projects.records,
        replay_seed.uow._events.records,
        replay_seed.uow._idempotency.records,
    ) == before
