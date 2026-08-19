# ARch Runtime

`arch-runtime` is the application and persistence runtime for ARch. It consumes
the deterministic contracts published by [`arch-kernel`](https://github.com/erikmktdig-cell/ARCH-kernel)
without allowing infrastructure concerns to flow back into the kernel.

## Version 0.1.0

Python 3.12 and 3.13 are supported. Version 0.1.0 exposes the approved runtime capabilities
through one stable synchronous Python facade.
`Runtime.open()` validates SQLite compatibility and composes project creation, transition,
replay, snapshots, recovery, and stored-contract migration. `create_project`
persists a validated aggregate and creation event. `apply_transition` loads and
verifies that aggregate, evaluates the transition through K08, rechecks optimistic
preconditions inside the write transaction, and atomically persists the projected
state, transition event, and idempotency result. `replay_project` reconstructs the
aggregate from authoritative events and verifies it against operational storage.
`create_snapshot` checkpoints only replay-verified committed state under a deterministic
version policy. `recover_aggregate` can replace divergent materialized state only after
the immutable project stream verifies and an optimistic CAS succeeds.
`dry_run_stored_contract_migration` plans explicit K10 routes without writes;
`apply_stored_contract_migration` updates materialized aggregates and snapshots under
CAS, preserves historical event rows, and appends a verified migration projection event.
HTTP APIs, CLIs, and user interfaces remain intentionally unimplemented.

```python
from arch_runtime import Runtime, RuntimeConfig

with Runtime.open(RuntimeConfig("arch.db", initialize_schema=True)) as runtime:
    result = runtime.create_project(command)
    project = runtime.get_project(result.project_id)
```

Schema initialization is explicit and applies only to an empty database. Opening an older,
newer, or altered schema fails closed; `Runtime.open()` never upgrades it implicitly.
SQL schema migration, stored-contract migration, and aggregate recovery are explicit operator
actions. Normal open and read operations never migrate, recover, or repair stored evidence.

The dependency direction is fixed:

```text
arch-runtime -> arch-kernel
arch-kernel  -X-> arch-runtime
```

## Development

The lock resolves `arch-kernel` from the
published `v0.1.0` Git tag while the built package retains the public compatible
range `arch-kernel>=0.1.0,<0.2.0`.

```console
uv sync --frozen --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
uv build
uv run twine check dist/*
```

The package root exposes the stable facade, immutable public commands/results, and structured
runtime errors. Infrastructure adapters remain available only from
`arch_runtime.persistence.sqlite` and never appear in facade method signatures.

## Frozen Boundaries

Future work must preserve these dependencies:

```text
application        -> arch_kernel, ports
persistence        -> ports
persistence.sqlite -> persistence, ports
replay             -> arch_kernel, ports
migrations         -> arch_kernel, ports
ports              -> public arch_kernel contracts and typing only
package root       -> public contracts, errors, Runtime facade
```

The idempotency contract distinguishes completed logical outcomes from
infrastructure failures: success and canonical logical rejection are persisted;
infrastructure failure does not complete an idempotency reservation. In the
event store, `previous_event_fingerprint` always refers to the
previous event in the same project stream, never a global stream tail.

R02 defines immutable storage evidence, repository protocols, Unit of Work,
Clock, and structured errors. Its in-memory implementations exist under tests
only as adapter contract fixtures.

R03 adds the SQLite foundation: validated connection configuration, mandatory
pragmas, explicit transaction primitives, immutable numbered SQL migrations,
approved checksums, and fail-closed schema compatibility inspection.

R04 adds four SQLite repository adapters and `SQLiteUnitOfWork`. Reads verify
canonical bytes and stored fingerprints; project writes use version-and-full-
fingerprint CAS; event chains are scoped to each project; and every adapter
shares one explicit transaction without committing independently.

R05 adds immutable create-project commands/results, injectable identity and UoW
ports, and a synchronous `CreateProjectService`. The persisted event type is
`project.aggregate.created`, satisfying the kernel's three-segment event grammar;
its payload identifies the conceptual convention as `project.created`.

R06 adds immutable transition commands/results and `ApplyTransitionService`.
Logical rejections persist only canonical idempotency evidence. Successful no-op
or test-only projections preserve aggregate version, timestamps, fingerprints,
and event stream. Actual changes use version-and-full-fingerprint CAS, append
`project.aggregate.transition_applied`, and reload persisted evidence after commit.
The envelope `event_type` is authoritative for routing and future replay;
`payload.event_name` remains descriptive only.

R07 adds immutable replay results and `ReplayService`. Replay validates canonical
event envelopes, project identity, stream order, fingerprint chain, version continuity,
persisted before/after evidence, K08 projection evidence, and the final operational
aggregate. Transition patches are reapplied through K05 at their persisted evaluation
time. Snapshots may skip prior patch application only after matching stream evidence;
invalid snapshots produce a visible finding and fall back to full replay when possible.
Replay performs no writes and never repairs, truncates, or rewrites evidence.

R08 adds deterministic `SnapshotPolicy` and `SnapshotService`. Snapshots contain canonical
state at an exact project stream position, are idempotent per aggregate version, and use
deterministic configurable retention. Replay selects the newest verifiable snapshot and
falls back to full history when none is valid. `RecoveryService` is the only recovery path:
it replays verified history, rechecks aggregate and stream evidence inside one Unit of Work,
uses CAS to replace divergent materialized state, and appends the non-state-changing
`project.aggregate.recovered` audit event. Correct aggregates produce an idempotent no-op;
invalid event streams prohibit recovery.

R09 integrates public K10 contract and migration registries with persisted evidence. Planning
verifies canonical bytes, schemas, fingerprints, aggregate preconditions, and the project-scoped
event chain without writing. Explicit application rechecks that evidence inside one Unit of Work,
migrates the aggregate and snapshots with CAS, and appends
`project.aggregate.contract_migrated` before replaying the complete projected history in the same
transaction. Historical event rows remain byte-for-byte immutable; their current canonical K10
projections live in the append-only audit event and are verified before replay. Logical migration
rejections are exactly idempotent, while infrastructure failures roll back every surface.

R10 adds immutable `RuntimeConfig`, verified `GetProjectResult`, tail fingerprint evidence on
`ReplayResult`, and the synchronous `Runtime` facade. Each operation delegates to the approved
R05-R09 service and creates its own SQLite Unit of Work; the facade retains no connection or
transaction. Lifecycle is explicit and context-manager safe, empty-schema initialization is
opt-in, normal reads perform full replay verification, and multiple instances may safely use the
same file under existing optimistic concurrency rules.

There is no automatic recovery during reads, automatic stored-contract migration, HTTP API,
CLI, authentication, or UI.
