from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from arch_kernel.contracts import DryRunResult, EventEnvelope, ProjectId, ProjectState
from arch_kernel.kernel import (
    apply_state_patch_dry_run,
    build_builtin_contract_registry,
    canonicalize_json,
    compute_fingerprint,
)
from pydantic import ValidationError

from arch_runtime.errors import ProjectNotFoundError, ReplayIntegrityError
from arch_runtime.ports import StoredSnapshot
from arch_runtime.ports.storage import SnapshotId
from arch_runtime.replay import (
    ReplayResult,
    ReplayService,
    SnapshotDisposition,
    replay_project,
)
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock
from tests.replay.conftest import ReplaySeed, save_snapshot

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parents[1] / "fixtures" / "replay"


class CountingPatchRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.evaluated_at: datetime | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> DryRunResult:
        self.calls += 1
        evaluated_at = kwargs.get("evaluated_at")
        assert isinstance(evaluated_at, datetime)
        self.evaluated_at = evaluated_at
        return apply_state_patch_dry_run(*args, **kwargs)


class UnreadableSnapshotStore:
    def get_latest(self, project_id: ProjectId) -> StoredSnapshot | None:
        from arch_runtime.errors import CorruptStoredRecordError

        raise CorruptStoredRecordError(
            "injected unreadable snapshot",
            operation="snapshot.get",
            remediation="test",
            project_id=project_id,
        )

    def save(self, snapshot: StoredSnapshot) -> None:
        raise AssertionError("replay must never save snapshots")

    def list_for_project(self, project_id: ProjectId) -> tuple[StoredSnapshot, ...]:
        self.get_latest(project_id)
        raise AssertionError("unreachable")

    def list_stored_for_project(self, project_id: ProjectId) -> tuple[StoredSnapshot, ...]:
        self.get_latest(project_id)
        raise AssertionError("unreachable")

    def replace(self, snapshot: StoredSnapshot, *, expected_record_fingerprint: str) -> None:
        raise AssertionError("replay must never replace snapshots")

    def delete(self, project_id: ProjectId, snapshot_id: SnapshotId) -> None:
        raise AssertionError("replay must never delete snapshots")


def _service(seed: ReplaySeed, runner: CountingPatchRunner | None = None) -> ReplayService:
    return ReplayService(
        unit_of_work_factory=lambda: seed.uow,
        patch_runner=runner or apply_state_patch_dry_run,
    )


def _replace_tail_envelope(seed: ReplaySeed, document: dict[str, Any]) -> None:
    envelope = EventEnvelope.model_validate_json(canonicalize_json(document), strict=True)
    tail = seed.uow._events.records[-1]
    seed.uow._events.records[-1] = tail.model_copy(
        update={
            "event_type": envelope.event_type,
            "event_json": canonicalize_json(envelope),
            "event_fingerprint": compute_fingerprint(envelope),
        }
    )


def test_full_replay_reconstructs_and_verifies_operational_aggregate_without_writes(
    replay_seed: ReplaySeed,
) -> None:
    runner = CountingPatchRunner()
    before = (
        deepcopy(replay_seed.uow._projects.records),
        deepcopy(replay_seed.uow._events.records),
        deepcopy(replay_seed.uow._snapshots.records),
        deepcopy(replay_seed.uow._idempotency.records),
    )

    result = replay_project(_service(replay_seed, runner), replay_seed.project_id)

    assert result.reconstructed_state == ProjectState.model_validate_json(
        replay_seed.current.state_json, strict=True
    )
    assert result.event_count == 2
    assert result.snapshot_disposition is SnapshotDisposition.NOT_AVAILABLE
    assert result.record_fingerprint == replay_seed.current.record_fingerprint
    assert runner.calls == 1
    transition_event = EventEnvelope.model_validate_json(
        replay_seed.uow._events.records[-1].event_json, strict=True
    )
    validation = transition_event.model_dump(mode="json")["payload"]["validation_result"]
    assert runner.evaluated_at is not None
    assert runner.evaluated_at.isoformat().replace("+00:00", "Z") == validation["evaluated_at"]
    after = (
        replay_seed.uow._projects.records,
        replay_seed.uow._events.records,
        replay_seed.uow._snapshots.records,
        replay_seed.uow._idempotency.records,
    )
    assert after == before


