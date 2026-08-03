import pytest

from tests.contract.test_event_store import (
    test_event_chain_tail_is_scoped_to_the_project,
    test_event_store_rejects_stale_project_tail,
)
from tests.contract.test_project_repository import (
    test_project_repository_adds_and_loads_canonical_evidence,
    test_project_repository_rejects_duplicate_add,
    test_project_repository_requires_version_and_fingerprint_cas,
)
from tests.contract.test_snapshot_and_idempotency import (
    test_idempotency_store_persists_canonical_logical_result,
    test_snapshot_store_returns_highest_aggregate_version,
)
from tests.contract.test_unit_of_work import (
    test_unit_of_work_keeps_changes_after_explicit_commit,
    test_unit_of_work_rolls_back_on_exception,
    test_unit_of_work_rolls_back_when_commit_is_omitted,
)

pytestmark = [pytest.mark.contract, pytest.mark.sqlite]

__all__ = (
    "test_event_chain_tail_is_scoped_to_the_project",
    "test_event_store_rejects_stale_project_tail",
    "test_idempotency_store_persists_canonical_logical_result",
    "test_project_repository_adds_and_loads_canonical_evidence",
    "test_project_repository_rejects_duplicate_add",
    "test_project_repository_requires_version_and_fingerprint_cas",
    "test_snapshot_store_returns_highest_aggregate_version",
    "test_unit_of_work_keeps_changes_after_explicit_commit",
    "test_unit_of_work_rolls_back_on_exception",
    "test_unit_of_work_rolls_back_when_commit_is_omitted",
)
