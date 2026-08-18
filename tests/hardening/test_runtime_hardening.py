from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

import pytest
from arch_kernel.contracts import (
    ChangeId,
    EffectType,
    EffectValueSource,
    PhaseStatus,
    ProjectId,
    Route,
    SemanticVersion,
    TransitionDefinition,
    TransitionDomain,
    TransitionEffect,
    TransitionId,
    TransitionTarget,
)
from arch_kernel.kernel import InvariantRegistry, TransitionRegistry, compute_fingerprint
from hypothesis import given, settings
from hypothesis import strategies as st

from arch_runtime import (
    ApplyTransitionCommand,
    ConcurrentModificationError,
    CorruptStoredRecordError,
    CreateProjectCommand,
    CreateSnapshotCommand,
    PersistenceError,
    ReplayIntegrityError,
    Runtime,
    RuntimeConfig,
    RuntimeConfigurationError,
)
from arch_runtime.application import SnapshotPolicy
from arch_runtime.persistence.sqlite import (
    SQLiteEventStore,
    SQLiteIdempotencyStore,
    SQLiteProjectRepository,
)
from arch_runtime.replay import SnapshotDisposition
from tests.fakes.adapters import FrozenClock

pytestmark = pytest.mark.sqlite
VERSION = SemanticVersion.parse("1.0.0")


class ProjectEvidence(Protocol):
    record_version: int
    record_fingerprint: str
    content_fingerprint: str


def _transition_definition(
    transition_id: str,
    key: str,
    from_state: PhaseStatus,
    to_state: PhaseStatus,
) -> TransitionDefinition:
    return TransitionDefinition(
        transition_id=TransitionId(transition_id),
        name=key,
        transition_key=key,
        contract_version=VERSION,
        domain=TransitionDomain.PROJECT_LIFECYCLE,
        from_states=(from_state.value,),
        to_state=to_state.value,
        effects=(
            TransitionEffect(
                effect_type=EffectType.SET_FIELD,
                path_template="/lifecycle/status",
                value_source=EffectValueSource.TO_STATE,
            ),
        ),
    )


TRANSITIONS = TransitionRegistry(
    (
        _transition_definition(
            "TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R11",
            "project.cancel",
            PhaseStatus.NOT_STARTED,
            PhaseStatus.CANCELLED,
        ),
        _transition_definition(
            "TRN-01HZX7M3FQ1T2Q9V8Y6K4C2R12",
            "project.restart",
            PhaseStatus.CANCELLED,
            PhaseStatus.NOT_STARTED,
        ),
    )
)


def _runtime(path: Path, clock: FrozenClock, *, initialize: bool = True) -> Runtime:
    return Runtime.open(
        RuntimeConfig(
            path,
            initialize_schema=initialize,
            busy_timeout_ms=2_500,
            clock=clock,
            transition_registry=TRANSITIONS,
            invariant_registry=InvariantRegistry(()),
            snapshot_policy=SnapshotPolicy(every_n_versions=1),
        )
    )


def _create_command(key: str, name: str = "R11 Hardening") -> CreateProjectCommand:
    return CreateProjectCommand.model_validate(
        {
            "idempotency_key": key,
            "name": name,
            "slug": "r11-hardening",
            "summary": "Exercise integration and corruption boundaries.",
            "owner": "platform-team",
            "project_type": "platform",
            "criticality": "high",
            "default_route": Route.STANDARD,
            "objectives": ("Prove fail-closed behavior.",),
            "constraints": ("Preserve authoritative evidence.",),
            "success_criteria": ("Replay exact committed state.",),
            "actor_id": "user:r11",
        },
        strict=True,
    )


def _transition_command(
    project_id: ProjectId,
    evidence: ProjectEvidence,
    *,
    key: str,
    transition_key: str = "project.cancel",
    from_state: PhaseStatus = PhaseStatus.NOT_STARTED,
) -> ApplyTransitionCommand:
    return ApplyTransitionCommand.model_validate(
        {
            "idempotency_key": key,
            "project_id": project_id,
            "request_id": ChangeId.generate(),
            "target": TransitionTarget(
                domain=TransitionDomain.PROJECT_LIFECYCLE,
                entity_id=project_id,
            ),
            "transition_key": transition_key,
            "expected_from_state": from_state.value,
            "expected_record_version": evidence.record_version,
            "expected_record_fingerprint": evidence.record_fingerprint,
            "expected_content_fingerprint": evidence.content_fingerprint,
            "metadata": {"source": "r11"},
            "actor_id": "user:r11",
        },
        strict=True,
    )


