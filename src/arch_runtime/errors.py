"""Structured, payload-safe runtime error taxonomy."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from arch_kernel.contracts import ProjectId


class RuntimeErrorCode(StrEnum):
    """Stable machine-readable runtime error codes."""

    CONFIGURATION_INVALID = "runtime.configuration_invalid"
    SCHEMA_MIGRATION_REQUIRED = "runtime.schema_migration_required"
    SCHEMA_UNSUPPORTED = "runtime.schema_unsupported"
    PROJECT_ALREADY_EXISTS = "runtime.project_already_exists"
    PROJECT_NOT_FOUND = "runtime.project_not_found"
    CONCURRENT_MODIFICATION = "runtime.concurrent_modification"
    IDEMPOTENCY_CONFLICT = "runtime.idempotency_conflict"
    STORED_MIGRATION_REQUIRED = "runtime.stored_migration_required"
    STORED_CONTRACT_UNSUPPORTED = "runtime.stored_contract_unsupported"
    STORED_RECORD_CORRUPT = "runtime.stored_record_corrupt"
    REPLAY_INTEGRITY_FAILED = "runtime.replay_integrity_failed"
    PERSISTENCE_FAILED = "runtime.persistence_failed"


@dataclass(frozen=True, slots=True)
class RuntimeErrorDetails:
    """Safe structured details suitable for logs and API translation."""

    code: RuntimeErrorCode
    message: str
    operation: str
    remediation: str
    project_id: ProjectId | None = None


class ArchRuntimeError(Exception):
    """Base class for failures owned by the runtime boundary."""

    code: ClassVar[RuntimeErrorCode]

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        remediation: str,
        project_id: ProjectId | None = None,
    ) -> None:
        self.details = RuntimeErrorDetails(
            code=self.code,
            message=message,
            operation=operation,
            remediation=remediation,
            project_id=project_id,
        )
        super().__init__(message)


class RuntimeConfigurationError(ArchRuntimeError):
    code = RuntimeErrorCode.CONFIGURATION_INVALID


class RuntimeSchemaMigrationRequiredError(ArchRuntimeError):
    code = RuntimeErrorCode.SCHEMA_MIGRATION_REQUIRED


class UnsupportedRuntimeSchemaError(ArchRuntimeError):
    code = RuntimeErrorCode.SCHEMA_UNSUPPORTED


class ProjectAlreadyExistsError(ArchRuntimeError):
    code = RuntimeErrorCode.PROJECT_ALREADY_EXISTS


class ProjectNotFoundError(ArchRuntimeError):
    code = RuntimeErrorCode.PROJECT_NOT_FOUND


class ConcurrentModificationError(ArchRuntimeError):
    code = RuntimeErrorCode.CONCURRENT_MODIFICATION


class IdempotencyConflictError(ArchRuntimeError):
    code = RuntimeErrorCode.IDEMPOTENCY_CONFLICT


class StoredMigrationRequiredError(ArchRuntimeError):
    code = RuntimeErrorCode.STORED_MIGRATION_REQUIRED


class UnsupportedStoredContractError(ArchRuntimeError):
    code = RuntimeErrorCode.STORED_CONTRACT_UNSUPPORTED


class CorruptStoredRecordError(ArchRuntimeError):
    code = RuntimeErrorCode.STORED_RECORD_CORRUPT


class ReplayIntegrityError(ArchRuntimeError):
    code = RuntimeErrorCode.REPLAY_INTEGRITY_FAILED


class PersistenceError(ArchRuntimeError):
    code = RuntimeErrorCode.PERSISTENCE_FAILED
