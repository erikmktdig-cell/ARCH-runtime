from __future__ import annotations

import inspect
from pathlib import Path
from typing import Protocol, cast

import pytest
from arch_kernel.contracts import (
    ChangeId,
    EventId,
    FrozenJsonObject,
    PhaseStatus,
    ProjectId,
    Route,
    SemanticVersion,
    TransitionDomain,
    TransitionTarget,
    WorkflowDefinition,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    TransitionRegistry,
    WorkflowDefinitionRegistry,
    canonicalize_json,
    compute_content_fingerprint,
)

from arch_runtime import (
    ApplyStoredContractMigrationCommand,
    ApplyTransitionCommand,
    CreateProjectCommand,
    CreateSnapshotCommand,
    GetProjectResult,
    InitializeWorkflowCommand,
    PlanStoredContractMigrationCommand,
    ProjectNotFoundError,
    RecoverAggregateCommand,
    Runtime,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeSchemaMigrationRequiredError,
    UnsupportedRuntimeSchemaError,
)
from arch_runtime.application import SnapshotPolicy
from arch_runtime.persistence.sqlite import (
    SQLiteConfig,
    migrate_schema,
    open_sqlite_connection,
)
from tests.application.conftest import ProjectIds
from tests.application.test_apply_transition import _definition
from tests.application.test_snapshots import SnapshotIds
from tests.application.test_workflows import workflow_definition, workflow_transition
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite
PROJECT_CURRENT = SemanticVersion.parse("2.0.0")
EVENT_CURRENT = SemanticVersion.parse("1.0.0")


class ProjectEvidence(Protocol):
    record_version: int
    record_fingerprint: str
    content_fingerprint: str


class EventIds:
    def __init__(self) -> None:
        self._values = iter(
            (
                EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R10"),
                EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R11"),
                EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R12"),
                EventId("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2R13"),
            )
        )

    def new(self) -> EventId:
        return next(self._values)


def _config(path: Path, clock: FrozenClock, *, initialize: bool) -> RuntimeConfig:
    workflow = workflow_definition()
    return RuntimeConfig(
        path,
        initialize_schema=initialize,
        busy_timeout_ms=2_500,
        clock=clock,
        project_ids=ProjectIds(),
        event_ids=EventIds(),
        snapshot_ids=SnapshotIds(),
        transition_registry=TransitionRegistry(
            (_definition(), workflow_transition(workflow)),
            workflow_definitions=(workflow,),
        ),
        workflow_definition_registry=WorkflowDefinitionRegistry((workflow,)),
        invariant_registry=InvariantRegistry(()),
        snapshot_policy=SnapshotPolicy(every_n_versions=1),
    )


def _create_command() -> CreateProjectCommand:
    return CreateProjectCommand.model_validate(
        {
            "idempotency_key": "runtime:create:001",
            "name": "Runtime Public API",
            "slug": "runtime-public-api",
            "summary": "Exercise the complete public facade.",
            "owner": "platform-team",
            "project_type": "platform",
            "criticality": "high",
            "default_route": Route.STANDARD,
            "objectives": ("Prove the public runtime facade.",),
            "constraints": ("Keep infrastructure private.",),
            "success_criteria": ("Replay verified evidence.",),
            "actor_id": "user:runtime",
        },
        strict=True,
    )


def _transition(project_id: ProjectId, current: ProjectEvidence) -> ApplyTransitionCommand:
    return ApplyTransitionCommand.model_validate(
        {
            "idempotency_key": "runtime:transition:001",
            "project_id": project_id,
            "request_id": ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R10"),
            "target": TransitionTarget(
                domain=TransitionDomain.PROJECT_LIFECYCLE,
                entity_id=project_id,
            ),
            "transition_key": "project.cancel",
            "expected_from_state": PhaseStatus.NOT_STARTED.value,
            "expected_record_version": current.record_version,
            "expected_record_fingerprint": current.record_fingerprint,
            "expected_content_fingerprint": current.content_fingerprint,
            "metadata": {"source": "runtime-e2e"},
            "actor_id": "user:runtime",
        },
        strict=True,
    )


