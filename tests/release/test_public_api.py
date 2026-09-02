import pytest

import arch_runtime

pytestmark = pytest.mark.release


EXPECTED_PUBLIC_API = {
    "ApplyStoredContractMigrationCommand",
    "ApplyStoredContractMigrationResult",
    "ApplyTransitionCommand",
    "ApplyTransitionResult",
    "ArchRuntimeError",
    "ConcurrentModificationError",
    "CorruptStoredRecordError",
    "CreateProjectCommand",
    "CreateProjectResult",
    "CreateSnapshotCommand",
    "CreateSnapshotResult",
    "GetProjectResult",
    "IdempotencyConflictError",
    "InitializeWorkflowCommand",
    "InitializeWorkflowResult",
    "PersistenceError",
    "PlanStoredContractMigrationCommand",
    "ProjectAlreadyExistsError",
    "ProjectNotFoundError",
    "RecoverAggregateCommand",
    "RecoverAggregateResult",
    "ReplayIntegrityError",
    "ReplayResult",
    "Runtime",
    "RuntimeConfig",
    "RuntimeConfigurationError",
    "RuntimeSchemaMigrationRequiredError",
    "StoredContractMigrationDryRunResult",
    "StoredMigrationRequiredError",
    "UnsupportedRuntimeSchemaError",
    "UnsupportedStoredContractError",
    "WorkflowInitializationDisposition",
    "__version__",
}


def test_public_api_is_frozen_at_c02_surface() -> None:
    assert set(arch_runtime.__all__) == EXPECTED_PUBLIC_API
    assert not any("sqlite" in name.lower() for name in arch_runtime.__all__)
