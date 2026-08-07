"""Explicit K10 integration for persisted contract payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NoReturn, Protocol, cast

from arch_kernel.contracts import (
    EventEnvelope,
    EventId,
    FrozenJsonObject,
    MigrationPolicy,
    MigrationRequest,
    MigrationResult,
    ProjectId,
    ProjectState,
    SemanticVersion,
)
from arch_kernel.kernel import (
    ContractRegistry,
    MigrationRegistry,
    build_builtin_contract_registry,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
    run_migration_dry_run,
)

from arch_runtime.application.commands import (
    ApplyStoredContractMigrationCommand,
    PlanStoredContractMigrationCommand,
)
from arch_runtime.application.events import build_project_event
from arch_runtime.application.idempotency import resolve_completed_result
from arch_runtime.application.results import (
    ApplyStoredContractMigrationResult,
    StoredContractKind,
    StoredContractMigrationDryRunResult,
    StoredContractMigrationItem,
)
from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    ProjectNotFoundError,
    UnsupportedStoredContractError,
)
from arch_runtime.ports import (
    Clock,
    EventIdGenerator,
    IdempotencyRecord,
    IdempotencyStatus,
    StoredEvent,
    StoredProject,
    StoredSnapshot,
    UnitOfWork,
    UnitOfWorkFactory,
)

_OPERATION = "project.contract_migrate"


class TransactionReplayVerifier(Protocol):
    def replay_project_in_unit_of_work(
        self, unit_of_work: UnitOfWork, project_id: ProjectId
    ) -> object: ...


class StoredContractMigrationService:
    """Plan with K10, then explicitly persist one atomic migration projection."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        clock: Clock,
        event_ids: EventIdGenerator,
        contract_registry: ContractRegistry,
        migration_registry: MigrationRegistry,
        replay_verifier: TransactionReplayVerifier,
        migration_policy: MigrationPolicy | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._event_ids = event_ids
        self._contracts = contract_registry
        self._migrations = migration_registry
        self._replay = replay_verifier
        self._policy = migration_policy or MigrationPolicy()

    def dry_run(
        self, command: PlanStoredContractMigrationCommand
    ) -> StoredContractMigrationDryRunResult:
        now = self._clock.now()
        with self._unit_of_work_factory() as unit_of_work:
            aggregate = unit_of_work.projects.get_stored(command.project_id)
            events = unit_of_work.events.read_stored_stream(command.project_id)
            snapshots = unit_of_work.snapshots.list_stored_for_project(command.project_id)
        if aggregate is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.contract_migrate.plan",
                remediation="create the project before planning migration",
                project_id=command.project_id,
            )
        if not events:
            raise CorruptStoredRecordError(
                "project event stream is empty",
                operation="project.contract_migrate.plan",
                remediation="preserve storage and investigate missing history",
                project_id=command.project_id,
            )
        _verify_event_chain(events, command.project_id)
        tail = events[-1]
        _require_plan_preconditions(command, aggregate, tail)
        project_name, event_name = _current_canonical_names()
        _require_current_targets(
            self._contracts,
            project_name,
            event_name,
            command.target_project_version,
            command.target_event_version,
            command.project_id,
        )

        items: list[StoredContractMigrationItem] = []
        migrated_events = _migrated_event_targets(events, command.project_id)
        aggregate_item = self._project_item(
            StoredContractKind.AGGREGATE,
            str(command.project_id),
            aggregate,
            command.target_project_version,
            project_name,
            now,
        )
        if aggregate_item is not None:
            items.append(aggregate_item)
        for event in events:
            if migrated_events.get(event.event_fingerprint) == str(command.target_event_version):
                continue
            item = self._event_item(event, command.target_event_version, event_name, now)
            if item is not None:
                items.append(item)
        for snapshot in snapshots:
            item = self._project_item(
                StoredContractKind.SNAPSHOT,
                snapshot.snapshot_id,
                snapshot,
                command.target_project_version,
                project_name,
                now,
            )
            if item is not None:
                items.append(item)

        success = all(item.migration_result.success for item in items)
        projected = _projected_state(aggregate, aggregate_item) if success else None
        plan_fingerprint = _plan_fingerprint(
            command,
            aggregate,
            tail,
            self._migrations.registry_fingerprint,
            tuple(items),
        )
        return StoredContractMigrationDryRunResult(
            success=success,
            changed=bool(items),
            project_id=command.project_id,
            evaluated_at=now,
            before_record_version=aggregate.record_version,
            before_record_fingerprint=aggregate.record_fingerprint,
            before_content_fingerprint=aggregate.content_fingerprint,
            stream_position=tail.stream_position,
            stream_fingerprint=tail.event_fingerprint,
            migration_registry_fingerprint=self._migrations.registry_fingerprint,
            items=tuple(items),
            projected_state=projected,
            plan_fingerprint=plan_fingerprint,
        )

    def apply(
        self, command: ApplyStoredContractMigrationCommand
    ) -> ApplyStoredContractMigrationResult:
        request_fingerprint = compute_fingerprint(command.request_payload())
        completed = self._find_completed(command, request_fingerprint)
        if completed is not None:
            return completed
        dry_run = self.dry_run(command.plan)
        if dry_run.plan_fingerprint != command.expected_plan_fingerprint:
            raise ConcurrentModificationError(
                "stored migration plan differs from the authorized dry run",
                operation="project.contract_migrate.apply",
                remediation="review and authorize a fresh dry-run plan",
                project_id=command.plan.project_id,
            )
        event_id = self._event_ids.new() if dry_run.success and dry_run.changed else None
        return self._persist(
            command,
            request_fingerprint,
            dry_run,
            event_id,
            self._clock.now(),
        )

    def _project_item(
        self,
        kind: StoredContractKind,
        record_id: str,
        stored: StoredProject | StoredSnapshot,
        target_version: SemanticVersion,
        canonical_name: str,
        now: datetime,
    ) -> StoredContractMigrationItem | None:
        payload = _verified_json(stored.state_json, stored.record_fingerprint, stored.project_id)
        if compute_content_fingerprint(payload) != stored.content_fingerprint:
            _corrupt("stored project content fingerprint is invalid", stored.project_id)
        source = self._registration(canonical_name, stored.contract_version, stored.project_id)
        if source.descriptor.schema_fingerprint != stored.schema_fingerprint:
            _corrupt("stored project source schema fingerprint is invalid", stored.project_id)
        if stored.contract_version == target_version:
            return None
        result = self._run(canonical_name, stored.contract_version, target_version, payload, now)
        return _item(
            kind,
            record_id,
            stored.contract_version,
            target_version,
            stored.schema_fingerprint,
            stored.record_fingerprint,
            stored.content_fingerprint,
            result,
        )

    def _event_item(
        self,
        stored: StoredEvent,
        target_version: SemanticVersion,
        canonical_name: str,
        now: datetime,
    ) -> StoredContractMigrationItem | None:
        payload = _verified_json(stored.event_json, stored.event_fingerprint, stored.project_id)
        _require_event_identity(stored, payload)
        source = self._registration(canonical_name, stored.schema_version, stored.project_id)
        if stored.schema_version == target_version:
            return None
        result = self._run(canonical_name, stored.schema_version, target_version, payload, now)
        return _item(
            StoredContractKind.EVENT,
            str(stored.event_id),
            stored.schema_version,
            target_version,
            source.descriptor.schema_fingerprint,
            stored.event_fingerprint,
            None,
            result,
        )

    def _run(
        self,
        canonical_name: str,
        source_version: SemanticVersion,
        target_version: SemanticVersion,
        payload: dict[str, Any],
        now: datetime,
    ) -> MigrationResult:
        return run_migration_dry_run(
            MigrationRequest(
                canonical_name=canonical_name,
                source_version=source_version,
                target_version=target_version,
                source_payload=cast(FrozenJsonObject, payload),
                expected_source_payload_fingerprint=compute_fingerprint(payload),
                expected_registry_fingerprint=self._migrations.registry_fingerprint,
                evaluated_at=now,
                policy=self._policy,
            ),
            self._migrations,
            self._contracts,
        )

    def _registration(
        self, canonical_name: str, version: SemanticVersion, project_id: ProjectId
    ) -> Any:
        registration = self._contracts.get(canonical_name, version)
        if registration is None:
            raise UnsupportedStoredContractError(
                "stored contract version is absent from the injected registry",
                operation="project.contract_migrate.plan",
                remediation="install and inject the approved historical contract catalog",
                project_id=project_id,
            )
        return registration

    def _find_completed(
        self,
        command: ApplyStoredContractMigrationCommand,
        request_fingerprint: str,
    ) -> ApplyStoredContractMigrationResult | None:
        with self._unit_of_work_factory() as unit_of_work:
            return resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                ApplyStoredContractMigrationResult,
                operation=_OPERATION,
            )

    def _persist(
        self,
        command: ApplyStoredContractMigrationCommand,
        request_fingerprint: str,
        dry_run: StoredContractMigrationDryRunResult,
        event_id: EventId | None,
        now: datetime,
    ) -> ApplyStoredContractMigrationResult:
        project_id = command.plan.project_id
        with self._unit_of_work_factory() as unit_of_work:
            completed = resolve_completed_result(
                unit_of_work.idempotency.get(_OPERATION, command.idempotency_key),
                request_fingerprint,
                ApplyStoredContractMigrationResult,
                operation=_OPERATION,
            )
            if completed is not None:
                return completed
            unit_of_work.idempotency.reserve(
                IdempotencyRecord(
                    operation_name=_OPERATION,
                    idempotency_key=command.idempotency_key,
                    project_id=project_id,
                    request_fingerprint=request_fingerprint,
                    status=IdempotencyStatus.IN_PROGRESS,
                    created_at=now,
                )
            )
            aggregate = unit_of_work.projects.get_stored(project_id)
            events = unit_of_work.events.read_stored_stream(project_id)
            if aggregate is None or not events:
                raise ConcurrentModificationError(
                    "stored migration evidence disappeared before commit",
                    operation="project.contract_migrate.commit",
                    remediation="rerun and authorize a fresh migration plan",
                    project_id=project_id,
                )
            tail = events[-1]
            if (
                aggregate.record_version != dry_run.before_record_version
                or aggregate.record_fingerprint != dry_run.before_record_fingerprint
                or aggregate.content_fingerprint != dry_run.before_content_fingerprint
                or tail.stream_position != dry_run.stream_position
                or tail.event_fingerprint != dry_run.stream_fingerprint
            ):
                raise ConcurrentModificationError(
                    "aggregate or stream changed after migration dry run",
                    operation="project.contract_migrate.commit",
                    remediation="rerun and authorize a fresh migration plan",
                    project_id=project_id,
                )

            position: int | None = None
            if dry_run.success and dry_run.changed:
                assert event_id is not None
                assert dry_run.projected_state is not None
                aggregate_item = _find_item(dry_run.items, StoredContractKind.AGGREGATE)
                if aggregate_item is not None:
                    unit_of_work.projects.save(
                        dry_run.projected_state,
                        expected_record_version=aggregate.record_version,
                        expected_record_fingerprint=aggregate.record_fingerprint,
                    )
                snapshots = {
                    item.snapshot_id: item
                    for item in unit_of_work.snapshots.list_stored_for_project(project_id)
                }
                for item in dry_run.items:
                    if item.kind is StoredContractKind.SNAPSHOT:
                        current = snapshots.get(item.record_id)
                        if current is None:
                            raise ConcurrentModificationError(
                                "snapshot disappeared after migration dry run",
                                operation="project.contract_migrate.commit",
                                remediation="rerun and authorize a fresh migration plan",
                                project_id=project_id,
                            )
                        unit_of_work.snapshots.replace(
                            _migrated_snapshot(current, item),
                            expected_record_fingerprint=item.before_record_fingerprint,
                        )
                event = _build_migration_event(command, aggregate, dry_run, event_id, now)
                position = unit_of_work.events.append(
                    event,
                    version_before=dry_run.projected_state.record_version,
                    version_after=dry_run.projected_state.record_version,
                    previous_event_fingerprint=tail.event_fingerprint,
                )
            result = ApplyStoredContractMigrationResult(
                success=dry_run.success,
                changed=dry_run.changed,
                project_id=project_id,
                dry_run=dry_run,
                event_id=event_id,
                stream_position=position,
            )
            if dry_run.success and dry_run.changed:
                self._replay.replay_project_in_unit_of_work(unit_of_work, project_id)
            unit_of_work.idempotency.complete(_OPERATION, command.idempotency_key, result)
            unit_of_work.commit()
            return result


