# ruff: noqa: F811
from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy

import pytest
from arch_kernel.contracts import (
    ContractRegistration,
    ContractStatus,
    EventEnvelope,
    EventId,
    MigrationDefinition,
    MigrationDirection,
    MigrationId,
    MigrationOperation,
    MigrationOperationType,
    MigrationPath,
    MigrationStatus,
    ProjectState,
    SemanticVersion,
)
from arch_kernel.kernel import (
    ContractRegistry,
    MigrationRegistry,
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
    generate_contract_schema,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arch_runtime.application import (
    ApplyStoredContractMigrationCommand,
    ApplyStoredContractMigrationResult,
    PlanStoredContractMigrationCommand,
    StoredContractKind,
    StoredContractMigrationService,
    apply_stored_contract_migration,
    dry_run_stored_contract_migration,
)
from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    IdempotencyConflictError,
    PersistenceError,
    ProjectNotFoundError,
    ReplayIntegrityError,
    UnsupportedStoredContractError,
)
from arch_runtime.replay import ReplayService
from tests.fakes.adapters import FrozenClock
from tests.replay.conftest import ReplaySeed, replay_seed, save_snapshot  # noqa: F401

pytestmark = pytest.mark.unit
OLD = SemanticVersion.parse("0.9.0")
CURRENT = SemanticVersion.parse("1.0.0")


class HistoricalProjectState(ProjectState):
    pass


class HistoricalEventEnvelope(EventEnvelope):
    pass


class MigrationEventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R09")


def _historical_registration(
    current: ContractRegistration,
    version: SemanticVersion,
    historical_model: type[ProjectState] | type[EventEnvelope],
) -> ContractRegistration:
    descriptor = current.descriptor.model_copy(
        update={
            "version": version,
            "status": ContractStatus.DEPRECATED,
            "schema_id": f"https://schemas.arch.local/{current.descriptor.canonical_name}/{version}",
            "schema_filename": (
                f"contracts/{current.descriptor.canonical_name}.{version}.schema.json"
            ),
            "python_module": historical_model.__module__,
            "python_qualname": historical_model.__qualname__,
            "model_fingerprint": compute_fingerprint(
                {
                    "module": historical_model.__module__,
                    "qualname": historical_model.__qualname__,
                }
            ),
            "replaces": None,
            "schema_fingerprint": "sha256:" + "0" * 64,
        }
    )
    provisional = ContractRegistration(descriptor, historical_model)
    schema = generate_contract_schema(provisional)
    return ContractRegistration(
        descriptor.model_copy(update={"schema_fingerprint": schema.schema_fingerprint}),
        historical_model,
    )


def _definition(
    *,
    migration_id: str,
    key: str,
    canonical_name: str,
    path: str,
) -> MigrationDefinition:
    return MigrationDefinition(
        migration_id=MigrationId.from_str(migration_id),
        key=key,
        title="Stored contract fixture migration",
        description="Upgrade one historical runtime fixture to the current contract.",
        canonical_name=canonical_name,
        source_version=OLD,
        target_version=CURRENT,
        direction=MigrationDirection.UPGRADE,
        status=MigrationStatus.ACTIVE,
        priority=0,
        operations=(
            MigrationOperation(
                operation_type=MigrationOperationType.REPLACE,
                path=MigrationPath.parse(path),
                value="1.0.0",
            ),
        ),
        lossless=True,
        reversible=False,
        contract_version=CURRENT,
        schema_uri="https://schemas.arch.local/arch.runtime/test_migration/1.0.0",
    )


