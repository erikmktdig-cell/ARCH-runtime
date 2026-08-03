import json
import sqlite3

import pytest
from arch_kernel.contracts import EventEnvelope, ProjectId, ProjectState

from arch_runtime.errors import CorruptStoredRecordError
from arch_runtime.persistence.sqlite import migrate_schema
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from arch_runtime.persistence.sqlite.repositories import SQLiteEventStore, SQLiteProjectRepository
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite


def _alternate_event(event: EventEnvelope) -> EventEnvelope:
    value = event.model_dump(mode="json")
    project_id = "PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1Z"
    value.update(
        event_id="EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1Z",
        project_id=project_id,
        idempotency_key="project/phase/activation:002",
    )
    value["aggregate"]["target_id"] = project_id
    return EventEnvelope.model_validate_json(json.dumps(value), strict=True)


def test_event_tail_is_project_scoped_with_interleaved_global_positions(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    event_envelope: EventEnvelope,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    codec = KernelContractCodec()
    store = SQLiteEventStore(sqlite_connection, codec)
    sqlite_connection.execute("BEGIN IMMEDIATE")
    for project_id in (event_envelope.project_id, ProjectId("PRJ-01HZX7M3FQ1T2Q9V8Y6K4C2B1Z")):
        sqlite_connection.execute(
            "INSERT INTO project_aggregates VALUES (?,1,'project_state','1.0.0',?,?,?,X'7B7D',?,?)",
            (
                str(project_id),
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
                "2026-07-23T12:00:00Z",
                "2026-07-23T12:00:00Z",
            ),
        )
    first_position = store.append(
        event_envelope, version_before=1, version_after=2, previous_event_fingerprint=None
    )
    other_position = store.append(
        _alternate_event(event_envelope),
        version_before=1,
        version_after=2,
        previous_event_fingerprint=None,
    )
    first = store.read_stream(event_envelope.project_id)[0]
    follow_up = event_envelope.model_copy(update={"event_id": "EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1Y"})
    final_position = store.append(
        follow_up,
        version_before=2,
        version_after=2,
        previous_event_fingerprint=first.event_fingerprint,
    )
    assert (first_position, other_position, final_position) == (1, 2, 3)


def test_project_read_rejects_tampered_payload(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    project_state: ProjectState,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    repository = SQLiteProjectRepository(sqlite_connection, KernelContractCodec())
    sqlite_connection.execute("BEGIN IMMEDIATE")
    repository.add(project_state)
    sqlite_connection.execute(
        "UPDATE project_aggregates SET state_json=? WHERE project_id=?",
        (b'{"altered":true}', str(project_state.metadata.project_id)),
    )
    with pytest.raises(CorruptStoredRecordError):
        repository.get(project_state.metadata.project_id)


def test_event_read_rejects_broken_project_chain(
    sqlite_connection: sqlite3.Connection,
    frozen_clock: FrozenClock,
    event_envelope: EventEnvelope,
) -> None:
    migrate_schema(sqlite_connection, clock=frozen_clock)
    sqlite_connection.execute("BEGIN IMMEDIATE")
    sqlite_connection.execute(
        "INSERT INTO project_aggregates VALUES (?,1,'project_state','1.0.0',?,?,?,X'7B7D',?,?)",
        (
            str(event_envelope.project_id),
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            "2026-07-23T12:00:00Z",
            "2026-07-23T12:00:00Z",
        ),
    )
    store = SQLiteEventStore(sqlite_connection, KernelContractCodec())
    store.append(event_envelope, version_before=1, version_after=2, previous_event_fingerprint=None)
    sqlite_connection.execute(
        "UPDATE project_events SET previous_event_fingerprint=? WHERE event_id=?",
        ("sha256:" + "d" * 64, str(event_envelope.event_id)),
    )
    with pytest.raises(CorruptStoredRecordError, match="chain"):
        store.read_stream(event_envelope.project_id)