def test_valid_snapshot_is_derived_optimization_and_skips_prior_patch_application(
    replay_seed: ReplaySeed,
) -> None:
    snapshot = save_snapshot(replay_seed)
    runner = CountingPatchRunner()

    result = _service(replay_seed, runner).replay_project(replay_seed.project_id)

    assert result.snapshot_disposition is SnapshotDisposition.USED
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.snapshot_findings == ()
    assert result.record_fingerprint == replay_seed.current.record_fingerprint
    assert runner.calls == 0


def test_invalid_snapshot_falls_back_to_full_replay_with_visible_finding(
    replay_seed: ReplaySeed,
) -> None:
    snapshot = save_snapshot(replay_seed, valid=False)
    runner = CountingPatchRunner()

    result = _service(replay_seed, runner).replay_project(replay_seed.project_id)

    assert result.snapshot_disposition is SnapshotDisposition.BYPASSED_INVALID
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.snapshot_findings[0].code == "replay.snapshot_invalid"
    assert result.record_fingerprint == replay_seed.current.record_fingerprint
    assert runner.calls == 1


def test_most_recent_valid_snapshot_is_selected_after_newer_invalid_checkpoint(
    replay_seed: ReplaySeed,
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
    invalid = save_snapshot(replay_seed, valid=False)
    runner = CountingPatchRunner()

    result = _service(replay_seed, runner).replay_project(replay_seed.project_id)

    assert result.snapshot_disposition is SnapshotDisposition.USED
    assert result.snapshot_id == "SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R01"
    assert result.snapshot_findings[0].snapshot_id == invalid.snapshot_id
    assert runner.calls == 1


def test_unreadable_snapshot_falls_back_without_snapshot_identity(
    replay_seed: ReplaySeed,
) -> None:
    replay_seed.uow.snapshots = UnreadableSnapshotStore()

    result = _service(replay_seed).replay_project(replay_seed.project_id)

    assert result.snapshot_disposition is SnapshotDisposition.BYPASSED_INVALID
    assert result.snapshot_id is None
    assert result.snapshot_findings[0].snapshot_id is None


def test_payload_event_name_is_not_used_for_replay_routing(replay_seed: ReplaySeed) -> None:
    tail = replay_seed.uow._events.records[-1]
    document = EventEnvelope.model_validate_json(tail.event_json, strict=True).model_dump(
        mode="json"
    )
    document["payload"]["event_name"] = "descriptive.value.changed"
    _replace_tail_envelope(replay_seed, document)

    result = _service(replay_seed).replay_project(replay_seed.project_id)

    assert result.record_fingerprint == replay_seed.current.record_fingerprint


def test_missing_project_is_not_reported_as_domain_rejection(
    frozen_clock: FrozenClock,
) -> None:
    service = ReplayService(unit_of_work_factory=lambda: FakeUnitOfWork(frozen_clock))
    missing = ProjectId("PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2R99")

    with pytest.raises(ProjectNotFoundError):
        service.replay_project(missing)


def test_existing_aggregate_without_creation_event_fails_closed(replay_seed: ReplaySeed) -> None:
    replay_seed.uow._events.records.clear()

    with pytest.raises(ReplayIntegrityError, match="empty"):
        _service(replay_seed).replay_project(replay_seed.project_id)


def test_invalid_event_json_fails_closed(replay_seed: ReplaySeed) -> None:
    first = replay_seed.uow._events.records[0]
    replay_seed.uow._events.records[0] = first.model_copy(update={"event_json": b"{}"})

    with pytest.raises(ReplayIntegrityError, match="event JSON"):
        _service(replay_seed).replay_project(replay_seed.project_id)


def test_malformed_adjacent_evidence_fails_closed(replay_seed: ReplaySeed) -> None:
    tail = replay_seed.uow._events.records[-1]
    document = EventEnvelope.model_validate_json(tail.event_json, strict=True).model_dump(
        mode="json"
    )
    document["payload"]["before"]["record_version"] = "invalid"
    _replace_tail_envelope(replay_seed, document)

    with pytest.raises(ReplayIntegrityError, match="malformed"):
        _service(replay_seed).replay_project(replay_seed.project_id)


def test_replay_result_rejects_contradictory_evidence(replay_seed: ReplaySeed) -> None:
    valid = _service(replay_seed).replay_project(replay_seed.project_id)
    values = valid.model_dump()
    contradictions = (
        {"project_id": ProjectId("PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2R99")},
        {"record_version": valid.record_version + 1},
        {"first_stream_position": valid.last_stream_position + 1},
        {"snapshot_id": "SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R07"},
        {"snapshot_disposition": SnapshotDisposition.USED},
        {
            "snapshot_disposition": SnapshotDisposition.BYPASSED_INVALID,
            "snapshot_findings": (),
        },
    )
    for change in contradictions:
        with pytest.raises(ValidationError):
            ReplayResult.model_validate({**values, **change}, strict=True)


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURES / "corruption_cases.json").read_text(encoding="utf-8")),
)
def test_corruption_fixtures_fail_closed(replay_seed: ReplaySeed, case: str) -> None:
    if case == "broken_chain":
        tail = replay_seed.uow._events.records[-1]
        replay_seed.uow._events.records[-1] = tail.model_copy(
            update={"previous_event_fingerprint": "sha256:" + "f" * 64}
        )
    elif case == "missing_creation":
        first = replay_seed.uow._events.records[0]
        document = EventEnvelope.model_validate_json(first.event_json, strict=True).model_dump(
            mode="json"
        )
        document["event_type"] = "project.aggregate.transition_applied"
        envelope = EventEnvelope.model_validate_json(canonicalize_json(document), strict=True)
        replacement = first.model_copy(
            update={
                "event_type": envelope.event_type,
                "event_json": canonicalize_json(envelope),
                "event_fingerprint": compute_fingerprint(envelope),
            }
        )
        replay_seed.uow._events.records[0] = replacement
        tail = replay_seed.uow._events.records[-1]
        replay_seed.uow._events.records[-1] = tail.model_copy(
            update={"previous_event_fingerprint": replacement.event_fingerprint}
        )
    elif case == "unsupported_event_type":
        tail = replay_seed.uow._events.records[-1]
        document = EventEnvelope.model_validate_json(tail.event_json, strict=True).model_dump(
            mode="json"
        )
        document["event_type"] = "project.aggregate.unknown"
        _replace_tail_envelope(replay_seed, document)
    elif case == "missing_patch":
        tail = replay_seed.uow._events.records[-1]
        document = EventEnvelope.model_validate_json(tail.event_json, strict=True).model_dump(
            mode="json"
        )
        document["payload"]["patch"] = None
        _replace_tail_envelope(replay_seed, document)
    elif case == "wrong_project_identity":
        tail = replay_seed.uow._events.records[-1]
        document = EventEnvelope.model_validate_json(tail.event_json, strict=True).model_dump(
            mode="json"
        )
        other = "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2R99"
        document["project_id"] = other
        document["aggregate"]["target_id"] = other
        _replace_tail_envelope(replay_seed, document)
    elif case == "out_of_order":
        first, tail = replay_seed.uow._events.records
        replay_seed.uow._events.records = [
            first.model_copy(update={"stream_position": 2}),
            tail.model_copy(update={"stream_position": 1}),
        ]
    else:
        replay_seed.uow._projects.records[replay_seed.project_id] = replay_seed.created

    with pytest.raises(ReplayIntegrityError):
        _service(replay_seed).replay_project(replay_seed.project_id)
