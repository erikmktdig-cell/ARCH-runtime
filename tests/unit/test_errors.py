import pytest

from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    IdempotencyConflictError,
    PersistenceError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ReplayIntegrityError,
    RuntimeConfigurationError,
    RuntimeErrorCode,
    RuntimeSchemaMigrationRequiredError,
    StoredMigrationRequiredError,
    UnsupportedRuntimeSchemaError,
    UnsupportedStoredContractError,
)

pytestmark = pytest.mark.unit


def test_error_taxonomy_has_one_stable_code_per_failure_class() -> None:
    error_types = (
        RuntimeConfigurationError,
        RuntimeSchemaMigrationRequiredError,
        UnsupportedRuntimeSchemaError,
        ProjectAlreadyExistsError,
        ProjectNotFoundError,
        ConcurrentModificationError,
        IdempotencyConflictError,
        StoredMigrationRequiredError,
        UnsupportedStoredContractError,
        CorruptStoredRecordError,
        ReplayIntegrityError,
        PersistenceError,
    )
    codes = {error_type.code for error_type in error_types}
    assert codes == set(RuntimeErrorCode)