def _current_canonical_names() -> tuple[str, str]:
    registry = build_builtin_contract_registry()
    project = registry.get_by_model(ProjectState)
    event = registry.get_by_model(EventEnvelope)
    if project is None or event is None:
        raise UnsupportedStoredContractError(
            "current runtime contracts are absent from the kernel registry",
            operation="project.contract_migrate.initialize",
            remediation="install a compatible arch-kernel release",
        )
    return project.descriptor.canonical_name, event.descriptor.canonical_name


def _require_current_targets(
    contracts: ContractRegistry,
    project_name: str,
    event_name: str,
    project_version: SemanticVersion,
    event_version: SemanticVersion,
    project_id: ProjectId,
) -> None:
    builtins = build_builtin_contract_registry()
    current_project = builtins.get_by_model(ProjectState)
    current_event = builtins.get_by_model(EventEnvelope)
    assert current_project is not None
    assert current_event is not None
    if (
        project_version != current_project.descriptor.version
        or event_version != current_event.descriptor.version
        or contracts.get(project_name, project_version) is None
        or contracts.get(event_name, event_version) is None
    ):
        raise UnsupportedStoredContractError(
            "migration target is not the runtime's current public contract",
            operation="project.contract_migrate.plan",
            remediation="target the installed ProjectState and EventEnvelope versions",
            project_id=project_id,
        )


