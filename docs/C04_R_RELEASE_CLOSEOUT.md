# C04-R Release Verification

## Scope

Release the approved C02 implementation (`af19867518cae87fc3c1a8b229f49c22a2c9b87a`)
as arch-runtime 0.2.0. Runtime production changes are limited to package version
metadata. No Kernel, Web, SQL schema, workflow, replay, migration, or application
semantics are redesigned by this closeout.

The dependency contract is `arch-kernel>=0.2.0,<0.3.0`. Development and CI use the
published Kernel wheel, not a source checkout. The approved Kernel release commit
is `4aa6a3acddd1b0ef761fb4dd0cfff720706ff5df`; its wheel SHA-256 is
`3c581c37c4890ac50ff8c1722aca7cc4e29fed85a37656d6f08e22ffb8847b5c`.

## Installed Evidence

`scripts/verify_installed.py` downloads the released Kernel wheel and verifies its
digest before installing it alongside the candidate Runtime wheel or sdist in a
new environment outside both repositories. The process removes environment import
overrides and executes Python in isolated mode. The smoke checks both distribution
versions and verifies both imports originate inside that environment's site-packages.
The printed local paths are diagnostic evidence, not embedded artifact metadata.

The public facade exercises creation, explicit workflow initialization, states
`a -> b -> c`, exact retries, conflicting retries, stale aggregate version/fingerprint,
stale workflow version/content fingerprint, full replay, a snapshot at state `b`,
snapshot plus transition tail replay to `c`, recovery no-op, and reopening.
An independent disposable database copy omits snapshots and fully replays the same
four-event history; its final state and both fingerprints must match snapshot-tail replay.

The historical fixture is representative test input constructed in a new disposable
database. It models ProjectState 1.0.0 using public contracts and registration APIs.
Direct SQLite writes are confined to fixture preparation. All actual migration
planning, application, retries, and post-migration replay use public Runtime methods.
Assertions verify that opening and dry-run write nothing, migration produces an empty
workflow registry, and the original creation event remains byte-for-byte unchanged.
This is not a claim of an exhaustive historical-database inventory.

Existing C02 tests retain corruption, event chain, initialization fault injection,
workflow authority, replay, snapshot, recovery, property, and CAS regression coverage.

## Release Gates

CI retains all seven protected-main check names. Every Windows/Ubuntu Python 3.12/3.13
matrix job runs the entire suite, including fresh wheel and sdist installations and
the released-Kernel vertical slice. The build job additionally checks the standalone
wheel installation. Ruff, strict mypy, archive inspection, Twine, reproducibility,
and security remain blocking. No skips, xfails, or coverage exclusions are introduced.

Archive inspection verifies version and dependency metadata, rejects generated state,
checks archive content for secrets and personal paths, and rejects source dependency
leakage. Twelve release regressions cover the published Kernel hash, portable metadata,
wrong versions/dependencies, generated files, and secret leakage.

Security includes the hash-pinned requirements audit and an additional advisory query
for every registry version in the lock, irrespective of platform markers. The latter
does not replace lock integrity validation or installation verification.

## Final Evidence Authority

The external release manifest and Notion closeout record the exact final commit,
actual test count/coverage, hosted run identities, artifact hashes, governance, and
independent publication verification. This source document describes the reproducible
method; it does not predeclare unexecuted gates as passing. Final distributions must
be rebuilt after the final commit, and any later commit invalidates them.

## Residual Boundaries

- No automatic stored-contract migration, repair, or workflow inference.
- Historical events remain immutable; snapshots are not historical authority.
- No SLSA, signing, SBOM, or attestation claim is made.
- GitHub Releases availability is needed to reproduce dependency downloads.
- Web 0.2.0, C04-W, and acceptance tracks remain outside this release.