def _execute(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def _counts(path: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(path)
    try:
        values = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "project_aggregates",
                "project_events",
                "project_snapshots",
                "idempotency_records",
            )
        )
        return values[0], values[1], values[2], values[3]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("state_json", b"{}"),
        ("schema_fingerprint", "sha256:" + "1" * 64),
        ("record_fingerprint", "sha256:" + "2" * 64),
        ("content_fingerprint", "sha256:" + "3" * 64),
    ],
)
def test_aggregate_corruption_fails_closed(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    column: str,
    value: object,
) -> None:
    database = tmp_path / f"aggregate-{column}.db"
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command(f"r11:aggregate:{column}"))
    _execute(database, f"UPDATE project_aggregates SET {column} = ?", (value,))

    with (
        _runtime(database, frozen_clock, initialize=False) as runtime,
        pytest.raises((CorruptStoredRecordError, ReplayIntegrityError)),
    ):
        runtime.get_project(created.project_id)


@pytest.mark.parametrize(
    ("assignment", "parameters"),
    [
        ("event_json = ?", (b"{}",)),
        ("event_fingerprint = ?", ("sha256:" + "4" * 64,)),
        ("previous_event_fingerprint = ?", ("sha256:" + "5" * 64,)),
        ("aggregate_version_before = 1, aggregate_version_after = 2", ()),
    ],
)
def test_event_corruption_fails_closed(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    assignment: str,
    parameters: tuple[object, ...],
) -> None:
    database = tmp_path / ("event-" + str(abs(hash(assignment))) + ".db")
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command("r11:event:create"))
    _execute(database, f"UPDATE project_events SET {assignment}", parameters)

    with (
        _runtime(database, frozen_clock, initialize=False) as runtime,
        pytest.raises((CorruptStoredRecordError, ReplayIntegrityError)),
    ):
        runtime.replay_project(created.project_id)


def test_missing_authoritative_event_fails_closed(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "missing-event.db"
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command("r11:event:missing"))
    _execute(database, "DELETE FROM project_events")

    with (
        _runtime(database, frozen_clock, initialize=False) as runtime,
        pytest.raises(ReplayIntegrityError),
    ):
        runtime.replay_project(created.project_id)


def test_invalid_snapshot_is_bypassed_without_changing_replay_truth(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "snapshot-corrupt.db"
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command("r11:snapshot:create"))
        loaded = runtime.get_project(created.project_id)
        runtime.create_snapshot(
            CreateSnapshotCommand(
                project_id=created.project_id,
                expected_record_version=loaded.record_version,
                expected_record_fingerprint=loaded.record_fingerprint,
                expected_content_fingerprint=loaded.content_fingerprint,
                expected_stream_position=loaded.stream_position,
                expected_stream_fingerprint=loaded.stream_fingerprint,
                force=True,
            )
        )
        expected = runtime.replay_project(created.project_id)
    _execute(database, "UPDATE project_snapshots SET state_json = ?", (b"{}",))

    with _runtime(database, frozen_clock, initialize=False) as runtime:
        replayed = runtime.replay_project(created.project_id)
    assert replayed.snapshot_disposition is SnapshotDisposition.BYPASSED_INVALID
    assert replayed.record_fingerprint == expected.record_fingerprint
    assert replayed.last_event_fingerprint == expected.last_event_fingerprint