def _verified_json(payload: bytes, fingerprint: str, project_id: ProjectId) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _corrupt("stored contract JSON is invalid", project_id, error)
    if not isinstance(value, dict):
        _corrupt("stored contract payload is not an object", project_id)
    if canonicalize_json(value) != payload or compute_fingerprint(value) != fingerprint:
        _corrupt("stored contract evidence is not canonical", project_id)
    return cast(dict[str, Any], value)


def _require_event_identity(stored: StoredEvent, payload: Mapping[str, Any]) -> None:
    if (
        payload.get("event_id") != str(stored.event_id)
        or payload.get("project_id") != str(stored.project_id)
        or payload.get("event_type") != stored.event_type
        or payload.get("schema_version") != str(stored.schema_version)
        or payload.get("idempotency_key") != stored.idempotency_key
    ):
        _corrupt("stored event indexed evidence disagrees with payload", stored.project_id)


def _verify_event_chain(events: tuple[StoredEvent, ...], project_id: ProjectId) -> None:
    previous: str | None = None
    position = 0
    for event in events:
        if (
            event.project_id != project_id
            or event.stream_position <= position
            or event.previous_event_fingerprint != previous
        ):
            _corrupt("stored project event chain is invalid", project_id)
        _verified_json(event.event_json, event.event_fingerprint, project_id)
        previous = event.event_fingerprint
        position = event.stream_position


