# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Independent `arch-runtime` repository and typed package scaffold.
- Enforced runtime-to-kernel dependency direction and layer boundaries.
- Static quality, architecture, distribution, and isolated-install gates.
- Ubuntu and Windows CI matrix for Python 3.12 and 3.13.
- Immutable stored-project, event, snapshot, and idempotency evidence contracts.
- Repository, event, snapshot, idempotency, Unit of Work, and Clock protocols.
- Structured runtime error taxonomy and reusable in-memory adapter contract tests.
- Secure SQLite connection setup with verified pragmas and explicit transactions.
- Immutable SQL schema migrations, approved checksums, compatibility inspection,
  and the complete R00 table/index foundation.
- Canonical SQLite adapters for projects, events, snapshots, and idempotency evidence.
- `SQLiteUnitOfWork` with schema validation, `BEGIN IMMEDIATE`, explicit commit,
  rollback by default, version-and-fingerprint CAS, and project-scoped event chains.
- Strict normalized `CreateProjectCommand` and canonical `CreateProjectResult` contracts.
- Synchronous `create_project` vertical slice with K08 validation, injectable IDs/time,
  exact idempotency replay, atomic aggregate/event/result persistence, and post-commit checks.
- Immutable `ApplyTransitionCommand` and canonical `ApplyTransitionResult` contracts.
- Synchronous `apply_transition` vertical slice with K08 transition evaluation, in-transaction
  CAS revalidation, explicit no-op handling, project-scoped event chaining, exact replay, and
  atomic aggregate/event/idempotency persistence.
- Shared application helpers for canonical idempotency replay, project evidence verification,
  and authoritative K04 project event envelopes.
- Immutable `ReplayResult`, structured snapshot findings, and synchronous read-only replay.
- Full reconstruction from authoritative creation and transition events using persisted K05
  patches and evaluation times, with per-step chain/version/fingerprint verification.
- Valid snapshot optimization, visible invalid-snapshot fallback, corruption fixtures, and
  Hypothesis properties for version continuity and event fingerprint integrity.
- Deterministic version-based snapshot policy, explicit canonical checkpoint creation,
  idempotent per-version writes, configurable retention, and newest-valid selection.
- Explicit materialized-aggregate recovery from verified event history with CAS, exact
  idempotency, an append-only `project.aggregate.recovered` audit event, and fail-closed
  handling of invalid streams and snapshots.
