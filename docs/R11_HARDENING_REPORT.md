# R11 Hardening Report

## Baseline

- Baseline commit: `5c0f235b42601578326386994eddd8af3d2f118b` (R10).
- Baseline suite: 178 tests passing.
- Baseline branch coverage: 91.36%.
- R11 scope: integration, corruption, property, concurrency, and fault hardening only.
- Production LOC changed: 0.

## Invariants Audited

- The append-only project event stream remains the replay authority.
- Materialized aggregates must match verified replay evidence.
- Project-local fingerprint chains are continuous even when global stream positions interleave.
- Aggregate writes use version and record-fingerprint CAS.
- Exact idempotency retries return the original canonical result without new rows.
- A reused key with stale or contradictory evidence cannot partially commit.
- Invalid snapshots are optimization failures and fall back to full replay.
- Corrupt aggregate, event, idempotency, or schema evidence fails closed.
- Public mutations preserve aggregate, event, snapshot, and idempotency atomicity.
- Close and reopen preserve canonical state, fingerprints, versions, and event tails.

## Corruption Matrix

| Surface | Injected corruption | Expected behavior | Result |
| --- | --- | --- | --- |
| Aggregate | Invalid canonical bytes | Read/replay fails closed | PASS |
| Aggregate | Schema fingerprint mismatch | Read/replay fails closed | PASS |
| Aggregate | Record fingerprint mismatch | Read/replay fails closed | PASS |
| Aggregate | Content fingerprint mismatch | Read/replay fails closed | PASS |
| Aggregate | Indexed contract version contradicts payload | Replay integrity failure | PASS |
| Event | Invalid envelope bytes | Replay fails closed | PASS |
| Event | Event fingerprint mismatch | Chain/replay fails closed | PASS |
| Event | Previous fingerprint mismatch | Chain/replay fails closed | PASS |
| Event | Aggregate version discontinuity | Replay fails closed | PASS |
| Event | Authoritative creation event deleted | Replay fails closed | PASS |
| Snapshot | Invalid state bytes | Snapshot bypassed; full replay succeeds | PASS |
| Idempotency | Result fingerprint mismatch | Exact retry fails closed | PASS |
| SQL migration history | Approved checksum altered | `Runtime.open()` refuses database | PASS |
| Global positions | Another project creates a numeric gap | Project replay remains valid | PASS |

Historical K10 no-route, migrated projection, event ordering, snapshot-position, and altered
schema fixtures remain covered by the R09, replay, and SQLite migration suites. R11 exercises
their composition through the R10 facade where a public operation exists.

## Property Tests

- Canonical command normalization yields an identical request fingerprint for equivalent input.
- Existing Hypothesis suites continue to cover replay version continuity and event fingerprint
  integrity.
- Existing migration properties preserve dry-run/write equivalence and transactional rollback.
- Existing snapshot properties preserve deterministic policy and newest-valid selection.
- Exact idempotency replay was rechecked against real SQLite row counts.

## Concurrency Matrix

| Scenario | Expected behavior | Result |
| --- | --- | --- |
| Two `Runtime` instances, stale version/fingerprint | `ConcurrentModificationError` | PASS |
| Stale mutation reservation | Rolled back with no result evidence | PASS |
| Exact same key and request | Exact result, no additional rows | PASS |
| Same key and different request | Conflict (existing R05/R06 coverage) | PASS |
| Interleaved projects in one global event table | Independent project chains | PASS |

## Fault Injection

Public `create_project` was faulted at `project.add`, `event.append`, and
`idempotency.complete`. Every injected persistence exception left all four storage surfaces at
their pre-transaction row counts. Existing R06-R09 suites continue to inject failures across
transition, recovery, snapshot, and migration commit boundaries.

## Bounded Stress and Determinism

- Eight alternating lifecycle transitions produced a nine-event project stream.
- A midpoint snapshot was created and verified.
- Replay before and after closing/reopening the runtime produced identical state and
  fingerprints.
- Stream ordering is by global `stream_position`; numeric gaps are valid when projects
  interleave, while project-local fingerprint discontinuities are corruption.
- No wall-clock performance claim is made by this functional stress fixture.

## Defects Found and Fixed

- Production defects found: none.
- Production fixes required: none.
- Test defect fixed during implementation: the generated normalization fixture initially
  derived an invalid slug from intentionally padded names. The slug was made independent of
  the observed normalization field.

No prior defect was waived or converted to a skip, xfail, warning, or coverage exclusion.

## Remaining Risks

- Remote Ubuntu and Windows CI evidence remains pending under remote governance.
- The bounded chain test proves integrity and deterministic reconstruction, not throughput or
  latency targets.
- SQLite process-level contention and crash/power-loss durability require environment-specific
  operational testing beyond deterministic unit and integration fault injection.
- No automatic repair is provided; corrupt authoritative evidence remains an operator-visible,
  fail-closed condition by design.

## Quality Gates

- Full suite: 199 PASS.
- Branch coverage: 91.62% (baseline 91.36%, net +0.26 percentage points).
- R11-specific suite: 21 PASS.
- Ruff: PASS.
- mypy strict: PASS.
- Build wheel and sdist: PASS.
- Twine metadata: PASS.
- Architecture suite: PASS.
- Scope audit: PASS; no R12 release/governance implementation added.

## Final Status

- WP-R11 IMPLEMENTATION: COMPLETE
- WP-R11 REVIEW: PENDING
- WP-R12: BLOCKED pending R11 approval
- Remote governance: PENDING IN PARALLEL