def _migrated_event_targets(
    events: tuple[StoredEvent, ...], project_id: ProjectId
) -> dict[str, str]:
    migrated: dict[str, str] = {}
    known = {event.event_fingerprint: event for event in events}
    for stored in events:
        if stored.event_type != "project.aggregate.contract_migrated":
            continue
        payload = _verified_json(stored.event_json, stored.event_fingerprint, project_id)
        try:
            envelope = EventEnvelope.model_validate_json(canonicalize_json(payload), strict=True)
        except ValueError as error:
            _corrupt("stored migration audit envelope is invalid", project_id, error)
        projections = envelope.model_dump(mode="json")["payload"].get("event_projections")
        if not isinstance(projections, list):
            _corrupt("stored migration audit projections are invalid", project_id)
        for projection in projections:
            if not isinstance(projection, dict):
                _corrupt("stored migration audit projection is invalid", project_id)
            source_fingerprint = projection.get("source_event_fingerprint")
            target_version = projection.get("target_schema_version")
            target_event = projection.get("target_event")
            target_fingerprint = projection.get("target_event_fingerprint")
            source = known.get(source_fingerprint) if isinstance(source_fingerprint, str) else None
            if (
                source is None
                or source.stream_position >= stored.stream_position
                or not isinstance(target_version, str)
                or not isinstance(target_event, dict)
                or not isinstance(target_fingerprint, str)
                or compute_fingerprint(target_event) != target_fingerprint
                or source_fingerprint in migrated
            ):
                _corrupt("stored migration audit projection evidence is inconsistent", project_id)
            migrated[cast(str, source_fingerprint)] = target_version
    return migrated


