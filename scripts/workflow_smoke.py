"""Public API vertical slice executed only from installed distributions."""

from __future__ import annotations

import importlib.metadata
import json
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import arch_kernel
from arch_kernel.contracts import (
    ChangeId,
    EffectType,
    EffectValueSource,
    FrozenJsonObject,
    Route,
    SemanticVersion,
    TransitionDomain,
    TransitionEffect,
    TransitionId,
    TransitionTarget,
    WorkflowDefinition,
    WorkflowId,
    WorkflowNamespace,
    WorkflowStateKey,
    WorkflowTransitionDefinition,
)
from arch_kernel.kernel import (
    InvariantRegistry,
    TransitionRegistry,
    WorkflowDefinitionRegistry,
    compute_content_fingerprint,
)

import arch_runtime
from arch_runtime import (
    ApplyTransitionCommand,
    ConcurrentModificationError,
    CreateProjectCommand,
    CreateSnapshotCommand,
    GetProjectResult,
    IdempotencyConflictError,
    InitializeWorkflowCommand,
    RecoverAggregateCommand,
    Runtime,
    RuntimeConfig,
)

VERSION = SemanticVersion.parse("1.0.0")


def definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        contract_name="workflow_definition",
        contract_version=VERSION,
        schema_uri="urn:arch:contracts:workflow_definition:1.0.0",
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        workflow_id=WorkflowId("WFL-01HZX7M3FQ1T2Q9V8Y6K4C2B1A"),
        namespace=WorkflowNamespace.parse("example.release"),
        definition_version=VERSION,
        allowed_states=tuple(WorkflowStateKey.parse(s) for s in ("a", "b", "c")),
        initial_state=WorkflowStateKey.parse("a"),
    )


def transitions(workflow: WorkflowDefinition) -> tuple[WorkflowTransitionDefinition, ...]:
    return tuple(
        WorkflowTransitionDefinition(
            transition_id=TransitionId(f"TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R{20 + i}"),
            name=f"Advance to {after}",
            transition_key=f"workflow.advance_{after}",
            contract_version=VERSION,
            domain=TransitionDomain.WORKFLOW_STATE,
            from_states=(before,),
            to_state=after,
            effects=(
                TransitionEffect(
                    effect_type=EffectType.SET_FIELD,
                    path_template="/workflows/{entity_id}/current_state",
                    value_source=EffectValueSource.TO_STATE,
                ),
            ),
            workflow_namespace=workflow.namespace,
            workflow_definition_version=workflow.definition_version,
            workflow_definition_fingerprint=workflow.definition_fingerprint(),
        )
        for i, (before, after) in enumerate((("a", "b"), ("b", "c")))
    )


def transition_command(
    current: GetProjectResult,
    workflow: WorkflowDefinition,
    before: str,
    after: str,
) -> ApplyTransitionCommand:
    target = current.state.workflow_registry[workflow.workflow_id]
    return ApplyTransitionCommand(
        idempotency_key=f"release:advance:{after}",
        project_id=current.project_id,
        request_id=ChangeId.generate(),
        target=TransitionTarget(
            domain=TransitionDomain.WORKFLOW_STATE, entity_id=workflow.workflow_id
        ),
        transition_key=f"workflow.advance_{after}",
        expected_from_state=before,
        expected_record_version=current.record_version,
        expected_record_fingerprint=current.record_fingerprint,
        expected_content_fingerprint=current.content_fingerprint,
        expected_target_record_version=target.record_version,
        expected_target_content_fingerprint=compute_content_fingerprint(target),
        metadata=cast(FrozenJsonObject, {}),
        actor_id="release-check",
    )


