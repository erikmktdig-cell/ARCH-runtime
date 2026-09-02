from __future__ import annotations

import json
from typing import Any

from arch_kernel.contracts import (
    ContractRegistration,
    ContractStatus,
    EventId,
    ProjectState,
    SemanticVersion,
    VersionedRecord,
)
from arch_kernel.kernel import (
    ContractRegistry,
    MigrationRegistry,
    build_builtin_contract_registry,
    build_project_state_v1_to_v2_migration,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
    generate_contract_schema,
)
from pydantic import BaseModel, Field, create_model

from arch_runtime.application import (
    ApplyStoredContractMigrationCommand,
    CreateProjectCommand,
    CreateProjectService,
    PlanStoredContractMigrationCommand,
    StoredContractKind,
    StoredContractMigrationService,
)
from arch_runtime.ports import StoredSnapshot
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock

PROJECT_V1 = SemanticVersion.parse("1.0.0")
PROJECT_V2 = SemanticVersion.parse("2.0.0")
EVENT_V1 = SemanticVersion.parse("1.0.0")


class MigrationEventIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self) -> EventId:
        self.calls += 1
        return EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R29")


def _project_state_v1_model() -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, model_field in ProjectState.model_fields.items():
        if name == "workflow_registry":
            continue
        if model_field.is_required():
            default: object = ...
        elif model_field.default_factory is not None:
            default = Field(default_factory=model_field.default_factory)
        else:
            default = model_field.default
        fields[name] = (model_field.annotation, default)
    return create_model("ProjectStateV1", __base__=VersionedRecord, **fields)


def _historical_registration(current: ContractRegistration) -> ContractRegistration:
    model = _project_state_v1_model()
    descriptor = current.descriptor.model_copy(
        update={
            "version": PROJECT_V1,
            "status": ContractStatus.DEPRECATED,
            "schema_id": "https://schemas.arch.local/arch.project.project_state/1.0.0",
            "schema_filename": "contracts/arch.project.project_state.1.0.0.schema.json",
            "python_module": model.__module__,
            "python_qualname": model.__qualname__,
            "model_fingerprint": compute_fingerprint(
                {"module": model.__module__, "qualname": model.__qualname__}
            ),
            "replaces": None,
            "schema_fingerprint": "sha256:" + "0" * 64,
        }
    )
    provisional = ContractRegistration(descriptor, model)
    schema = generate_contract_schema(provisional)
    return ContractRegistration(
        descriptor.model_copy(update={"schema_fingerprint": schema.schema_fingerprint}), model
    )


def _registries() -> tuple[ContractRegistry, MigrationRegistry, ContractRegistration]:
    builtins = build_builtin_contract_registry()
    current = builtins.get_by_model(ProjectState)
    assert current is not None
    historical = _historical_registration(current)
    contracts = ContractRegistry((*builtins.registrations, historical))
    migrations = MigrationRegistry((build_project_state_v1_to_v2_migration(),), contracts)
    return contracts, migrations, historical


def _make_v1(
    uow: FakeUnitOfWork, project_id: object, historical: ContractRegistration
) -> tuple[object, bytes]:
    stored = uow.projects.get_stored(project_id)  # type: ignore[arg-type]
    assert stored is not None
    state = json.loads(stored.state_json)
    state.pop("workflow_registry")
    state["contract_version"] = "1.0.0"
    state["schema_uri"] = "urn:arch:contracts:project_state:1.0.0"
    state_json = canonicalize_json(state)
    record_fingerprint = compute_fingerprint(state)
    content_fingerprint = compute_content_fingerprint(state)
    uow._projects.records[stored.project_id] = stored.model_copy(
        update={
            "contract_version": PROJECT_V1,
            "schema_fingerprint": historical.descriptor.schema_fingerprint,
            "record_fingerprint": record_fingerprint,
            "content_fingerprint": content_fingerprint,
            "state_json": state_json,
        }
    )
    event = uow._events.records[0]
    envelope = json.loads(event.event_json)
    envelope["payload"]["state"] = state
    envelope["payload"]["record_fingerprint"] = record_fingerprint
    envelope["payload"]["content_fingerprint"] = content_fingerprint
    envelope["payload"]["contract_version"] = "1.0.0"
    envelope["payload"]["schema_fingerprint"] = historical.descriptor.schema_fingerprint
    event_json = canonicalize_json(envelope)
    event_fingerprint = compute_fingerprint(envelope)
    uow._events.records[0] = event.model_copy(
        update={"event_json": event_json, "event_fingerprint": event_fingerprint}
    )
    uow.snapshots.save(
        StoredSnapshot(
            snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R29",
            project_id=stored.project_id,
            aggregate_version=stored.record_version,
            last_stream_position=1,
            contract_name=stored.contract_name,
            contract_version=PROJECT_V1,
            schema_fingerprint=historical.descriptor.schema_fingerprint,
            record_fingerprint=record_fingerprint,
            content_fingerprint=content_fingerprint,
            state_json=state_json,
            created_at=stored.created_at,
        )
    )
    return stored.project_id, event_json


