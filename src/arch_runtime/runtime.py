"""Stable synchronous public facade for ARch Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from arch_kernel.contracts import EventId, ProjectId
from arch_kernel.kernel import (
    ContractRegistry,
    InvariantRegistry,
    MigrationRegistry,
    TransitionRegistry,
    build_builtin_contract_registry,
)

from arch_runtime.application import (
    ApplyStoredContractMigrationCommand,
    ApplyStoredContractMigrationResult,
    ApplyTransitionCommand,
    ApplyTransitionResult,
    ApplyTransitionService,
    CreateProjectCommand,
    CreateProjectResult,
    CreateProjectService,
    CreateSnapshotCommand,
    CreateSnapshotResult,
    PlanStoredContractMigrationCommand,
    RecoverAggregateCommand,
    RecoverAggregateResult,
    RecoveryService,
    SnapshotPolicy,
    SnapshotService,
    StoredContractMigrationDryRunResult,
    StoredContractMigrationService,
)
from arch_runtime.application.results import GetProjectResult
from arch_runtime.errors import RuntimeConfigurationError
from arch_runtime.persistence.sqlite import (
    SchemaStatus,
    SQLiteConfig,
    SQLiteUnitOfWork,
    inspect_schema,
    migrate_schema,
    open_sqlite_connection,
    require_current_schema,
)
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    ProjectIdGenerator,
    SnapshotIdGenerator,
)
from arch_runtime.ports.storage import SnapshotId
from arch_runtime.replay import ReplayResult, ReplayService


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _ProjectIds:
    def new(self) -> ProjectId:
        return ProjectId.generate()


class _EventIds:
    def new(self) -> EventId:
        return EventId.generate()


class _SnapshotIds:
    def new(self) -> SnapshotId:
        return f"SNP-{ProjectId.generate().ulid}"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Immutable public configuration with optional deterministic dependencies."""

    database: str | Path
    initialize_schema: bool = False
    busy_timeout_ms: int = 5_000
    clock: Clock | None = None
    project_ids: ProjectIdGenerator | None = None
    event_ids: EventIdGenerator | None = None
    snapshot_ids: SnapshotIdGenerator | None = None
    transition_registry: TransitionRegistry = field(default_factory=lambda: TransitionRegistry(()))
    invariant_registry: InvariantRegistry | None = None
    contract_registry: ContractRegistry | None = None
    migration_registry: MigrationRegistry | None = None
    snapshot_policy: SnapshotPolicy = field(default_factory=SnapshotPolicy)

    def __post_init__(self) -> None:
        target = str(self.database)
        if target == ":memory:" or target.startswith("file:"):
            raise RuntimeConfigurationError(
                "Runtime requires a filesystem SQLite database",
                operation="runtime.configure",
                remediation="provide a local SQLite file path",
            )
        SQLiteConfig(self.database, busy_timeout_ms=self.busy_timeout_ms)