def _item(
    kind: StoredContractKind,
    record_id: str,
    source_version: SemanticVersion,
    target_version: SemanticVersion,
    before_schema_fingerprint: str,
    before_record_fingerprint: str,
    before_content_fingerprint: str | None,
    result: MigrationResult,
) -> StoredContractMigrationItem:
    payload = result.final_payload
    after_record = None if payload is None else compute_fingerprint(payload)
    after_content = (
        compute_content_fingerprint(payload)
        if payload is not None and kind is not StoredContractKind.EVENT
        else None
    )
    return StoredContractMigrationItem(
        kind=kind,
        record_id=record_id,
        source_version=source_version,
        target_version=target_version,
        before_schema_fingerprint=before_schema_fingerprint,
        before_record_fingerprint=before_record_fingerprint,
        before_content_fingerprint=before_content_fingerprint,
        after_schema_fingerprint=result.target_schema_fingerprint if result.success else None,
        after_record_fingerprint=after_record,
        after_content_fingerprint=after_content,
        after_payload=payload,
        migration_result=result,
    )


def _projected_state(
    aggregate: StoredProject, item: StoredContractMigrationItem | None
) -> ProjectState:
    payload: object = json.loads(aggregate.state_json) if item is None else item.after_payload
    try:
        return ProjectState.model_validate_json(canonicalize_json(payload), strict=True)
    except ValueError as error:
        _corrupt("migrated aggregate is not the current ProjectState", aggregate.project_id, error)


def _plan_fingerprint(
    command: PlanStoredContractMigrationCommand,
    aggregate: StoredProject,
    tail: StoredEvent,
    registry_fingerprint: str,
    items: tuple[StoredContractMigrationItem, ...],
) -> str:
    return compute_fingerprint(
        {
            "command": command,
            "aggregate_fingerprint": aggregate.record_fingerprint,
            "tail_fingerprint": tail.event_fingerprint,
            "migration_registry_fingerprint": registry_fingerprint,
            "items": tuple(
                {
                    "kind": item.kind.value,
                    "record_id": item.record_id,
                    "source_version": str(item.source_version),
                    "target_version": str(item.target_version),
                    "before_record_fingerprint": item.before_record_fingerprint,
                    "after_record_fingerprint": item.after_record_fingerprint,
                    "migration_result_fingerprint": item.migration_result.result_fingerprint,
                }
                for item in items
            ),
        }
    )


def _find_item(
    items: tuple[StoredContractMigrationItem, ...], kind: StoredContractKind
) -> StoredContractMigrationItem | None:
    return next((item for item in items if item.kind is kind), None)