def test_explicit_c01_migration_projects_creation_history_and_replays_v2(
    frozen_clock: FrozenClock,
    create_command: CreateProjectCommand,
    project_ids: ProjectIds,
) -> None:
    uow = FakeUnitOfWork(frozen_clock)
    created = CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        project_ids=project_ids,
        event_ids=EventIds(),
    ).create_project(create_command)
    contracts, migrations, historical = _registries()
    project_id, original_event_json = _make_v1(uow, created.project_id, historical)
    stored = uow.projects.get_stored(created.project_id)
    tail = uow.events.read_stored_stream(created.project_id)[-1]
    assert stored is not None
    replay = ReplayService(unit_of_work_factory=lambda: uow)
    service = StoredContractMigrationService(
        unit_of_work_factory=lambda: uow,
        clock=frozen_clock,
        event_ids=MigrationEventIds(),
        contract_registry=contracts,
        migration_registry=migrations,
        replay_verifier=replay,
    )
    plan = PlanStoredContractMigrationCommand(
        project_id=created.project_id,
        target_project_version=PROJECT_V2,
        target_event_version=EVENT_V1,
        expected_record_version=stored.record_version,
        expected_record_fingerprint=stored.record_fingerprint,
        expected_content_fingerprint=stored.content_fingerprint,
        expected_stream_position=tail.stream_position,
        expected_stream_fingerprint=tail.event_fingerprint,
    )

    dry_run = service.dry_run(plan)
    assert service.dry_run(plan) == dry_run
    assert dry_run.success
    assert dry_run.changed
    assert {item.kind for item in dry_run.items} == {
        StoredContractKind.AGGREGATE,
        StoredContractKind.EVENT,
        StoredContractKind.SNAPSHOT,
    }
    assert dry_run.projected_state is not None
    assert dry_run.projected_state.workflow_registry == {}
    result = service.apply(
        ApplyStoredContractMigrationCommand(
            idempotency_key="project:migrate:c02",
            plan=plan,
            expected_plan_fingerprint=dry_run.plan_fingerprint,
            reason="Apply the approved ProjectState v1 to v2 migration.",
            actor_id="operator:migration",
        )
    )
    repeated = service.apply(
        ApplyStoredContractMigrationCommand(
            idempotency_key="project:migrate:c02",
            plan=plan,
            expected_plan_fingerprint=dry_run.plan_fingerprint,
            reason="Apply the approved ProjectState v1 to v2 migration.",
            actor_id="operator:migration",
        )
    )

    assert repeated == result
    current = uow.projects.get_stored(created.project_id)
    assert current is not None
    assert current.contract_version == PROJECT_V2
    assert uow._events.records[0].event_json == original_event_json
    snapshot = uow.snapshots.get_latest(created.project_id)
    assert snapshot is not None
    assert snapshot.contract_version == PROJECT_V2
    replayed = replay.replay_project(created.project_id)
    assert replayed.reconstructed_state.contract_version == PROJECT_V2
    assert replayed.reconstructed_state.workflow_registry == {}
    assert project_id == replayed.project_id