def test_corrupt_idempotency_result_is_never_replayed(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "idempotency-corrupt.db"
    command = _create_command("r11:idempotency:corrupt")
    with _runtime(database, frozen_clock) as runtime:
        runtime.create_project(command)
    _execute(
        database,
        "UPDATE idempotency_records SET result_fingerprint = ?",
        ("sha256:" + "6" * 64,),
    )

    with (
        _runtime(database, frozen_clock, initialize=False) as runtime,
        pytest.raises(CorruptStoredRecordError),
    ):
        runtime.create_project(command)


def test_contradictory_indexed_contract_version_fails_closed(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "unsupported-contract.db"
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command("r11:contract:old"))
    _execute(database, "UPDATE project_aggregates SET contract_version = '0.9.0'")

    with (
        _runtime(database, frozen_clock, initialize=False) as runtime,
        pytest.raises(ReplayIntegrityError),
    ):
        runtime.get_project(created.project_id)


def test_altered_sql_migration_history_blocks_runtime_open(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "migration-history.db"
    with _runtime(database, frozen_clock):
        pass
    _execute(
        database,
        "UPDATE runtime_schema_migrations SET checksum = ? WHERE version = 1",
        ("sha256:" + "7" * 64,),
    )

    with pytest.raises(RuntimeConfigurationError, match="integrity"):
        _runtime(database, frozen_clock, initialize=False)


def test_global_event_positions_may_have_project_local_gaps(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "interleaved.db"
    with _runtime(database, frozen_clock) as runtime:
        first = runtime.create_project(_create_command("r11:gap:a", "Gap Project A"))
        runtime.create_project(_create_command("r11:gap:b", "Gap Project B"))
        loaded = runtime.get_project(first.project_id)
        runtime.apply_transition(
            _transition_command(first.project_id, loaded, key="r11:gap:a:cancel")
        )
        replayed = runtime.replay_project(first.project_id)

    connection = sqlite3.connect(database)
    try:
        positions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT stream_position FROM project_events WHERE project_id = ? ORDER BY 1",
                (str(first.project_id),),
            )
        )
    finally:
        connection.close()
    assert positions == (1, 3)
    assert replayed.record_version == 2


def test_two_runtimes_reject_stale_cas_without_partial_evidence(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "concurrency.db"
    with (
        _runtime(database, frozen_clock) as first,
        _runtime(database, frozen_clock, initialize=False) as second,
    ):
        created = first.create_project(_create_command("r11:concurrency:create"))
        stale = second.get_project(created.project_id)
        first.apply_transition(
            _transition_command(created.project_id, stale, key="r11:concurrency:first")
        )
        with pytest.raises(ConcurrentModificationError):
            second.apply_transition(
                _transition_command(created.project_id, stale, key="r11:concurrency:stale")
            )
    assert _counts(database) == (1, 2, 0, 2)


def test_exact_idempotent_retry_does_not_add_rows(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "idempotent-retry.db"
    command = _create_command("r11:idempotency:exact")
    with _runtime(database, frozen_clock) as runtime:
        first = runtime.create_project(command)
        before = _counts(database)
        second = runtime.create_project(command)
    assert second == first
    assert _counts(database) == before


@pytest.mark.parametrize(
    ("adapter", "method"),
    [
        (SQLiteProjectRepository, "add"),
        (SQLiteEventStore, "append"),
        (SQLiteIdempotencyStore, "complete"),
    ],
)
def test_public_create_rolls_back_at_each_transaction_boundary(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    adapter: type[object],
    method: str,
) -> None:
    database = tmp_path / f"fault-{method}.db"
    runtime = _runtime(database, frozen_clock)

    def fail(*args: object, **kwargs: object) -> None:
        raise PersistenceError(
            "injected transaction failure",
            operation="r11.fault",
            remediation="rollback",
        )

    monkeypatch.setattr(adapter, method, fail)
    with pytest.raises(PersistenceError, match="injected"):
        runtime.create_project(_create_command(f"r11:fault:{method}"))
    runtime.close()
    assert _counts(database) == (0, 0, 0, 0)


@settings(max_examples=12, deadline=None)
@given(st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=24))
def test_command_normalization_fingerprint_is_deterministic(name: str) -> None:
    compact = " ".join(name.split()) or "R11"
    left = _create_command("r11:property:left", compact)
    right = _create_command("r11:property:right", f"  {compact}  ")
    assert compute_fingerprint(left.request_payload()) == compute_fingerprint(
        right.request_payload()
    )


def test_bounded_long_chain_replays_identically_after_reopen(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    database = tmp_path / "long-chain.db"
    with _runtime(database, frozen_clock) as runtime:
        created = runtime.create_project(_create_command("r11:chain:create", "Long Chain"))
        current = runtime.get_project(created.project_id)
        for index in range(8):
            cancelled = index % 2 == 0
            runtime.apply_transition(
                _transition_command(
                    created.project_id,
                    current,
                    key=f"r11:chain:{index}",
                    transition_key="project.cancel" if cancelled else "project.restart",
                    from_state=(PhaseStatus.NOT_STARTED if cancelled else PhaseStatus.CANCELLED),
                )
            )
            current = runtime.get_project(created.project_id)
            if index == 3:
                runtime.create_snapshot(
                    CreateSnapshotCommand(
                        project_id=created.project_id,
                        expected_record_version=current.record_version,
                        expected_record_fingerprint=current.record_fingerprint,
                        expected_content_fingerprint=current.content_fingerprint,
                        expected_stream_position=current.stream_position,
                        expected_stream_fingerprint=current.stream_fingerprint,
                        force=True,
                    )
                )
        expected = runtime.replay_project(created.project_id)

    with _runtime(database, frozen_clock, initialize=False) as reopened:
        actual = reopened.replay_project(created.project_id)
        loaded = reopened.get_project(created.project_id)
    assert actual.reconstructed_state == expected.reconstructed_state
    assert actual.record_fingerprint == expected.record_fingerprint == loaded.record_fingerprint
    assert actual.last_stream_position == 9