class Runtime:
    """Synchronous facade that delegates every operation to an approved service."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        create_projects: CreateProjectService,
        transitions: ApplyTransitionService,
        replay: ReplayService,
        snapshots: SnapshotService,
        recovery: RecoveryService,
        contract_migrations: StoredContractMigrationService,
    ) -> None:
        self._config = config
        self._create_projects = create_projects
        self._transitions = transitions
        self._replay = replay
        self._snapshots = snapshots
        self._recovery = recovery
        self._contract_migrations = contract_migrations
        self._closed = False

    @classmethod
    def open(cls, config: RuntimeConfig | str | Path) -> Self:
        """Validate storage and compose a ready runtime without retaining a connection."""

        resolved = config if isinstance(config, RuntimeConfig) else RuntimeConfig(config)
        path = Path(resolved.database)
        if path.exists() and path.is_dir():
            raise RuntimeConfigurationError(
                "Runtime database path points to a directory",
                operation="runtime.open",
                remediation="provide a SQLite database file path",
            )
        sqlite_config = SQLiteConfig(
            resolved.database,
            busy_timeout_ms=resolved.busy_timeout_ms,
        )
        clock = resolved.clock or _SystemClock()
        cls._prepare_schema(sqlite_config, clock, resolved.initialize_schema)

        def factory() -> SQLiteUnitOfWork:
            return SQLiteUnitOfWork(sqlite_config, clock=clock)

        replay = ReplayService(unit_of_work_factory=factory)
        project_ids = resolved.project_ids or _ProjectIds()
        event_ids = resolved.event_ids or _EventIds()
        snapshot_ids = resolved.snapshot_ids or _SnapshotIds()
        contracts = resolved.contract_registry or (
            resolved.migration_registry.contract_registry
            if resolved.migration_registry is not None
            else build_builtin_contract_registry()
        )
        migrations = resolved.migration_registry or MigrationRegistry((), contracts)
        return cls(
            config=resolved,
            create_projects=CreateProjectService(
                unit_of_work_factory=factory,
                clock=clock,
                project_ids=project_ids,
                event_ids=event_ids,
                invariant_registry=resolved.invariant_registry,
            ),
            transitions=ApplyTransitionService(
                unit_of_work_factory=factory,
                clock=clock,
                event_ids=event_ids,
                transition_registry=resolved.transition_registry,
                invariant_registry=resolved.invariant_registry,
            ),
            replay=replay,
            snapshots=SnapshotService(
                unit_of_work_factory=factory,
                replay_service=replay,
                clock=clock,
                snapshot_ids=snapshot_ids,
                policy=resolved.snapshot_policy,
            ),
            recovery=RecoveryService(
                unit_of_work_factory=factory,
                replay_service=replay,
                clock=clock,
                event_ids=event_ids,
            ),
            contract_migrations=StoredContractMigrationService(
                unit_of_work_factory=factory,
                clock=clock,
                event_ids=event_ids,
                contract_registry=contracts,
                migration_registry=migrations,
                replay_verifier=replay,
            ),
        )

    @staticmethod
    def _prepare_schema(config: SQLiteConfig, clock: Clock, initialize: bool) -> None:
        connection = open_sqlite_connection(config)
        try:
            inspection = inspect_schema(connection)
            if inspection.status is SchemaStatus.EMPTY and initialize:
                migrate_schema(connection, clock=clock)
            else:
                require_current_schema(connection)
        finally:
            connection.close()

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def closed(self) -> bool:
        return self._closed

    def create_project(self, command: CreateProjectCommand) -> CreateProjectResult:
        self._require_open()
        return self._create_projects.create_project(command)

    def get_project(self, project_id: ProjectId) -> GetProjectResult:
        self._require_open()
        replay = self._replay.replay_project(project_id)
        return GetProjectResult(
            project_id=project_id,
            state=replay.reconstructed_state,
            record_version=replay.record_version,
            record_fingerprint=replay.record_fingerprint,
            content_fingerprint=replay.content_fingerprint,
            stream_position=replay.last_stream_position,
            stream_fingerprint=replay.last_event_fingerprint,
        )

    def apply_transition(self, command: ApplyTransitionCommand) -> ApplyTransitionResult:
        self._require_open()
        return self._transitions.apply_transition(command)

    def replay_project(self, project_id: ProjectId) -> ReplayResult:
        self._require_open()
        return self._replay.replay_project(project_id)

    def create_snapshot(self, command: CreateSnapshotCommand) -> CreateSnapshotResult:
        self._require_open()
        return self._snapshots.create_snapshot(command)

    def recover_project(self, command: RecoverAggregateCommand) -> RecoverAggregateResult:
        self._require_open()
        return self._recovery.recover_aggregate(command)

    def plan_project_contract_migration(
        self, command: PlanStoredContractMigrationCommand
    ) -> StoredContractMigrationDryRunResult:
        self._require_open()
        return self._contract_migrations.dry_run(command)

    def migrate_project_contracts(
        self, command: ApplyStoredContractMigrationCommand
    ) -> ApplyStoredContractMigrationResult:
        self._require_open()
        return self._contract_migrations.apply(command)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeConfigurationError(
                "Runtime is closed",
                operation="runtime.lifecycle",
                remediation="open a new Runtime instance",
            )
