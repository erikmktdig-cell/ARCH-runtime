# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-09-07

### Added

- Generic workflow authority through an explicit immutable `WorkflowDefinitionRegistry`,
  `InitializeWorkflowCommand`, `InitializeWorkflowResult`, and
  `Runtime.initialize_workflow()`.
- Atomic workflow initialization with canonical event history, exact idempotency,
  aggregate CAS, replay, snapshot, recovery, tamper, and SQLite coverage.
- Generic workflow transitions through the existing `Runtime.apply_transition()` API with
  configured definition binding and workflow-record optimistic preconditions.
- Explicit ProjectState 1.0.0 to 2.0.0 stored-contract migration using the existing K10
  planning/application path, preserving historical event rows and producing an empty
  workflow registry until governed initialization.

### Changed

- Require `arch-kernel>=0.2.0,<0.3.0`, resolved from the verified published v0.2.0 wheel.
- C04-R release hardening verifies installed workflow, migration, replay and snapshot
  behavior outside source checkouts on the supported Python/OS matrix.
- New projects now persist ProjectState 2.0.0 with an explicit workflow registry.
- Runtime replay and stored-contract migration projection understand authoritative generic
  workflow initialization without changing phase-status semantics.

## [0.1.0] - 2026-08-18

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
- Explicit K10 stored-contract migration planning and application with canonical evidence
  verification, aggregate and snapshot CAS, exact idempotency, transactional post-migration
  replay, and the append-only `project.aggregate.contract_migrated` audit event.
- Immutable historical event rows with verified current-contract projections carried by the
  migration audit event, plus fake and SQLite integration, rollback, and property tests.
- Stable synchronous `Runtime` facade and immutable `RuntimeConfig` composing every approved
  R05-R09 use case without exposing SQLite adapters or retaining shared transactions.
- Verified `GetProjectResult`, public event-tail fingerprint evidence, explicit empty-schema
  initialization, idempotent lifecycle closure, and real-file multi-instance SQLite E2E tests.
- R11 integration and corruption hardening across aggregate, event, snapshot, idempotency,
  schema-history, concurrency, transaction rollback, deterministic replay, and bounded
  long-chain behavior through the public `Runtime` facade.
