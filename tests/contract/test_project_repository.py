import pytest
from arch_kernel.contracts import ProjectState

from arch_runtime.errors import ConcurrentModificationError, ProjectAlreadyExistsError
from arch_runtime.ports import ProjectRepository

pytestmark = pytest.mark.contract


def test_project_repository_adds_and_loads_canonical_evidence(
    project_state: ProjectState,
    project_repository: ProjectRepository,
) -> None:
    project_repository.add(project_state)

    stored = project_repository.get(project_state.metadata.project_id)

    assert stored is not None
    assert stored.project_id == project_state.metadata.project_id
    assert stored.record_version == project_state.record_version
    assert stored.state_json.startswith(b"{")


def test_project_repository_rejects_duplicate_add(
    project_state: ProjectState,
    project_repository: ProjectRepository,
) -> None:
    project_repository.add(project_state)

    with pytest.raises(ProjectAlreadyExistsError):
        project_repository.add(project_state)


def test_project_repository_requires_version_and_fingerprint_cas(
    project_state: ProjectState,
    project_repository: ProjectRepository,
) -> None:
    project_repository.add(project_state)
    stored = project_repository.get(project_state.metadata.project_id)
    assert stored is not None
    candidate = project_state.model_copy(update={"record_version": 2})

    with pytest.raises(ConcurrentModificationError):
        project_repository.save(
            candidate,
            expected_record_version=stored.record_version,
            expected_record_fingerprint="sha256:" + "0" * 64,
        )

    project_repository.save(
        candidate,
        expected_record_version=stored.record_version,
        expected_record_fingerprint=stored.record_fingerprint,
    )
    updated = project_repository.get(project_state.metadata.project_id)
    assert updated is not None
    assert updated.record_version == 2