def test_public_runtime_executes_all_approved_use_cases_against_real_sqlite(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> None:
    database = tmp_path / "runtime.db"
    runtime = Runtime.open(_config(database, frozen_clock, initialize=True))
    assert runtime.config.database == database
    with runtime:
        created = runtime.create_project(_create_command())
        loaded = runtime.get_project(created.project_id)
        assert loaded.record_fingerprint == created.record_fingerprint
        loaded_data = loaded.model_dump(mode="json")
        contradictions = (
            ("project_id", str(ProjectId.generate()), "identity"),
            ("record_version", loaded.record_version + 1, "version"),
            ("record_fingerprint", "sha256:" + "a" * 64, "fingerprint"),
            ("content_fingerprint", "sha256:" + "b" * 64, "content fingerprint"),
        )
        for field, value, message in contradictions:
            with pytest.raises(ValueError, match=message):
                GetProjectResult.model_validate_json(
                    canonicalize_json(loaded_data | {field: value}), strict=True
                )

        definition: WorkflowDefinition = workflow_definition()
        initialized = runtime.initialize_workflow(
            InitializeWorkflowCommand(
                idempotency_key="runtime:workflow:init:001",
                project_id=created.project_id,
                workflow_id=definition.workflow_id,
                workflow_namespace=definition.namespace,
                definition_version=definition.definition_version,
                definition_fingerprint=definition.definition_fingerprint(),
                expected_record_version=loaded.record_version,
                expected_record_fingerprint=loaded.record_fingerprint,
                expected_content_fingerprint=loaded.content_fingerprint,
                actor_id="user:runtime",
            )
        )
        assert initialized.success
        assert initialized.changed
        assert (
            runtime.initialize_workflow(
                InitializeWorkflowCommand(
                    idempotency_key="runtime:workflow:init:001",
                    project_id=created.project_id,
                    workflow_id=definition.workflow_id,
                    workflow_namespace=definition.namespace,
                    definition_version=definition.definition_version,
                    definition_fingerprint=definition.definition_fingerprint(),
                    expected_record_version=loaded.record_version,
                    expected_record_fingerprint=loaded.record_fingerprint,
                    expected_content_fingerprint=loaded.content_fingerprint,
                    actor_id="user:runtime",
                )
            )
            == initialized
        )
        workflow_current = runtime.get_project(created.project_id)
        workflow_record = workflow_current.state.workflow_registry[definition.workflow_id]
        workflow_changed = runtime.apply_transition(
            ApplyTransitionCommand(
                idempotency_key="runtime:workflow:transition:001",
                project_id=created.project_id,
                request_id=ChangeId("CHG-01HZX7M3FQ1T2Q9V8Y6K4C2R11"),
                target=TransitionTarget(
                    domain=TransitionDomain.WORKFLOW_STATE,
                    entity_id=definition.workflow_id,
                ),
                transition_key="workflow.start_delivery",
                expected_from_state="queued",
                expected_record_version=workflow_current.record_version,
                expected_record_fingerprint=workflow_current.record_fingerprint,
                expected_content_fingerprint=workflow_current.content_fingerprint,
                expected_target_record_version=workflow_record.record_version,
                expected_target_content_fingerprint=compute_content_fingerprint(workflow_record),
                metadata=cast(FrozenJsonObject, {"source": "runtime-e2e"}),
                actor_id="user:runtime",
            )
        )
        assert workflow_changed.success
        assert workflow_changed.changed
        loaded = runtime.get_project(created.project_id)

        transitioned = runtime.apply_transition(_transition(created.project_id, loaded))
        assert transitioned.success
        assert transitioned.changed
        current = runtime.get_project(created.project_id)
        assert current.record_version == loaded.record_version + 1

        snapshot = runtime.create_snapshot(
            CreateSnapshotCommand(
                project_id=created.project_id,
                expected_record_version=current.record_version,
                expected_record_fingerprint=current.record_fingerprint,
                expected_content_fingerprint=current.content_fingerprint,
                expected_stream_position=current.stream_position,
                expected_stream_fingerprint=current.stream_fingerprint,
                force=True,
            )
        )
        assert snapshot.created
        replay = runtime.replay_project(created.project_id)
        assert replay.snapshot_id == snapshot.snapshot_id
        assert replay.last_event_fingerprint == current.stream_fingerprint

        recovered = runtime.recover_project(
            RecoverAggregateCommand(
                idempotency_key="runtime:recover:001",
                project_id=created.project_id,
                expected_record_version=current.record_version,
                expected_record_fingerprint=current.record_fingerprint,
                expected_content_fingerprint=current.content_fingerprint,
                reason="Verify the public no-op recovery path.",
                actor_id="operator:runtime",
            )
        )
        assert not recovered.recovered

        plan = PlanStoredContractMigrationCommand(
            project_id=created.project_id,
            target_project_version=PROJECT_CURRENT,
            target_event_version=EVENT_CURRENT,
            expected_record_version=current.record_version,
            expected_record_fingerprint=current.record_fingerprint,
            expected_content_fingerprint=current.content_fingerprint,
            expected_stream_position=current.stream_position,
            expected_stream_fingerprint=current.stream_fingerprint,
        )
        dry_run = runtime.plan_project_contract_migration(plan)
        assert dry_run.success
        assert not dry_run.changed
        migrated = runtime.migrate_project_contracts(
            ApplyStoredContractMigrationCommand(
                idempotency_key="runtime:migrate:001",
                plan=plan,
                expected_plan_fingerprint=dry_run.plan_fingerprint,
                reason="Verify the public no-op migration path.",
                actor_id="operator:runtime",
            )
        )
        assert migrated.success
        assert not migrated.changed

        with Runtime.open(_config(database, frozen_clock, initialize=False)) as second:
            assert second.get_project(created.project_id) == runtime.get_project(created.project_id)

    assert runtime.closed
    runtime.close()
    with pytest.raises(RuntimeConfigurationError, match="closed"):
        runtime.get_project(created.project_id)


def test_runtime_requires_explicit_initialization_for_empty_database(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "empty.db"
    with pytest.raises(RuntimeSchemaMigrationRequiredError):
        Runtime.open(_config(database, frozen_clock, initialize=False))
    assert database.exists()
    with Runtime.open(_config(database, frozen_clock, initialize=True)) as runtime:
        assert not runtime.closed


def test_runtime_never_upgrades_an_outdated_schema_implicitly(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "outdated.db"
    connection = open_sqlite_connection(SQLiteConfig(database))
    try:
        migrate_schema(connection, clock=frozen_clock, target_version=1)
    finally:
        connection.close()

    with pytest.raises(RuntimeSchemaMigrationRequiredError):
        Runtime.open(_config(database, frozen_clock, initialize=True))


def test_runtime_rejects_newer_and_altered_schemas(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    newer = tmp_path / "newer.db"
    with Runtime.open(_config(newer, frozen_clock, initialize=True)):
        pass
    connection = open_sqlite_connection(SQLiteConfig(newer))
    try:
        connection.execute(
            "INSERT INTO runtime_schema_migrations "
            "(version, name, checksum, applied_at) VALUES (3, 'future', ?, ?)",
            ("sha256:" + "f" * 64, frozen_clock.now().isoformat()),
        )
    finally:
        connection.close()
    with pytest.raises(UnsupportedRuntimeSchemaError):
        Runtime.open(_config(newer, frozen_clock, initialize=False))

    altered = tmp_path / "altered.db"
    with Runtime.open(_config(altered, frozen_clock, initialize=True)):
        pass
    connection = open_sqlite_connection(SQLiteConfig(altered))
    try:
        connection.execute("DROP INDEX ix_project_events_project_position")
    finally:
        connection.close()
    with pytest.raises(RuntimeConfigurationError, match="integrity"):
        Runtime.open(_config(altered, frozen_clock, initialize=False))


def test_runtime_configuration_and_public_signatures_hide_sqlite_types(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError, match="filesystem"):
        RuntimeConfig(":memory:")
    with pytest.raises(RuntimeConfigurationError, match="directory"):
        Runtime.open(RuntimeConfig(tmp_path))

    public_methods = (
        Runtime.create_project,
        Runtime.get_project,
        Runtime.apply_transition,
        Runtime.initialize_workflow,
        Runtime.replay_project,
        Runtime.create_snapshot,
        Runtime.recover_project,
        Runtime.plan_project_contract_migration,
        Runtime.migrate_project_contracts,
    )
    assert all("sqlite" not in str(inspect.signature(method)).lower() for method in public_methods)


def test_runtime_defaults_generate_canonical_ids_and_support_path_open(tmp_path: Path) -> None:
    database = tmp_path / "defaults.db"
    runtime = Runtime.open(RuntimeConfig(database, initialize_schema=True))
    created = runtime.create_project(_create_command())
    assert str(created.project_id).startswith("PRJ-")
    assert created.event_id is not None
    assert str(created.event_id).startswith("EVT-")
    loaded = runtime.get_project(created.project_id)
    snapshot = runtime.create_snapshot(
        CreateSnapshotCommand(
            project_id=created.project_id,
            expected_record_version=loaded.record_version,
            expected_record_fingerprint=loaded.record_fingerprint,
            expected_content_fingerprint=loaded.content_fingerprint,
            expected_stream_position=loaded.stream_position,
            expected_stream_fingerprint=loaded.stream_fingerprint,
            force=True,
        )
    )
    assert snapshot.snapshot_id is not None
    assert snapshot.snapshot_id.startswith("SNP-")
    runtime.close()

    with Runtime.open(database) as reopened:
        assert reopened.get_project(created.project_id).project_id == created.project_id
        with pytest.raises(ProjectNotFoundError):
            reopened.get_project(ProjectId.generate())
    with pytest.raises(RuntimeConfigurationError, match="closed"):
        reopened.__enter__()
