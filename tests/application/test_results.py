import pytest
from arch_kernel.contracts import ValidationIntent, ValidationPipelineRequest
from arch_kernel.kernel import run_validation_pipeline

from arch_runtime.application import CreateProjectResult
from tests.application.conftest import EventIds, ProjectIds
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.unit


def test_result_rejects_partial_or_contradictory_commit_evidence(
    frozen_clock: FrozenClock,
    project_ids: ProjectIds,
    event_ids: EventIds,
) -> None:
    validation = run_validation_pipeline(
        ValidationPipelineRequest.model_validate(
            {
                "intent": ValidationIntent.VALIDATE_STATE,
                "evaluated_at": frozen_clock.now(),
                "raw_state": {"contract_name": "project_state"},
            }
        )
    )
    project_id = project_ids.new()
    with pytest.raises(ValueError, match="cannot contain"):
        CreateProjectResult(
            success=False,
            project_id=project_id,
            validation_result=validation,
            event_id=event_ids.new(),
        )
    with pytest.raises(ValueError, match="must agree"):
        CreateProjectResult(
            success=True,
            project_id=project_id,
            validation_result=validation,
            event_id=event_ids.new(),
            stream_position=1,
            record_version=1,
            record_fingerprint="sha256:" + "a" * 64,
            content_fingerprint="sha256:" + "b" * 64,
        )