def workflow_slice(database: Path) -> dict[str, object]:
    workflow = definition()
    config = RuntimeConfig(
        database,
        initialize_schema=True,
        workflow_definition_registry=WorkflowDefinitionRegistry((workflow,)),
        transition_registry=TransitionRegistry(
            transitions(workflow), workflow_definitions=(workflow,)
        ),
        invariant_registry=InvariantRegistry(()),
    )
    with Runtime.open(config) as runtime:
        created = runtime.create_project(
            CreateProjectCommand(
                idempotency_key="release:create",
                name="Installed compatibility",
                slug="installed-compatibility",
                owner="release-team",
                project_type="platform",
                criticality="low",
                default_route=Route.QUICK,
                actor_id="release-check",
            )
        )
        current = runtime.get_project(created.project_id)
        initialized = runtime.initialize_workflow(
            InitializeWorkflowCommand(
                idempotency_key="release:initialize",
                project_id=created.project_id,
                workflow_id=workflow.workflow_id,
                workflow_namespace=workflow.namespace,
                definition_version=workflow.definition_version,
                definition_fingerprint=workflow.definition_fingerprint(),
                expected_record_version=current.record_version,
                expected_record_fingerprint=current.record_fingerprint,
                expected_content_fingerprint=current.content_fingerprint,
                actor_id="release-check",
            )
        )
        assert initialized.success
        assert initialized.workflow_state is not None
        assert str(initialized.workflow_state.current_state) == "a"
        current = runtime.get_project(created.project_id)
        first_command = transition_command(current, workflow, "a", "b")
        for field, value in (
            ("expected_record_version", 99),
            ("expected_record_fingerprint", "sha256:" + "f" * 64),
        ):
            try:
                runtime.apply_transition(first_command.model_copy(update={field: value}))
            except ConcurrentModificationError:
                pass
            else:
                raise AssertionError(f"stale {field} was accepted")
        for field, value in (
            ("expected_target_record_version", 99),
            ("expected_target_content_fingerprint", "sha256:" + "e" * 64),
        ):
            rejected = runtime.apply_transition(
                first_command.model_copy(
                    update={
                        field: value,
                        "idempotency_key": f"release:stale:{field}",
                    }
                )
            )
            assert not rejected.success
            assert not rejected.changed
            assert runtime.get_project(created.project_id) == current
        first = runtime.apply_transition(first_command)
        assert first.success
        assert first.changed
        current = runtime.get_project(created.project_id)
        assert str(current.state.workflow_registry[workflow.workflow_id].current_state) == "b"
        full = runtime.replay_project(created.project_id)
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
        assert runtime.replay_project(created.project_id).snapshot_disposition == "used"
        assert (
            runtime.replay_project(created.project_id).reconstructed_state
            == full.reconstructed_state
        )
        second_command = transition_command(current, workflow, "b", "c")
        second = runtime.apply_transition(second_command)
        assert second.success
        assert second.changed
        final = runtime.get_project(created.project_id)
        assert runtime.apply_transition(second_command) == second
        assert runtime.get_project(created.project_id) == final
        try:
            runtime.apply_transition(second_command.model_copy(update={"actor_id": "other"}))
        except IdempotencyConflictError:
            pass
        else:
            raise AssertionError("conflicting retry was accepted")
        assert runtime.replay_project(created.project_id).reconstructed_state == final.state
        assert runtime.replay_project(created.project_id).snapshot_disposition == "used"
        assert str(final.state.workflow_registry[workflow.workflow_id].current_state) == "c"
        assert final.record_version == 4
        assert final.stream_position == 4
        noop = runtime.recover_project(
            RecoverAggregateCommand(
                idempotency_key="release:recovery:noop",
                project_id=created.project_id,
                expected_record_version=final.record_version,
                expected_record_fingerprint=final.record_fingerprint,
                expected_content_fingerprint=final.content_fingerprint,
                reason="Verify exact workflow authority.",
                actor_id="release-check",
            )
        )
        assert not noop.recovered
    with Runtime.open(config) as reopened:
        assert reopened.get_project(created.project_id) == final
    return {
        "workflow": "a -> b -> c",
        "record_version": final.record_version,
        "stream_position": final.stream_position,
        "retry": "PASS",
        "cas": "PASS",
        "snapshot_tail_replay": "PASS",
        "recovery_noop": "PASS",
    }


def main() -> None:
    origins: dict[str, str] = {}
    for package, module in (("arch-kernel", arch_kernel), ("arch-runtime", arch_runtime)):
        assert importlib.metadata.version(package) == "0.2.0"
        assert module.__file__ is not None
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(Path(sys.prefix).resolve()), origin
        assert "site-packages" in origin.parts, origin
        origins[package] = str(origin)
    result = workflow_slice(Path("workflow.db"))
    fixture = runpy.run_path("historical_fixture.py")
    result["migration"] = fixture["migration_slice"](Path("historical.db"))
    result["versions"] = {name: importlib.metadata.version(name) for name in origins}
    result["origins"] = origins
    result["python"] = sys.version
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