def _registries() -> tuple[ContractRegistry, MigrationRegistry, str, str]:
    builtins = build_builtin_contract_registry()
    project = builtins.get_by_model(ProjectState)
    event = builtins.get_by_model(EventEnvelope)
    assert project is not None
    assert event is not None
    historical_project = _historical_registration(project, OLD, HistoricalProjectState)
    historical_event = _historical_registration(event, OLD, HistoricalEventEnvelope)
    contracts = ContractRegistry((*builtins.registrations, historical_project, historical_event))
    definitions = (
        _definition(
            migration_id="MIG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            key="runtime.project_state.upgrade",
            canonical_name=project.descriptor.canonical_name,
            path="/contract_version",
        ),
        _definition(
            migration_id="MIG-01ARZ3NDEKTSV4RRFFQ69G5FAW",
            key="runtime.event_envelope.upgrade",
            canonical_name=event.descriptor.canonical_name,
            path="/schema_version",
        ),
    )
    return (
        contracts,
        MigrationRegistry(definitions, contracts),
        historical_project.descriptor.schema_fingerprint,
        historical_event.descriptor.schema_fingerprint,
    )


def _make_historical(seed: ReplaySeed) -> None:
    existing = seed.uow.projects.get_stored(seed.project_id)
    assert existing is not None
    if existing.contract_version == OLD:
        return
    _, _, project_schema, _ = _registries()
    current = existing
    state = ProjectState.model_validate_json(current.state_json, strict=True)
    old_state = state.model_copy(update={"contract_version": OLD})
    seed.uow._projects.records[seed.project_id] = current.model_copy(
        update={
            "contract_version": OLD,
            "schema_fingerprint": project_schema,
            "record_fingerprint": compute_fingerprint(old_state),
            "content_fingerprint": compute_content_fingerprint(old_state),
            "state_json": canonicalize_json(old_state),
        }
    )
    snapshot = save_snapshot(seed)
    snapshot_state = ProjectState.model_validate_json(snapshot.state_json, strict=True)
    old_snapshot_state = snapshot_state.model_copy(update={"contract_version": OLD})
    seed.uow._snapshots.records[seed.project_id][0] = snapshot.model_copy(
        update={
            "contract_version": OLD,
            "schema_fingerprint": project_schema,
            "record_fingerprint": compute_fingerprint(old_snapshot_state),
            "content_fingerprint": compute_content_fingerprint(old_snapshot_state),
            "state_json": canonicalize_json(old_snapshot_state),
        }
    )
    previous: str | None = None
    historical_events = []
    for stored in seed.uow._events.records:
        envelope = EventEnvelope.model_validate_json(stored.event_json, strict=True)
        old_envelope = envelope.model_copy(update={"schema_version": OLD})
        fingerprint = compute_fingerprint(old_envelope)
        historical_events.append(
            stored.model_copy(
                update={
                    "schema_version": OLD,
                    "event_json": canonicalize_json(old_envelope),
                    "event_fingerprint": fingerprint,
                    "previous_event_fingerprint": previous,
                }
            )
        )
        previous = fingerprint
    seed.uow._events.records = historical_events


def _plan(seed: ReplaySeed) -> PlanStoredContractMigrationCommand:
    aggregate = seed.uow.projects.get_stored(seed.project_id)
    tail = seed.uow.events.read_stored_stream(seed.project_id)[-1]
    assert aggregate is not None
    return PlanStoredContractMigrationCommand(
        project_id=seed.project_id,
        target_project_version=CURRENT,
        target_event_version=CURRENT,
        expected_record_version=aggregate.record_version,
        expected_record_fingerprint=aggregate.record_fingerprint,
        expected_content_fingerprint=aggregate.content_fingerprint,
        expected_stream_position=tail.stream_position,
        expected_stream_fingerprint=tail.event_fingerprint,
    )


def _service(
    seed: ReplaySeed, clock: FrozenClock, ids: MigrationEventIds
) -> StoredContractMigrationService:
    contracts, migrations, _, _ = _registries()
    replay = ReplayService(unit_of_work_factory=lambda: seed.uow)
    return StoredContractMigrationService(
        unit_of_work_factory=lambda: seed.uow,
        clock=clock,
        event_ids=ids,
        contract_registry=contracts,
        migration_registry=migrations,
        replay_verifier=replay,
    )


def _apply_command(
    plan: PlanStoredContractMigrationCommand, fingerprint: str, *, key: str = "migration:001"
) -> ApplyStoredContractMigrationCommand:
    return ApplyStoredContractMigrationCommand(
        idempotency_key=key,
        plan=plan,
        expected_plan_fingerprint=fingerprint,
        reason="Upgrade verified historical contracts.",
        actor_id="operator:migration",
    )