def _migrated_snapshot(
    current: StoredSnapshot, item: StoredContractMigrationItem
) -> StoredSnapshot:
    assert item.after_payload is not None
    assert item.after_schema_fingerprint is not None
    assert item.after_record_fingerprint is not None
    assert item.after_content_fingerprint is not None
    return current.model_copy(
        update={
            "contract_version": item.target_version,
            "schema_fingerprint": item.after_schema_fingerprint,
            "record_fingerprint": item.after_record_fingerprint,
            "content_fingerprint": item.after_content_fingerprint,
            "state_json": canonicalize_json(item.after_payload),
        }
    )


def _build_migration_event(
    command: ApplyStoredContractMigrationCommand,
    aggregate: StoredProject,
    dry_run: StoredContractMigrationDryRunResult,
    event_id: EventId,
    now: datetime,
) -> EventEnvelope:
    assert dry_run.projected_state is not None
    state = dry_run.projected_state
    after = {
        "record_version": state.record_version,
        "record_fingerprint": compute_fingerprint(state),
        "content_fingerprint": compute_content_fingerprint(state),
    }
    event_projections = []
    for item in dry_run.items:
        if item.kind is not StoredContractKind.EVENT:
            continue
        assert item.after_payload is not None
        assert item.after_record_fingerprint is not None
        event_projections.append(
            {
                "source_event_id": item.record_id,
                "source_event_fingerprint": item.before_record_fingerprint,
                "source_schema_version": str(item.source_version),
                "target_schema_version": str(item.target_version),
                "target_event_fingerprint": item.after_record_fingerprint,
                "target_event": json.loads(canonicalize_json(item.after_payload)),
            }
        )
    return build_project_event(
        event_id=event_id,
        event_type="project.aggregate.contract_migrated",
        semantic_name="project.contract_migrated",
        project_id=command.plan.project_id,
        aggregate_version=state.contract_version,
        idempotency_key=command.idempotency_key,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        actor_display_name=command.actor_display_name,
        occurred_at=now,
        payload={
            "reason": command.reason,
            "plan_fingerprint": dry_run.plan_fingerprint,
            "migration_registry_fingerprint": dry_run.migration_registry_fingerprint,
            "stream_before": after,
            "materialized_before": {
                "record_version": aggregate.record_version,
                "record_fingerprint": aggregate.record_fingerprint,
                "content_fingerprint": aggregate.content_fingerprint,
            },
            "after": after,
            "event_projections": event_projections,
            "migrated_records": [
                {
                    "kind": item.kind.value,
                    "record_id": item.record_id,
                    "source_version": str(item.source_version),
                    "target_version": str(item.target_version),
                    "before_record_fingerprint": item.before_record_fingerprint,
                    "after_record_fingerprint": item.after_record_fingerprint,
                }
                for item in dry_run.items
            ],
        },
    )


def _require_plan_preconditions(
    command: PlanStoredContractMigrationCommand,
    aggregate: StoredProject,
    tail: StoredEvent,
) -> None:
    if (
        aggregate.record_version != command.expected_record_version
        or aggregate.record_fingerprint != command.expected_record_fingerprint
        or aggregate.content_fingerprint != command.expected_content_fingerprint
        or tail.stream_position != command.expected_stream_position
        or tail.event_fingerprint != command.expected_stream_fingerprint
    ):
        raise ConcurrentModificationError(
            "stored evidence differs from migration plan preconditions",
            operation="project.contract_migrate.plan",
            remediation="reload aggregate and stream evidence",
            project_id=command.project_id,
        )


def _corrupt(message: str, project_id: ProjectId, cause: BaseException | None = None) -> NoReturn:
    error = CorruptStoredRecordError(
        message,
        operation="project.contract_migrate.verify",
        remediation="preserve storage and inspect historical evidence",
        project_id=project_id,
    )
    if cause is None:
        raise error
    raise error from cause


def dry_run_stored_contract_migration(
    service: StoredContractMigrationService,
    command: PlanStoredContractMigrationCommand,
) -> StoredContractMigrationDryRunResult:
    return service.dry_run(command)


def apply_stored_contract_migration(
    service: StoredContractMigrationService,
    command: ApplyStoredContractMigrationCommand,
) -> ApplyStoredContractMigrationResult:
    return service.apply(command)
