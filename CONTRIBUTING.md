# Contributing

Use Python 3.12 or 3.13 and install the frozen development environment with
`uv sync --frozen --all-groups`.

Changes must preserve the dependency direction from `arch-runtime` to
`arch-kernel`, keep infrastructure out of `ports`, and pass all commands listed
in the README. R01 namespace modules are boundary markers, not permission to
introduce persistence behavior before its work package is authorized.

Import kernel contracts only from documented public `arch_kernel` paths.
Architecture tests must fail closed and identify the source file and import that
crosses a frozen boundary. Before opening a change, run Ruff lint and format,
mypy strict, the complete pytest suite, `uv build`, and `twine check dist/*`.

SQLite imports remain confined to `arch_runtime.persistence.sqlite`. Do not add
repository behavior, SQLite Unit of Work, application commands, or runtime
singletons until the corresponding work package is approved.

Do not commit generated environments, caches, build output, credentials, or
vulnerability reports.
