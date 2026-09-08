"""Reject contradictory public result evidence at every outcome boundary."""

from datetime import UTC, datetime
from typing import Any

import pytest
from arch_kernel.contracts import (
    EventId,
    ProjectId,
    Route,
    ValidationIntent,
    ValidationPipelineRequest,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    WorkflowDefinitionRegistry,
    run_validation_pipeline,
)
from pydantic import BaseModel, ValidationError

from arch_runtime.application import (
    ApplyTransitionResult,
    CreateProjectCommand,
    CreateProjectService,
    CreateSnapshotResult,
    InitializeWorkflowCommand,
    InitializeWorkflowService,
    RecoverAggregateResult,
    WorkflowInitializationDisposition,
)
from arch_runtime.replay import ReplayService
from tests.application.conftest import EventIds, ProjectIds
from tests.application.test_workflows import WorkflowEventIds, workflow_definition
from tests.fakes.adapters import FakeUnitOfWork, FrozenClock

FP = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
DISP = WorkflowInitializationDisposition


@pytest.fixture(scope="module")
def results() -> dict[str, BaseModel]:
    clock = FrozenClock(datetime(2026, 9, 7, tzinfo=UTC))
    uow = FakeUnitOfWork(clock)
    created = CreateProjectService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        project_ids=ProjectIds(),
        event_ids=EventIds(),
    ).create_project(
        CreateProjectCommand(
            idempotency_key="results:create",
            name="Evidence",
            slug="evidence",
            owner="test",
            project_type="test",
            criticality="low",
            default_route=Route.QUICK,
            actor_id="test",
        )
    )
    current = uow.projects.get(created.project_id)
    assert current is not None
    definition = workflow_definition()
    initialized = InitializeWorkflowService(
        unit_of_work_factory=lambda: uow,
        clock=clock,
        event_ids=WorkflowEventIds(),
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
        invariant_registry=InvariantRegistry(()),
    ).initialize_workflow(
        InitializeWorkflowCommand(
            idempotency_key="results:init",
            project_id=created.project_id,
            workflow_id=definition.workflow_id,
            workflow_namespace=definition.namespace,
            definition_version=definition.definition_version,
            definition_fingerprint=definition.definition_fingerprint(),
            expected_record_version=current.record_version,
            expected_record_fingerprint=current.record_fingerprint,
            expected_content_fingerprint=current.content_fingerprint,
            actor_id="test",
        )
    )
    assert initialized.validation_result is not None
    transition = ApplyTransitionResult(
        success=True,
        changed=True,
        project_id=created.project_id,
        validation_result=initialized.validation_result,
        before_record_version=1,
        before_record_fingerprint=FP,
        before_content_fingerprint=FP,
        after_record_version=2,
        after_record_fingerprint=OTHER,
        after_content_fingerprint=OTHER,
        event_id=EventId.generate(),
        stream_position=2,
    )
    replay = ReplayService(
        unit_of_work_factory=lambda: uow,
        workflow_definitions=WorkflowDefinitionRegistry((definition,)),
    ).replay_project(created.project_id)
    recovery = RecoverAggregateResult(
        project_id=created.project_id,
        recovered=True,
        replay_result=replay,
        before_record_version=replay.record_version,
        before_record_fingerprint=FP,
        before_content_fingerprint=FP,
        after_record_version=replay.record_version,
        after_record_fingerprint=replay.record_fingerprint,
        after_content_fingerprint=replay.content_fingerprint,
        event_id=EventId.generate(),
        stream_position=3,
    )
    snapshot = CreateSnapshotResult(
        project_id=created.project_id,
        policy_matched=True,
        created=True,
        snapshot_id="SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R30",
        record_version=2,
        record_fingerprint=FP,
        content_fingerprint=FP,
        stream_position=2,
        stream_fingerprint=FP,
        retained_snapshot_ids=("SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R30",),
    )
    return {
        "create": created,
        "initialize": initialized,
        "transition": transition,
        "recovery": recovery,
        "snapshot": snapshot,
    }


