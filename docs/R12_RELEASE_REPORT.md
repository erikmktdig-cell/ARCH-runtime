# R12 Release Report

## R11 Baseline

- Commit: `a66b746df5df9703e5ce9f840029fcf4a0434362`.
- Tests: 199 PASS.
- Branch coverage: 91.62%.
- Production LOC changed in R11: 0.

## R12 Changes

- Pinned every GitHub Action to an immutable commit SHA.
- Preserved the Ubuntu/Windows and Python 3.12/3.13 hosted test matrix.
- Added a connected `pip-audit` job and monthly Dependabot checks.
- Added package project URLs and finalized the 0.1.0 changelog entry.
- Clarified Python support, fail-closed reads, and explicit migration/recovery in README.
- Hardened `.gitignore` against local databases, environment files, and SQLite sidecars.
- Added installed create/get/close/reopen smoke coverage using only public imports.
- Froze the R10 public API through an explicit export regression test.
- Added fail-closed source/archive inspection, deterministic manifest generation, and
  byte-reproducible wheel/sdist validation.
- Added `pip-audit` only to the development group; runtime dependencies remain unchanged.

## Release Blockers and Fixes

Four release-tooling defects were found and fixed:

1. `check_reproducible_build.py` could not load `scripts.release_tools` when executed by path.
   The script now bootstraps the repository root explicitly, with a subprocess regression test.
2. The reproducibility report initially included uv's output-directory `.gitignore` helper.
   The inventory now accepts exactly one wheel and one sdist.
3. The tracked-source scanner initially matched its own Windows personal-path signature.
   The signature is now assembled without embedding a forbidden literal, with a regression test.
4. `build_release_manifest.py` could not load `scripts.release_tools` when executed by path.
   It now uses the same explicit repository bootstrap, covered by the path-execution regression.

Current remote blocker:

- `erikmktdig-cell/ARCH-runtime` does not yet exist in the connected GitHub installation.
- The available web session is not authenticated and `gh` is not installed.
- Hosted CI, branch governance, Private Vulnerability Reporting, tag, and GitHub Release are
  therefore **PENDING** and must not be marked PASS.

## Local Quality Evidence

- Full suite: 205 PASS.
- Branch coverage: 91.62%.
- Ruff check: PASS.
- Ruff format check: PASS after the final formatting gate.
- mypy strict: PASS across source, tests, and release scripts.
- Architecture suite: PASS as part of the full suite.
- Release suite: 14 PASS as part of the full suite.
- `git diff --check`: PASS.
- Wheel and sdist build: PASS.
- Twine metadata/readme rendering: PASS.
- Artifact inspection: PASS.
- Reproducible wheel: PASS.
- Reproducible sdist: PASS.

## Clean Install and Compatibility

- Windows / Python 3.12.13 clean wheel install: PASS.
- Windows / Python 3.13.14 clean wheel install: PASS.
- Windows / Python 3.12.13 clean sdist build and install: PASS.
- Installed smoke: `Runtime.open`, create, get, close, reopen, and get: PASS.
- Linux / Python 3.12: PENDING hosted CI.
- Linux / Python 3.13: PENDING hosted CI.
- Windows hosted CI / Python 3.12: PENDING hosted CI.
- Windows hosted CI / Python 3.13: PENDING hosted CI.

## Dependency Audit

- Runtime dependency range: `arch-kernel>=0.1.0,<0.2.0`.
- Development lock: coherent; `uv lock --check` is a required CI gate.
- Connected advisory audit of all indexed locked dependencies: PASS, no known
  vulnerabilities found.
- `arch-kernel` is intentionally resolved from immutable commit
  `08b9c9bd57d90ee12a6b40e350431ca1c3bb8bc0`. It is excluded from the PyPI
  requirements audit because a Git source is not a hash-addressed index artifact; its source
  identity is checked separately by the lock.
- No new runtime dependency was introduced.

## Security Audit

- Tracked-source high-confidence token/private-key/credential URL scan: PASS.
- Distribution forbidden-file and local-path inspection: PASS.
- No tracked `.env`, database, SQLite sidecar, private key, cache, or coverage artifact: PASS.
- Workflow token permissions: `contents: read` only.
- Checkout credentials are not persisted.
- Required workflow steps do not use `continue-on-error` or `|| true`.
- Private Vulnerability Reporting: PENDING; `SECURITY.md` correctly keeps release blocked until
  activation is verified.

## Artifact Evidence

The deterministic pre-commit build produced:

- `arch_runtime-0.1.0-py3-none-any.whl`:
  `sha256:1c184001ca076a8574a04d37e80ed507c0a81f2dd63913d9e03a67e002b79e82`
- `arch_runtime-0.1.0.tar.gz`:
  `sha256:5afaca6c2c045dc076839d28b4441a96f5e92ad422e65e5a2dd7aaaab18f12fe`

Final artifacts must be rebuilt from the exact release commit. The external release manifest is
generated only after that commit exists, because a versioned file cannot contain the SHA of the
same commit that contains it without changing that SHA. `scripts/build_release_manifest.py`
records the exact commit, tag, CI matrix, hashes, tests, and coverage without circular evidence.

## GitHub Governance

- Remote URL: `https://github.com/erikmktdig-cell/ARCH-runtime` (target; not yet created).
- Default branch: PENDING.
- Required pull request policy: PENDING.
- Required CI checks: PENDING.
- Force-push/deletion protection: PENDING.
- Private Vulnerability Reporting: PENDING.

## Release State

- Release commit: PENDING creation after final local review.
- Hosted CI: PENDING.
- Tag `v0.1.0`: NOT CREATED.
- Push: NOT PERFORMED.
- GitHub Release: NOT CREATED.
- Current state: **WP-R12 IMPLEMENTATION COMPLETE / RELEASE BLOCKED BY REMOTE GOVERNANCE**.

No R12 evidence above should be interpreted as architectural approval; final review remains with
the project owner.
