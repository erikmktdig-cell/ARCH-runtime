import pytest
from arch_kernel.contracts import ProjectState

from arch_runtime.ports import UnitOfWork

pytestmark = pytest.mark.contract


def test_unit_of_work_keeps_changes_after_explicit_commit(
    project_state: ProjectState,
    unit_of_work: UnitOfWork,
) -> None:
    with unit_of_work:
        unit_of_work.projects.add(project_state)
        unit_of_work.commit()

    assert unit_of_work.projects.get(project_state.metadata.project_id) is not None


def test_unit_of_work_rolls_back_when_commit_is_omitted(
    project_state: ProjectState,
    unit_of_work: UnitOfWork,
) -> None:
    with unit_of_work:
        unit_of_work.projects.add(project_state)

    assert unit_of_work.projects.get(project_state.metadata.project_id) is None


def test_unit_of_work_rolls_back_on_exception(
    project_state: ProjectState,
    unit_of_work: UnitOfWork,
) -> None:
    def mutate_and_fail() -> None:
        with unit_of_work:
            unit_of_work.projects.add(project_state)
            raise RuntimeError("failure")

    with pytest.raises(RuntimeError, match="failure"):
        mutate_and_fail()

    assert unit_of_work.projects.get(project_state.metadata.project_id) is None