def _rewrite_migration_audit(seed: ReplaySeed, mutate: Callable[[dict[str, object]], None]) -> None:
    stored = seed.uow._events.records[-1]
    envelope = EventEnvelope.model_validate_json(stored.event_json, strict=True)
    document = envelope.model_dump(mode="json")
    payload = document["payload"]
    assert isinstance(payload, dict)
    mutate(payload)
    altered = EventEnvelope.model_validate_json(canonicalize_json(document), strict=True)
    seed.uow._events.records[-1] = stored.model_copy(
        update={
            "event_json": canonicalize_json(altered),
            "event_fingerprint": compute_fingerprint(altered),
        }
    )


def test_dry_run_is_pure_deterministic_and_covers_all_stored_contract_kinds(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    before = deepcopy(
        (
            replay_seed.uow._projects.records,
            replay_seed.uow._events.records,
            replay_seed.uow._snapshots.records,
            replay_seed.uow._idempotency.records,
        )
    )

    first = service.dry_run(_plan(replay_seed))
    second = service.dry_run(_plan(replay_seed))

    assert first.success
    assert first.changed
    assert first.plan_fingerprint == second.plan_fingerprint
    assert {item.kind for item in first.items} == set(StoredContractKind)
    assert len(first.items) == 4
    assert (
        replay_seed.uow._projects.records,
        replay_seed.uow._events.records,
        replay_seed.uow._snapshots.records,
        replay_seed.uow._idempotency.records,
    ) == before


def test_apply_migrates_materialized_records_and_preserves_historical_event_rows(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    ids = MigrationEventIds()
    service = _service(replay_seed, frozen_clock, ids)
    plan = _plan(replay_seed)
    dry_run = service.dry_run(plan)
    original_events = deepcopy(replay_seed.uow._events.records)

    result = service.apply(_apply_command(plan, dry_run.plan_fingerprint))

    assert result.success
    assert result.changed
    assert result.event_id is not None
    aggregate = replay_seed.uow.projects.get(replay_seed.project_id)
    assert aggregate is not None
    assert aggregate.contract_version == CURRENT
    snapshots = replay_seed.uow.snapshots.list_for_project(replay_seed.project_id)
    assert snapshots[0].contract_version == CURRENT
    assert replay_seed.uow._events.records[:2] == original_events
    assert replay_seed.uow._events.records[-1].event_type == ("project.aggregate.contract_migrated")
    assert (
        ReplayService(unit_of_work_factory=lambda: replay_seed.uow)
        .replay_project(replay_seed.project_id)
        .record_fingerprint
        == aggregate.record_fingerprint
    )
    assert ids.calls == 1


def test_exact_retry_and_fresh_key_after_migration_are_idempotent_noops(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    dry_run = service.dry_run(plan)
    command = _apply_command(plan, dry_run.plan_fingerprint)
    first = service.apply(command)

    assert service.apply(command) == first
    with pytest.raises(IdempotencyConflictError):
        service.apply(command.model_copy(update={"reason": "Different intent."}))

    current_plan = _plan(replay_seed)
    current_dry_run = service.dry_run(current_plan)
    assert current_dry_run.success
    assert not current_dry_run.changed
    noop = service.apply(
        _apply_command(current_plan, current_dry_run.plan_fingerprint, key="migration:002")
    )
    assert noop.success
    assert not noop.changed
    assert noop.event_id is None


def test_migration_result_contracts_reject_contradictory_evidence(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    dry_run = dry_run_stored_contract_migration(service, plan)
    command = _apply_command(plan, dry_run.plan_fingerprint)

    item_data = dry_run.items[0].model_dump(mode="json")
    item_data["after_payload"] = None
    with pytest.raises(ValueError, match="complete after evidence"):
        type(dry_run.items[0]).model_validate_json(canonicalize_json(item_data), strict=True)

    dry_data = dry_run.model_dump(mode="json")
    for update, message in (
        ({"changed": False}, "changed must reflect"),
        ({"success": False}, "success must reflect"),
        ({"projected_state": None}, "requires projected"),
    ):
        with pytest.raises(ValueError, match=message):
            type(dry_run).model_validate_json(canonicalize_json(dry_data | update), strict=True)

    applied = apply_stored_contract_migration(service, command)
    applied_data = applied.model_dump(mode="json")
    with pytest.raises(ValueError, match="agree with its dry run"):
        ApplyStoredContractMigrationResult.model_validate_json(
            canonicalize_json(applied_data | {"success": False}), strict=True
        )
    with pytest.raises(ValueError, match="requires audit event"):
        ApplyStoredContractMigrationResult.model_validate_json(
            canonicalize_json(applied_data | {"event_id": None}), strict=True
        )

    current_plan = _plan(replay_seed)
    current_dry_run = service.dry_run(current_plan)
    noop = service.apply(
        _apply_command(current_plan, current_dry_run.plan_fingerprint, key="migration:contract")
    )
    noop_data = noop.model_dump(mode="json")
    with pytest.raises(ValueError, match="cannot contain event evidence"):
        ApplyStoredContractMigrationResult.model_validate_json(
            canonicalize_json(
                noop_data
                | {
                    "event_id": "EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R09",
                    "stream_position": 99,
                }
            ),
            strict=True,
        )

    assert "idempotency_key" not in command.request_payload()


def test_stale_plan_and_corrupt_stream_prohibit_writes(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    service.dry_run(plan)
    with pytest.raises(ConcurrentModificationError):
        service.apply(_apply_command(plan, "sha256:" + "f" * 64))

    tail = replay_seed.uow._events.records[-1]
    replay_seed.uow._events.records[-1] = tail.model_copy(
        update={"previous_event_fingerprint": "sha256:" + "e" * 64}
    )
    before = deepcopy(replay_seed.uow._idempotency.records)
    with pytest.raises((CorruptStoredRecordError, ReplayIntegrityError)):
        service.dry_run(_plan(replay_seed))
    assert replay_seed.uow._idempotency.records == before


def test_missing_storage_and_noncurrent_targets_fail_closed(
    replay_seed: ReplaySeed, frozen_clock: FrozenClock
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)

    project = replay_seed.uow._projects.records.pop(replay_seed.project_id)
    with pytest.raises(ProjectNotFoundError):
        service.dry_run(plan)
    replay_seed.uow._projects.records[replay_seed.project_id] = project

    events = replay_seed.uow._events.records
    replay_seed.uow._events.records = []
    with pytest.raises(CorruptStoredRecordError, match="stream is empty"):
        service.dry_run(plan)
    replay_seed.uow._events.records = events

    with pytest.raises(UnsupportedStoredContractError):
        service.dry_run(
            plan.model_copy(
                update={
                    "target_project_version": OLD,
                    "target_event_version": OLD,
                }
            )
        )


@pytest.mark.parametrize("corruption", ["invalid_json", "array", "noncanonical", "identity"])
def test_historical_record_corruption_is_rejected_by_plan_and_replay(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    corruption: str,
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    stored = replay_seed.uow._events.records[0]
    if corruption == "invalid_json":
        altered = stored.model_copy(update={"event_json": b"{"})
    elif corruption == "array":
        altered = stored.model_copy(
            update={"event_json": b"[]", "event_fingerprint": compute_fingerprint([])}
        )
    elif corruption == "noncanonical":
        value = json.loads(stored.event_json)
        altered = stored.model_copy(
            update={
                "event_json": json.dumps(value, indent=2).encode(),
                "event_fingerprint": compute_fingerprint(value),
            }
        )
    else:
        altered = stored.model_copy(update={"event_type": "project.aggregate.wrong"})
    replay_seed.uow._events.records[0] = altered

    with pytest.raises(CorruptStoredRecordError):
        service.dry_run(_plan(replay_seed))
    with pytest.raises(ReplayIntegrityError):
        ReplayService(unit_of_work_factory=lambda: replay_seed.uow).replay_project(
            replay_seed.project_id
        )


@pytest.mark.parametrize(
    "corruption",
    ["missing_list", "non_object", "missing_field", "unknown_source", "bad_target", "duplicate"],
)
def test_corrupt_migration_projections_are_rejected_by_plan_and_replay(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    corruption: str,
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    dry_run = service.dry_run(plan)
    service.apply(_apply_command(plan, dry_run.plan_fingerprint))

    def corrupt(payload: dict[str, object]) -> None:
        projections = payload["event_projections"]
        assert isinstance(projections, list)
        if corruption == "missing_list":
            payload["event_projections"] = None
        elif corruption == "non_object":
            payload["event_projections"] = ["invalid"]
        else:
            projection = projections[0]
            assert isinstance(projection, dict)
            if corruption == "missing_field":
                projection.pop("target_schema_version")
            elif corruption == "unknown_source":
                projection["source_event_fingerprint"] = "sha256:" + "f" * 64
            elif corruption == "bad_target":
                projection["target_event_fingerprint"] = "sha256:" + "e" * 64
            else:
                payload["event_projections"] = [projection, deepcopy(projection)]

    _rewrite_migration_audit(replay_seed, corrupt)

    with pytest.raises(CorruptStoredRecordError):
        service.dry_run(_plan(replay_seed))
    with pytest.raises(ReplayIntegrityError):
        ReplayService(unit_of_work_factory=lambda: replay_seed.uow).replay_project(
            replay_seed.project_id
        )


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(
    field=st.sampled_from(
        (
            "expected_record_version",
            "expected_record_fingerprint",
            "expected_content_fingerprint",
            "expected_stream_position",
            "expected_stream_fingerprint",
        )
    ),
    delta=st.integers(min_value=1, max_value=10_000),
)
def test_any_stale_plan_precondition_is_rejected_without_writes(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    field: str,
    delta: int,
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    replacement: int | str
    if field in {"expected_record_version", "expected_stream_position"}:
        replacement = getattr(plan, field) + delta
    else:
        replacement = "sha256:" + f"{delta:064x}"[-64:]
    before = deepcopy(
        (
            replay_seed.uow._projects.records,
            replay_seed.uow._events.records,
            replay_seed.uow._snapshots.records,
            replay_seed.uow._idempotency.records,
        )
    )

    with pytest.raises(ConcurrentModificationError):
        service.dry_run(plan.model_copy(update={field: replacement}))

    assert (
        replay_seed.uow._projects.records,
        replay_seed.uow._events.records,
        replay_seed.uow._snapshots.records,
        replay_seed.uow._idempotency.records,
    ) == before


@pytest.mark.parametrize("boundary", ["project", "snapshot", "event", "idempotency"])
def test_faults_roll_back_every_migration_surface(
    replay_seed: ReplaySeed,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _make_historical(replay_seed)
    service = _service(replay_seed, frozen_clock, MigrationEventIds())
    plan = _plan(replay_seed)
    dry_run = service.dry_run(plan)
    before = deepcopy(
        (
            replay_seed.uow._projects.records,
            replay_seed.uow._events.records,
            replay_seed.uow._snapshots.records,
            replay_seed.uow._idempotency.records,
        )
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise PersistenceError(
            "injected migration failure",
            operation=f"test.{boundary}",
            remediation="test",
            project_id=replay_seed.project_id,
        )

    target, method = {
        "project": (replay_seed.uow._projects, "save"),
        "snapshot": (replay_seed.uow._snapshots, "replace"),
        "event": (replay_seed.uow._events, "append"),
        "idempotency": (replay_seed.uow._idempotency, "complete"),
    }[boundary]
    monkeypatch.setattr(target, method, fail)
    with pytest.raises(PersistenceError):
        service.apply(_apply_command(plan, dry_run.plan_fingerprint))
    assert (
        replay_seed.uow._projects.records,
        replay_seed.uow._events.records,
        replay_seed.uow._snapshots.records,
        replay_seed.uow._idempotency.records,
    ) == before
