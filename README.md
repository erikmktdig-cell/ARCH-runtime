# ARch Runtime

`arch-runtime` is the application and persistence runtime for ARch. It consumes
the deterministic contracts published by [`arch-kernel`](https://github.com/erikmktdig-cell/ARCH-kernel)
without allowing infrastructure concerns to flow back into the kernel.

## Status

R01 establishes packaging, dependency, architecture, test, build, and CI
boundaries only. Persistence behavior, repositories, units of work, replay,
migrations, APIs, CLIs, and user interfaces are intentionally not implemented.

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

The only public root symbol in R01 is `arch_runtime.__version__`.

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

The future idempotency contract distinguishes completed logical outcomes from
infrastructure failures: success and canonical logical rejection may be
persisted; infrastructure failure must not complete an idempotency reservation.
For the future event store, `previous_event_fingerprint` always refers to the
previous event in the same project stream, never a global stream tail.

R02 defines immutable storage evidence, repository protocols, Unit of Work,
Clock, and structured errors. Its in-memory implementations exist under tests
only as adapter contract fixtures.

R03 adds the SQLite foundation: validated connection configuration, mandatory
pragmas, explicit transaction primitives, immutable numbered SQL migrations,
approved checksums, and fail-closed schema compatibility inspection.

There are currently no production repository adapters, SQLite Unit of Work,
application services, Runtime API, replay behavior, HTTP API, CLI,
authentication, or UI.
