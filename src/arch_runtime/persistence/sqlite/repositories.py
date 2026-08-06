"""SQLite implementations of the R02 persistence ports."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from arch_kernel.contracts import EventEnvelope, ProjectId, ProjectState, SemanticVersion
from arch_kernel.kernel import canonicalize_json, compute_fingerprint, ensure_utc
from pydantic import ValidationError

from arch_runtime.errors import (
    ConcurrentModificationError,
    CorruptStoredRecordError,
    IdempotencyConflictError,
    PersistenceError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from arch_runtime.ports import Clock
from arch_runtime.ports.storage import (
    IdempotencyRecord,
    IdempotencyStatus,
    SnapshotId,
    StoredEvent,
    StoredProject,
    StoredSnapshot,
)


def _time(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _persistence_error(
    operation: str, cause: sqlite3.Error, project_id: ProjectId | None = None
) -> PersistenceError:
    return PersistenceError(
        "SQLite persistence operation failed",
        operation=operation,
        remediation="inspect the database and retry only from a fresh Unit of Work",
        project_id=project_id,
    )


class SQLiteProjectRepository:
    def __init__(self, connection: sqlite3.Connection, codec: KernelContractCodec) -> None:
        self._connection = connection
        self._codec = codec

    def get(self, project_id: ProjectId) -> StoredProject | None:
        row = self._connection.execute(
            "SELECT * FROM project_aggregates WHERE project_id = ?", (str(project_id),)
        ).fetchone()
        if row is None:
            return None
        try:
            stored = StoredProject(
                project_id=ProjectId(row["project_id"]),
                record_version=row["record_version"],
                contract_name=row["contract_name"],
                contract_version=SemanticVersion.parse(row["contract_version"]),
                schema_fingerprint=row["schema_fingerprint"],
                record_fingerprint=row["record_fingerprint"],
                content_fingerprint=row["content_fingerprint"],
                state_json=bytes(row["state_json"]),
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
            )
            self._codec.verify_project(stored)
        except (ValidationError, ValueError, TypeError) as error:
            raise CorruptStoredRecordError(
                "stored project row is invalid",
                operation="project.get",
                remediation="preserve the database and restore verified evidence",
                project_id=project_id,
            ) from error
        return stored

    def add(self, state: ProjectState) -> None:
        encoded = self._codec.encode_project(state)
        updated_at = state.updated_at or state.created_at
        try:
            self._connection.execute(
                "INSERT INTO project_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(state.metadata.project_id),
                    state.record_version,
                    state.contract_name,
                    str(state.contract_version),
                    encoded.schema_fingerprint,
                    encoded.record_fingerprint,
                    encoded.content_fingerprint,
                    encoded.state_json,
                    _time(state.created_at),
                    _time(updated_at),
                ),
            )
        except sqlite3.IntegrityError as error:
            exists = self._connection.execute(
                "SELECT 1 FROM project_aggregates WHERE project_id=?",
                (str(state.metadata.project_id),),
            ).fetchone()
            if exists is not None:
                raise ProjectAlreadyExistsError(
                    "project already exists",
                    operation="project.add",
                    remediation="load the existing project",
                    project_id=state.metadata.project_id,
                ) from error
            raise _persistence_error("project.add", error, state.metadata.project_id) from error
        except sqlite3.Error as error:
            raise _persistence_error("project.add", error, state.metadata.project_id) from error

    def save(
        self, state: ProjectState, *, expected_record_version: int, expected_record_fingerprint: str
    ) -> None:
        encoded = self._codec.encode_project(state)
        updated_at = state.updated_at or state.created_at
        try:
            cursor = self._connection.execute(
                "UPDATE project_aggregates SET record_version=?, contract_name=?, "
                "contract_version=?, schema_fingerprint=?, record_fingerprint=?, "
                "content_fingerprint=?, state_json=?, created_at=?, "
                "updated_at=? WHERE project_id=? AND record_version=? AND record_fingerprint=?",
                (
                    state.record_version,
                    state.contract_name,
                    str(state.contract_version),
                    encoded.schema_fingerprint,
                    encoded.record_fingerprint,
                    encoded.content_fingerprint,
                    encoded.state_json,
                    _time(state.created_at),
                    _time(updated_at),
                    str(state.metadata.project_id),
                    expected_record_version,
                    expected_record_fingerprint,
                ),
            )
        except sqlite3.Error as error:
            raise _persistence_error("project.save", error, state.metadata.project_id) from error
        if cursor.rowcount == 1:
            return
        exists = self._connection.execute(
            "SELECT 1 FROM project_aggregates WHERE project_id=?", (str(state.metadata.project_id),)
        ).fetchone()
        if exists is None:
            raise ProjectNotFoundError(
                "project not found",
                operation="project.save",
                remediation="create the project first",
                project_id=state.metadata.project_id,
            )
        raise ConcurrentModificationError(
            "project changed concurrently",
            operation="project.save",
            remediation="reload and reevaluate the command",
            project_id=state.metadata.project_id,
        )


class SQLiteEventStore:
    def __init__(self, connection: sqlite3.Connection, codec: KernelContractCodec) -> None:
        self._connection = connection
        self._codec = codec

    def append(
        self,
        event: EventEnvelope,
        *,
        version_before: int,
        version_after: int,
        previous_event_fingerprint: str | None,
    ) -> int:
        stream = self.read_stream(event.project_id)
        actual_tail = stream[-1].event_fingerprint if stream else None
        if actual_tail != previous_event_fingerprint:
            raise ConcurrentModificationError(
                "project event tail changed concurrently",
                operation="event.append",
                remediation="reload the project stream tail",
                project_id=event.project_id,
            )
        payload, fingerprint = self._codec.encode_event(event)
        try:
            cursor = self._connection.execute(
                "INSERT INTO project_events (event_id, project_id, aggregate_version_before, "
                "aggregate_version_after, is_state_change, event_type, schema_version, "
                "idempotency_key, "
                "event_fingerprint, previous_event_fingerprint, event_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.event_id),
                    str(event.project_id),
                    version_before,
                    version_after,
                    int(version_after != version_before),
                    event.event_type,
                    str(event.schema_version),
                    event.idempotency_key,
                    fingerprint,
                    previous_event_fingerprint,
                    payload,
                    _time(event.recorded_at),
                ),
            )
        except sqlite3.Error as error:
            raise _persistence_error("event.append", error, event.project_id) from error
        position = cursor.lastrowid
        if position is None:
            raise PersistenceError(
                "SQLite did not return an event stream position",
                operation="event.append",
                remediation="rollback the Unit of Work and inspect the SQLite runtime",
                project_id=event.project_id,
            )
        return position

    def read_stream(
        self, project_id: ProjectId, *, after_position: int = 0
    ) -> tuple[StoredEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM project_events WHERE project_id=? ORDER BY stream_position",
            (str(project_id),),
        ).fetchall()
        records = tuple(self._row(row) for row in rows)
        expected_previous = None
        for record in records:
            self._codec.verify_event(record)
            if record.previous_event_fingerprint != expected_previous:
                raise CorruptStoredRecordError(
                    "stored project event chain is broken",
                    operation="event.read",
                    remediation="preserve the database and restore verified evidence",
                    project_id=project_id,
                )
            expected_previous = record.event_fingerprint
        return tuple(record for record in records if record.stream_position > after_position)

    @staticmethod
    def _row(row: sqlite3.Row) -> StoredEvent:
        try:
            return StoredEvent(
                stream_position=row["stream_position"],
                event_id=row["event_id"],
                project_id=row["project_id"],
                aggregate_version_before=row["aggregate_version_before"],
                aggregate_version_after=row["aggregate_version_after"],
                is_state_change=bool(row["is_state_change"]),
                event_type=row["event_type"],
                schema_version=SemanticVersion.parse(row["schema_version"]),
                idempotency_key=row["idempotency_key"],
                event_fingerprint=row["event_fingerprint"],
                previous_event_fingerprint=row["previous_event_fingerprint"],
                event_json=bytes(row["event_json"]),
                recorded_at=datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00")),
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise CorruptStoredRecordError(
                "stored event row is invalid",
                operation="event.read",
                remediation="preserve the database and restore verified evidence",
            ) from error


class SQLiteSnapshotStore:
    def __init__(self, connection: sqlite3.Connection, codec: KernelContractCodec) -> None:
        self._connection = connection
        self._codec = codec

    def get_latest(self, project_id: ProjectId) -> StoredSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM project_snapshots WHERE project_id=? "
            "ORDER BY aggregate_version DESC, created_at DESC, snapshot_id DESC LIMIT 1",
            (str(project_id),),
        ).fetchone()
        if row is None:
            return None
        snapshot = self._row(row)
        self._codec.verify_project(snapshot)
        return snapshot

    def list_for_project(self, project_id: ProjectId) -> tuple[StoredSnapshot, ...]:
        rows = self._connection.execute(
            "SELECT * FROM project_snapshots WHERE project_id=? "
            "ORDER BY aggregate_version DESC, created_at DESC, snapshot_id DESC",
            (str(project_id),),
        ).fetchall()
        snapshots = tuple(self._row(row) for row in rows)
        for snapshot in snapshots:
            self._codec.verify_project(snapshot)
        return snapshots

    def save(self, snapshot: StoredSnapshot) -> None:
        self._codec.verify_project(snapshot)
        try:
            self._connection.execute(
                "INSERT INTO project_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_id,
                    str(snapshot.project_id),
                    snapshot.aggregate_version,
                    snapshot.last_stream_position,
                    snapshot.contract_name,
                    str(snapshot.contract_version),
                    snapshot.schema_fingerprint,
                    snapshot.record_fingerprint,
                    snapshot.content_fingerprint,
                    snapshot.state_json,
                    _time(snapshot.created_at),
                ),
            )
        except sqlite3.Error as error:
            raise _persistence_error("snapshot.save", error, snapshot.project_id) from error

    def delete(self, project_id: ProjectId, snapshot_id: SnapshotId) -> None:
        try:
            cursor = self._connection.execute(
                "DELETE FROM project_snapshots WHERE project_id=? AND snapshot_id=?",
                (str(project_id), snapshot_id),
            )
        except sqlite3.Error as error:
            raise _persistence_error("snapshot.delete", error, project_id) from error
        if cursor.rowcount != 1:
            raise PersistenceError(
                "snapshot not found",
                operation="snapshot.delete",
                remediation="reload snapshot retention evidence",
                project_id=project_id,
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> StoredSnapshot:
        try:
            return StoredSnapshot(
                snapshot_id=row["snapshot_id"],
                project_id=row["project_id"],
                aggregate_version=row["aggregate_version"],
                last_stream_position=row["last_stream_position"],
                contract_name=row["contract_name"],
                contract_version=SemanticVersion.parse(row["contract_version"]),
                schema_fingerprint=row["schema_fingerprint"],
                record_fingerprint=row["record_fingerprint"],
                content_fingerprint=row["content_fingerprint"],
                state_json=bytes(row["state_json"]),
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise CorruptStoredRecordError(
                "stored snapshot row is invalid",
                operation="snapshot.get",
                remediation="preserve the database and restore verified evidence",
            ) from error


class SQLiteIdempotencyStore:
    def __init__(
        self, connection: sqlite3.Connection, codec: KernelContractCodec, clock: Clock
    ) -> None:
        self._connection = connection
        self._codec = codec
        self._clock = clock

    def get(self, operation_name: str, key: str) -> IdempotencyRecord | None:
        row = self._connection.execute(
            "SELECT * FROM idempotency_records WHERE operation_name=? AND idempotency_key=?",
            (operation_name, key),
        ).fetchone()
        if row is None:
            return None
        try:
            record = IdempotencyRecord(
                operation_name=row["operation_name"],
                idempotency_key=row["idempotency_key"],
                project_id=row["project_id"],
                request_fingerprint=row["request_fingerprint"],
                status=IdempotencyStatus(row["status"]),
                result_fingerprint=row["result_fingerprint"],
                result_json=None if row["result_json"] is None else bytes(row["result_json"]),
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
                completed_at=None
                if row["completed_at"] is None
                else datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")),
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise CorruptStoredRecordError(
                "stored idempotency row is invalid",
                operation="idempotency.get",
                remediation="preserve the database and restore verified evidence",
            ) from error
        if record.result_json is not None and record.result_fingerprint is not None:
            self._codec.verify_json_evidence(record.result_json, record.result_fingerprint)
        return record

    def reserve(self, record: IdempotencyRecord) -> None:
        try:
            self._connection.execute(
                "INSERT INTO idempotency_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.operation_name,
                    record.idempotency_key,
                    None if record.project_id is None else str(record.project_id),
                    record.request_fingerprint,
                    record.status.value,
                    record.result_fingerprint,
                    record.result_json,
                    _time(record.created_at),
                    None if record.completed_at is None else _time(record.completed_at),
                ),
            )
        except sqlite3.IntegrityError as error:
            exists = self._connection.execute(
                "SELECT 1 FROM idempotency_records WHERE operation_name=? AND idempotency_key=?",
                (record.operation_name, record.idempotency_key),
            ).fetchone()
            if exists is not None:
                raise IdempotencyConflictError(
                    "idempotency key is already reserved",
                    operation="idempotency.reserve",
                    remediation="load and compare the existing reservation",
                    project_id=record.project_id,
                ) from error
            raise _persistence_error("idempotency.reserve", error, record.project_id) from error
        except sqlite3.Error as error:
            raise _persistence_error("idempotency.reserve", error, record.project_id) from error

    def complete(self, operation_name: str, key: str, result: object) -> None:
        payload = canonicalize_json(result)
        fingerprint = compute_fingerprint(result)
        completed_at = _time(self._clock.now())
        try:
            cursor = self._connection.execute(
                "UPDATE idempotency_records SET status='completed', result_fingerprint=?, "
                "result_json=?, completed_at=? WHERE operation_name=? AND idempotency_key=? "
                "AND status='in_progress'",
                (fingerprint, payload, completed_at, operation_name, key),
            )
        except sqlite3.Error as error:
            raise _persistence_error("idempotency.complete", error) from error
        if cursor.rowcount != 1:
            raise PersistenceError(
                "idempotency reservation is missing or already completed",
                operation="idempotency.complete",
                remediation="reserve the key once in the active transaction",
            )
