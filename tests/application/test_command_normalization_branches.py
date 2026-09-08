from collections.abc import Callable
from typing import Any, cast

import pytest
from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import compute_content_fingerprint, compute_fingerprint
from pydantic import ValidationError
from scripts.workflow_smoke import definition

from arch_runtime import GetProjectResult
from arch_runtime.application import (
    ApplyStoredContractMigrationCommand,
    ApplyTransitionCommand,
    CreateProjectCommand,
    InitializeWorkflowCommand,
    RecoverAggregateCommand,
)


@pytest.mark.parametrize(
    "command",
    [
        CreateProjectCommand,
        ApplyTransitionCommand,
        InitializeWorkflowCommand,
        RecoverAggregateCommand,
        ApplyStoredContractMigrationCommand,
    ],
)
def test_non_mapping_input_is_left_for_pydantic_to_reject(command: Any) -> None:
    value = ("not", "a", "mapping")
    assert command.normalize_text(value) is value


def test_create_normalization_preserves_invalid_types_for_validation() -> None:
    value = {
        "name": 1,
        "owner": None,
        "slug": 3,
        "actor_id": False,
        "objectives": [" trim ", 42],
        "summary": None,
    }
    result = cast(Callable[[object], object], CreateProjectCommand.normalize_text)(value)
    assert result == {**value, "objectives": ("trim", 42)}
    assert value["objectives"] == [" trim ", 42]


def test_transition_normalization_handles_optional_and_nonstring_metadata() -> None:
    value = {
        "actor_display_name": " Tester ",
        "actor_id": 42,
        "metadata": {" key ": " value ", "number": 3, "nested": {"a": 1}},
    }
    normalize = cast(Callable[[object], object], ApplyTransitionCommand.normalize_text)
    result = normalize(value)
    assert result == {
        "actor_display_name": "Tester",
        "actor_id": 42,
        "metadata": {"key": "value", "number": 3, "nested": {"a": 1}},
    }
    assert normalize({"metadata": None}) == {"metadata": None}


def test_transition_requires_exactly_one_selector(project_state: ProjectState) -> None:
    current = GetProjectResult(
        project_id=project_state.metadata.project_id,
        state=project_state,
        record_version=project_state.record_version,
        record_fingerprint=compute_fingerprint(project_state),
        content_fingerprint=compute_content_fingerprint(project_state),
        stream_position=1,
        stream_fingerprint="sha256:" + "a" * 64,
    )
    # Build the required fields directly; no configured workflow is needed for selector validation.
    from arch_kernel.contracts import ChangeId, TransitionDomain, TransitionTarget

    fields = {
        "idempotency_key": "selector",
        "project_id": current.project_id,
        "request_id": ChangeId.generate(),
        "target": TransitionTarget(
            domain=TransitionDomain.WORKFLOW_STATE, entity_id=definition().workflow_id
        ),
        "expected_record_version": current.record_version,
        "expected_record_fingerprint": current.record_fingerprint,
        "expected_content_fingerprint": current.content_fingerprint,
        "metadata": {},
        "actor_id": "test",
    }
    with pytest.raises(ValidationError, match="exactly one transition selector"):
        ApplyTransitionCommand.model_validate(fields)
