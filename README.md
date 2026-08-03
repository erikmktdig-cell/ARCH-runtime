# ARch Runtime

`arch-runtime` is the application and persistence runtime for ARch. It consumes
the deterministic contracts published by [`arch-kernel`](https://github.com/erikmktdig-cell/ARCH-kernel)
without allowing infrastructure concerns to flow back into the kernel.

## Status

R05 provides the first synchronous application vertical slice. `create_project`
normalizes and fingerprints commands, runs K08 validation, commits aggregate,
creation event, and idempotency evidence atomically, then reloads the aggregate
to verify persisted fingerprints. Replay, transition services, APIs, CLIs, and
user interfaces remain intentionally unimplemented.

The dependency direction is fixed:

```text
arch-runtime -> arch-kernel
arch-kernel  -X-> arch-runtime
```

## Development

Python 3.12 and 3.13 are supported. The lock resolves `arch-kernel` from the
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

The package root remains limited to `arch_runtime.__version__`. Infrastructure
adapters are imported explicitly from `arch_runtime.persistence.sqlite`.

## Frozen Boundaries

Future work must preserve these dependencies:

```text
application        -> arch_kernel, ports
persistence        -> ports
persistence.sqlite -> persistence, ports
replay             -> arch_kernel, ports
migrations         -> arch_kernel, ports
ports              -> public arch_kernel contracts and typing only
package root       -> metadata only
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

There are currently no apply-transition service, functional Runtime API, replay
behavior, automatic snapshots, stored-contract migration execution, HTTP API,
CLI, authentication, or UI.