@pytest.mark.parametrize(
    ("kind", "changes", "message"),
    [
        ("transition", {"success": False}, "success values must agree"),
        ("transition", {"after_record_version": None}, "after-state evidence"),
        ("transition", {"event_id": None}, "event evidence"),
        ("transition", {"after_record_version": 8}, "increment the aggregate once"),
        ("transition", {"changed": False}, "no-op transition cannot"),
        ("transition", {"changed": False, "event_id": None, "stream_position": None}, "preserve"),
        ("initialize", {"success": False}, "cannot expose workflow"),
        (
            "initialize",
            {"success": False, "changed": False, "workflow_state": None},
            "commit evidence",
        ),
        ("initialize", {"validation_result": None}, "successful pipeline"),
        ("initialize", {"workflow_state": None}, "matching workflow"),
        ("initialize", {"after_record_version": None}, "after-state evidence"),
        ("initialize", {"disposition": DISP.ALREADY_INITIALIZED}, "initialized disposition"),
        ("initialize", {"event_id": None}, "event evidence"),
        ("initialize", {"after_record_version": 99}, "increment the aggregate once"),
        ("initialize", {"changed": False}, "successful no-op"),
        (
            "initialize",
            {"changed": False, "disposition": DISP.ALREADY_INITIALIZED},
            "cannot append",
        ),
        (
            "initialize",
            {
                "changed": False,
                "disposition": DISP.ALREADY_INITIALIZED,
                "event_id": None,
                "stream_position": None,
            },
            "preserve aggregate evidence",
        ),
        ("snapshot", {"snapshot_id": None}, "requires its identity"),
        ("snapshot", {"retained_snapshot_ids": ()}, "must be retained"),
        ("snapshot", {"deleted_snapshot_ids": ("SNP-01HZX7M3FQ1T2Q9V8Y6K4C2R30",)}, "disjoint"),
        ("recovery", {"project_id": ProjectId.generate()}, "belong to the recovered project"),
        ("recovery", {"after_record_version": 99}, "equal verified replay"),
        ("recovery", {"recovered": False}, "reflect materialized"),
        ("recovery", {"event_id": None}, "complete audit event"),
    ],
)
def test_contradictory_result_evidence_is_rejected(
    results: dict[str, BaseModel],
    kind: str,
    changes: dict[str, Any],
    message: str,
) -> None:
    model = results[kind]
    with pytest.raises(ValidationError, match=message):
        type(model).model_validate({**dict(model), **changes})


def test_rejected_results_cannot_claim_successful_validation(results: dict[str, BaseModel]) -> None:
    created = results["create"]
    data = dict(created)
    data.update(
        success=False,
        event_id=None,
        stream_position=None,
        record_version=None,
        record_fingerprint=None,
        content_fingerprint=None,
    )
    with pytest.raises(ValidationError, match="success values must agree"):
        type(created).model_validate(data)
    initialized = results["initialize"]
    data = dict(initialized)
    data.update(
        success=False,
        changed=False,
        workflow_state=None,
        event_id=None,
        stream_position=None,
        after_record_version=None,
        after_record_fingerprint=None,
        after_content_fingerprint=None,
        disposition=DISP.VALIDATION_REJECTED,
    )
    with pytest.raises(ValidationError, match="failed pipeline evidence"):
        type(initialized).model_validate(data)


def test_noop_recovery_rejects_audit_event(results: dict[str, BaseModel]) -> None:
    model = results["recovery"]
    data = dict(model)
    data.update(recovered=False, before_record_fingerprint=data["after_record_fingerprint"])
    with pytest.raises(ValidationError, match="no-op recovery"):
        type(model).model_validate(data)


def test_successful_creation_requires_all_commit_evidence(results: dict[str, BaseModel]) -> None:
    model = results["create"]
    with pytest.raises(ValidationError, match="complete commit evidence"):
        type(model).model_validate({**dict(model), "record_fingerprint": None})


def test_failed_transition_cannot_contain_after_evidence(results: dict[str, BaseModel]) -> None:
    validation = run_validation_pipeline(
        ValidationPipelineRequest.model_validate(
            {
                "intent": ValidationIntent.VALIDATE_STATE,
                "evaluated_at": datetime(2026, 9, 7, tzinfo=UTC),
                "raw_state": {"contract_name": "project_state"},
            }
        )
    )
    assert not validation.success
    model = results["transition"]
    with pytest.raises(ValidationError, match="cannot contain commit evidence"):
        type(model).model_validate(
            {**dict(model), "success": False, "validation_result": validation}
        )
