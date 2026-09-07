"""Representative v1 test fixture; all migration operations use the public facade.

SQLite writes below prepare historical input only. They are never a migration or
recovery implementation, and never operate on a user database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from arch_kernel.contracts import (
    ContractRegistration,
    ContractStatus,
    ProjectState,
    Route,
    SemanticVersion,
    VersionedRecord,
)
from arch_kernel.kernel import (
    ContractRegistry,
    InvariantRegistry,
    MigrationRegistry,
    build_builtin_contract_registry,
    build_project_state_v1_to_v2_migration,
    canonicalize_json,
    compute_content_fingerprint,
    compute_fingerprint,
    generate_contract_schema,
)
from pydantic import Field, create_model

from arch_runtime import (
    ApplyStoredContractMigrationCommand,
    CreateProjectCommand,
    PlanStoredContractMigrationCommand,
    Runtime,
    RuntimeConfig,
)


def historical_registry() -> tuple[ContractRegistry, MigrationRegistry, ContractRegistration]:
    fields: dict[str, Any] = {}
    for name, item in ProjectState.model_fields.items():
        if name == "workflow_registry":
            continue
        if item.is_required():
            default: object = ...
        elif item.default_factory is not None:
            default = Field(default_factory=item.default_factory)
        else:
            default = item.default
        fields[name] = (item.annotation, default)
    model = create_model("ProjectStateV1", __base__=VersionedRecord, **fields)
    builtins = build_builtin_contract_registry()
    current = builtins.get_by_model(ProjectState)
    assert current is not None
    descriptor = current.descriptor.model_copy(
        update={
            "version": SemanticVersion.parse("1.0.0"),
            "status": ContractStatus.DEPRECATED,
            "schema_id": "https://schemas.arch.local/arch.project.project_state/1.0.0",
            "schema_filename": "contracts/arch.project.project_state.1.0.0.schema.json",
            "python_module": model.__module__,
            "python_qualname": model.__qualname__,
            "model_fingerprint": compute_fingerprint(
                {
                    "module": model.__module__,
                    "qualname": model.__qualname__,
                }
            ),
            "replaces": None,
            "schema_fingerprint": "sha256:" + "0" * 64,
        }
    )
    schema = generate_contract_schema(ContractRegistration(descriptor, model))
    historical = ContractRegistration(
        descriptor.model_copy(
            update={
                "schema_fingerprint": schema.schema_fingerprint,
            }
        ),
        model,
    )
    contracts = ContractRegistry((*builtins.registrations, historical))
    return (
        contracts,
        MigrationRegistry((build_project_state_v1_to_v2_migration(),), contracts),
        historical,
    )


def migration_slice(database: Path) -> dict[str, object]:
    assert not database.exists(), "fixture must use a new disposable database"
    contracts, migrations, historical = historical_registry()
    config = RuntimeConfig(
        database,
        initialize_schema=True,
        contract_registry=contracts,
        migration_registry=migrations,
        invariant_registry=InvariantRegistry(()),
    )
    with Runtime.open(config) as runtime:
        created = runtime.create_project(
            CreateProjectCommand(
                idempotency_key="historical:create",
                name="Historical compatibility",
                slug="historical-compatibility",
                owner="release-team",
                project_type="platform",
                criticality="low",
                default_route=Route.QUICK,
                actor_id="release-check",
            )
        )
    project_id = created.project_id
    # Construct a reviewed representative v1 fixture with matching original evidence.
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT state_json FROM project_aggregates").fetchone()
        state = json.loads(row[0])
        state.pop("workflow_registry")
        state["contract_version"] = "1.0.0"
        state["schema_uri"] = "urn:arch:contracts:project_state:1.0.0"
        canonical = canonicalize_json(state)
        record = compute_fingerprint(state)
        content = compute_content_fingerprint(state)
        schema = historical.descriptor.schema_fingerprint
        connection.execute(
            "UPDATE project_aggregates SET contract_version=?, schema_fingerprint=?, "
            "record_fingerprint=?, content_fingerprint=?, state_json=?",
            ("1.0.0", schema, record, content, canonical),
        )
        event = json.loads(
            connection.execute("SELECT event_json FROM project_events").fetchone()[0]
        )
        event["payload"].update(
            state=state,
            record_fingerprint=record,
            content_fingerprint=content,
            contract_version="1.0.0",
            schema_fingerprint=schema,
        )
        original = canonicalize_json(event)
        tail = compute_fingerprint(event)
        connection.execute(
            "UPDATE project_events SET event_json=?, event_fingerprint=?", (original, tail)
        )
        connection.commit()
        before = tuple(connection.iterdump())
        with Runtime.open(config):
            pass
        assert tuple(connection.iterdump()) == before, "open silently changed stored contracts"
        plan = PlanStoredContractMigrationCommand(
            project_id=project_id,
            target_project_version=SemanticVersion.parse("2.0.0"),
            target_event_version=SemanticVersion.parse("1.0.0"),
            expected_record_version=1,
            expected_record_fingerprint=record,
            expected_content_fingerprint=content,
            expected_stream_position=1,
            expected_stream_fingerprint=tail,
        )
        with Runtime.open(config) as runtime:
            dry_run = runtime.plan_project_contract_migration(plan)
            assert dry_run.success
            assert dry_run.changed
            assert tuple(connection.iterdump()) == before, "dry-run wrote evidence"
            assert dry_run.projected_state is not None
            assert dry_run.projected_state.workflow_registry == {}
            command = ApplyStoredContractMigrationCommand(
                idempotency_key="historical:migrate",
                plan=plan,
                expected_plan_fingerprint=dry_run.plan_fingerprint,
                reason="Explicit representative v1 to v2 compatibility test.",
                actor_id="release-check",
            )
            result = runtime.migrate_project_contracts(command)
            assert result.success
            assert runtime.migrate_project_contracts(command) == result
            replay = runtime.replay_project(project_id)
            assert str(replay.reconstructed_state.contract_version) == "2.0.0"
            assert replay.reconstructed_state.workflow_registry == {}
            assert runtime.get_project(project_id).state == replay.reconstructed_state
        assert (
            connection.execute(
                "SELECT event_json FROM project_events WHERE stream_position=1"
            ).fetchone()[0]
            == original
        ), "historical event was rewritten"
        assert connection.execute("SELECT COUNT(*) FROM project_events").fetchone()[0] == 2
    finally:
        connection.close()
    return {
        "project_state": "1.0.0 -> 2.0.0",
        "workflow_registry": {},
        "open_no_migration": "PASS",
        "dry_run_no_writes": "PASS",
        "history_immutable": "PASS",
        "post_migration_replay": "PASS",
    }
