"""Deterministic explicit snapshot creation and retention."""

from typing import Annotated

from arch_kernel.contracts import ProjectState
from arch_kernel.kernel import build_builtin_contract_registry, canonicalize_json
from pydantic import BaseModel, ConfigDict, Field

from arch_runtime.application.commands import CreateSnapshotCommand
from arch_runtime.application.results import CreateSnapshotResult
from arch_runtime.errors import ConcurrentModificationError, PersistenceError, ReplayIntegrityError
from arch_runtime.ports import Clock, SnapshotIdGenerator, StoredSnapshot, UnitOfWorkFactory
from arch_runtime.replay import ReplayService
from arch_runtime.replay.contracts import ReplayResult


class SnapshotPolicy(BaseModel):
    """Version-based policy whose decision is independent of wall-clock time."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    every_n_versions: Annotated[int, Field(ge=1)] = 10
    minimum_version: Annotated[int, Field(ge=1)] = 1
    retain_latest: Annotated[int, Field(ge=1)] = 3

    def should_create(self, aggregate_version: int) -> bool:
        return (
            aggregate_version >= self.minimum_version
            and (aggregate_version - self.minimum_version) % self.every_n_versions == 0
        )


class SnapshotService:
    """Create a derived checkpoint only from replay-verified committed evidence."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        replay_service: ReplayService,
        clock: Clock,
        snapshot_ids: SnapshotIdGenerator,
        policy: SnapshotPolicy,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._replay = replay_service
        self._clock = clock
        self._snapshot_ids = snapshot_ids
        self._policy = policy

    def create_snapshot(self, command: CreateSnapshotCommand) -> CreateSnapshotResult:
        replay = self._replay.replay_project(command.project_id)
        with self._unit_of_work_factory() as unit_of_work:
            stream = unit_of_work.events.read_stream(command.project_id)
            snapshots = unit_of_work.snapshots.list_for_project(command.project_id)
        tail = stream[-1]
        self._require_expected(
            command,
            replay.record_version,
            replay.record_fingerprint,
            replay.content_fingerprint,
            tail.stream_position,
            tail.event_fingerprint,
        )
        existing = next(
            (item for item in snapshots if item.aggregate_version == replay.record_version), None
        )
        if existing is not None:
            self._require_snapshot_matches(existing, replay, tail.stream_position)
            return self._result(
                command,
                replay,
                tail.event_fingerprint,
                snapshots,
                policy_matched=True,
                created=False,
                selected=existing,
            )

        matched = command.force or self._policy.should_create(replay.record_version)
        if not matched:
            return self._result(
                command,
                replay,
                tail.event_fingerprint,
                snapshots,
                policy_matched=False,
                created=False,
                selected=None,
            )

        registration = build_builtin_contract_registry().get_by_model(ProjectState)
        if registration is None:
            raise PersistenceError(
                "ProjectState contract registration is unavailable",
                operation="snapshot.create",
                remediation="install a compatible arch-kernel release",
                project_id=command.project_id,
            )
        snapshot = StoredSnapshot(
            snapshot_id=self._snapshot_ids.new(),
            project_id=command.project_id,
            aggregate_version=replay.record_version,
            last_stream_position=tail.stream_position,
            contract_name=replay.reconstructed_state.contract_name,
            contract_version=replay.reconstructed_state.contract_version,
            schema_fingerprint=registration.descriptor.schema_fingerprint,
            record_fingerprint=replay.record_fingerprint,
            content_fingerprint=replay.content_fingerprint,
            state_json=canonicalize_json(replay.reconstructed_state),
            created_at=self._clock.now(),
        )
        retained, deleted, created, selected = self._persist(
            command, replay, tail.event_fingerprint, snapshot
        )
        return CreateSnapshotResult(
            project_id=command.project_id,
            policy_matched=True,
            created=created,
            snapshot_id=selected.snapshot_id,
            record_version=replay.record_version,
            record_fingerprint=replay.record_fingerprint,
            content_fingerprint=replay.content_fingerprint,
            stream_position=tail.stream_position,
            stream_fingerprint=tail.event_fingerprint,
            retained_snapshot_ids=tuple(item.snapshot_id for item in retained),
            deleted_snapshot_ids=tuple(item.snapshot_id for item in deleted),
        )

    def _persist(
        self,
        command: CreateSnapshotCommand,
        replay: ReplayResult,
        tail_fingerprint: str,
        snapshot: StoredSnapshot,
    ) -> tuple[
        tuple[StoredSnapshot, ...],
        tuple[StoredSnapshot, ...],
        bool,
        StoredSnapshot,
    ]:
        with self._unit_of_work_factory() as unit_of_work:
            current = unit_of_work.projects.get(command.project_id)
            stream = unit_of_work.events.read_stream(command.project_id)
            if current is None or not stream:
                raise ConcurrentModificationError(
                    "project evidence disappeared before snapshot commit",
                    operation="snapshot.create.commit",
                    remediation="reload and reevaluate snapshot creation",
                    project_id=command.project_id,
                )
            tail = stream[-1]
            if (
                current.record_version != command.expected_record_version
                or current.record_fingerprint != command.expected_record_fingerprint
                or tail.stream_position != command.expected_stream_position
                or tail.event_fingerprint != tail_fingerprint
            ):
                raise ConcurrentModificationError(
                    "project or event tail changed before snapshot commit",
                    operation="snapshot.create.commit",
                    remediation="reload and reevaluate snapshot creation",
                    project_id=command.project_id,
                )
            existing = unit_of_work.snapshots.list_for_project(command.project_id)
            duplicate = next(
                (item for item in existing if item.aggregate_version == snapshot.aggregate_version),
                None,
            )
            if duplicate is not None:
                self._require_snapshot_matches(duplicate, replay, tail.stream_position)
                return existing, (), False, duplicate
            unit_of_work.snapshots.save(snapshot)
            ordered = tuple(sorted((*existing, snapshot), key=_snapshot_order, reverse=True))
            retained = ordered[: self._policy.retain_latest]
            deleted = ordered[self._policy.retain_latest :]
            for item in deleted:
                unit_of_work.snapshots.delete(command.project_id, item.snapshot_id)
            unit_of_work.commit()
            return retained, deleted, True, snapshot

    @staticmethod
    def _require_expected(
        command: CreateSnapshotCommand,
        version: int,
        record: str,
        content: str,
        position: int,
        tail: str,
    ) -> None:
        if (version, record, content, position, tail) != (
            command.expected_record_version,
            command.expected_record_fingerprint,
            command.expected_content_fingerprint,
            command.expected_stream_position,
            command.expected_stream_fingerprint,
        ):
            raise ConcurrentModificationError(
                "verified project evidence differs from snapshot preconditions",
                operation="snapshot.create.precondition",
                remediation="reload the project and stream tail",
                project_id=command.project_id,
            )

    @staticmethod
    def _require_snapshot_matches(
        snapshot: StoredSnapshot, replay: ReplayResult, position: int
    ) -> None:
        expected = (
            replay.record_version,
            replay.record_fingerprint,
            replay.content_fingerprint,
            position,
        )
        actual = (
            snapshot.aggregate_version,
            snapshot.record_fingerprint,
            snapshot.content_fingerprint,
            snapshot.last_stream_position,
        )
        if actual != expected:
            raise ReplayIntegrityError(
                "snapshot for aggregate version disagrees with verified replay",
                operation="snapshot.create",
                remediation="preserve evidence and investigate before creating snapshots",
                project_id=snapshot.project_id,
            )

    @staticmethod
    def _result(
        command: CreateSnapshotCommand,
        replay: ReplayResult,
        tail_fingerprint: str,
        snapshots: tuple[StoredSnapshot, ...],
        *,
        policy_matched: bool,
        created: bool,
        selected: StoredSnapshot | None,
    ) -> CreateSnapshotResult:
        return CreateSnapshotResult(
            project_id=command.project_id,
            policy_matched=policy_matched,
            created=created,
            snapshot_id=None if selected is None else selected.snapshot_id,
            record_version=replay.record_version,
            record_fingerprint=replay.record_fingerprint,
            content_fingerprint=replay.content_fingerprint,
            stream_position=command.expected_stream_position,
            stream_fingerprint=tail_fingerprint,
            retained_snapshot_ids=tuple(item.snapshot_id for item in snapshots),
        )


def _snapshot_order(snapshot: StoredSnapshot) -> tuple[int, object, str]:
    return snapshot.aggregate_version, snapshot.created_at, snapshot.snapshot_id


def create_snapshot(
    service: SnapshotService, command: CreateSnapshotCommand
) -> CreateSnapshotResult:
    return service.create_snapshot(command)
