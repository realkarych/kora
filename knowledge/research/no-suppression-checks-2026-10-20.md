# No-suppression checks

Checked: 2026-10-20, for [issue #155](https://github.com/realkarych/kora/issues/155), against `master` at `6b784b7` with Ruff 0.17.1, Pyright 1.1.414, pytest 9.1.1, ESLint 10.13.0 and typescript-eslint 8.71.2 from the lock files. This record carries the rule families selected to enforce the "no suppression" rules of [AGENTS.md](../../AGENTS.md#code), every configuration allowance with its reason, the probes that show what each tool catches and misses, and the residuals that stay review items. The decisions are the owner's approval of the implementation plan on the issue; nothing here is a decision. The [logging and scanner record](logging-stack-and-ci-scanners-2026-10-20.md) carries the `S` family and the scanners; this record is the current list of every Ruff allowance.

## Ruff rules evaluated

| Rule or family | Decision | Reason |
|---|---|---|
| `BLE001` | selected, three pinned allowances | the only rule for `except Exception` or `except BaseException` without a re-raise; a handler that re-raises (`kora.db.connection._unit`, `kora.db.migrations.upgrade_database`) does not fire |
| `B017`, `E722`, `S101`, `S102`, `S110`, `S112`, `S307` | already selected | `pytest.raises(Exception)`, bare `except`, `assert` in `src/`, `exec`, `try`/`except`/`pass` and `continue`, `eval` |
| `PT` family | selected | `PT011` too-broad `raises` without `match` (Ruff's default list of types), `PT015` `assert False`, `PT009` and `PT027` unittest-style assertions, plus the style rules; 13 findings on the tree were fixed in the code (ten composite assertions split, two `raises` blocks reduced to one awaited helper, one `match` added) |
| `PGH003`, `PGH004` | selected as `PGH` | blanket `# type: ignore` and blanket `# noqa`; a specific code passes them, so they complement `--ignore-noqa` and the repository test |
| `RUF100` | selected | unused `noqa`; redundant once `noqa` is banned, kept so Ruff itself reports it |
| `ERA001` | selected as `ERA` | commented-out code; cannot fire on compliant code |
| `T201`, `T203` | selected as `T20` | `print` and `pprint`; CLI results go through `typer.echo`, diagnostics through the logging bootstrap |
| `TID251` | selected as `TID`, with the banned-API table below | the only Ruff mechanism that bans APIs by qualified name; it resolves aliases (`from pytest import mark`, `import warnings as w`, `from unittest import mock`), `pytestmark = [...]`, `pytest.param(marks=...)` and `pytest.skip.Exception`; `TID252` (parent-relative imports) does not fire on the tree |
| `D` family | not selected | pydocstyle requires docstrings, the opposite of the rule; Ruff has no rule that forbids a docstring or a bare string statement; the repository test covers both |
| `PLC0415` | not selected | import outside top level; fires on the deliberate deferred imports of `kora db upgrade` and is unrelated to suppression |
| `FIX`, `TD` | not selected | TODO comments are comments and already banned |

`ruff check` runs with `--ignore-noqa` in CI and in the development instructions, so a `# noqa` comment suppresses nothing even before the repository test reports it.

## Banned APIs

| Qualified name | Rule of AGENTS.md |
|---|---|
| `pytest.skip`, `pytest.xfail`, `pytest.importorskip`, `pytest.mark.skip`, `pytest.mark.skipif`, `pytest.mark.xfail` | Testing: no `skip` or `xfail` |
| `pytest.mark.filterwarnings`, `warnings.filterwarnings`, `warnings.simplefilter`, `warnings.catch_warnings`, `warnings.resetwarnings`, `warnings.filters` | Testing: warnings are errors (`filterwarnings = ["error"]`) |
| `pytest.MonkeyPatch`, `unittest.mock`, `mock`, `pytest_mock` | Testing: no mocks, no `monkeypatch` |
| `_pytest` | Testing: pytest internals (`_pytest.outcomes.skip`) reach the same outcomes under another name |
| `unittest.SkipTest`, `unittest.skip`, `unittest.skipIf`, `unittest.skipUnless`, `unittest.expectedFailure` | Testing: pytest honours the unittest skip and expected-failure forms |
| `contextlib.suppress` | Code: fail fast; a blind handler without an `except` keyword, invisible to `BLE001` |
| `os._exit` | Testing: a test process that exits with status 0 before pytest reports hides every failure |

## Allowances

Every `per-file-ignores` entry, with the reason the [security profile](../../.claude/skills/kora-verify/references/security.md) requires. Ruff's finest configuration unit is the file; `tests/architecture/test_no_suppression.py` pins each allowance to the construct that justifies it, so the allowance cannot grow silently.

| File | Rule | Pinned to | Reason |
|---|---|---|---|
| `hatch_build.py` | `S603` | — | subprocess calls with fixed argument lists, `pnpm` resolved through `shutil.which` ([logging and scanner record](logging-stack-and-ci-scanners-2026-10-20.md#ruff-rule-changes)) |
| `tests/**` | `S101`, `S603` | — | assertions are the test mechanism; the tests start `sys.executable` |
| `src/kora/__main__.py` | `BLE001` | exactly one blind handler | [observability.md section 5](../specs/observability.md#5-safe-content-and-trust-boundaries): an exception that reaches the composition root is one CRITICAL `process.failed` record and the process exits 70 |
| `src/kora/observability/bootstrap.py` | `BLE001` | exactly two blind handlers, both on `Exception` | sections 5 and [6](../specs/observability.md#6-bounded-output-and-failure-behavior): a policy or renderer failure becomes a fixed loss counter (`policy_failure`), never a fallback representation of the input; `Exception` rather than `BaseException`, so cancellation propagates |
| `src/kora/observability/sink.py` | `BLE001` | exactly one blind handler | section 6: the sole writer thread marks the sink failed, closes the queue and never blocks producers or prevents process exit |
| `tests/support/warning_filters.py` | `TID251` | exactly the names `warnings.catch_warnings`, `warnings.filterwarnings`, `warnings.simplefilter` | `expression_index_warnings_ignored()`: SQLAlchemy's SQLite dialect cannot reflect an index built on an expression (`ix_graph_namespace_name` uses `coalesce(namespace_id, '')`) and warns once per table, and the migration and durable-event tests compare the reflectable schema; `every_warning_raised()`: the redaction test makes a planted warning fire so the standard-library bridge can be tested |

The two `except Exception` handlers that the holder coroutines of `tests/db/test_transactions.py` and `tests/observability/test_db_diagnostics_follow_the_commit.py` carried were removed, not allowed: with the real fixture on both databases, `Database.close` cutting a held transaction raises nothing (the holder commits), so the handlers never ran and a future exception should fail the test. `test_close_after_the_deadline_fails_the_unit_and_releases` therefore asserts nothing about failure; renaming or strengthening it is outside this change.

## Probes

Each row was reproduced on a scratch copy of the repository configuration on 2026-10-20.

| Construct | Ruff | Pyright | pytest | Repository test |
|---|---|---|---|---|
| `import os  # noqa: F401` (used, specific) | passes without `--ignore-noqa`; `F401` with it | — | — | tool directive |
| `# ruff: noqa: F401` (file level, specific) | passes without `--ignore-noqa`; `F401` with it; blanket `# ruff: noqa` is `PGH004` | — | — | tool directive |
| `# type: ignore[return-value]` on a wrong return | `PGH003` only for the blanket form | with `enableTypeIgnoreComments = false` the error is reported | — | tool directive |
| `# pyright: ignore[reportReturnType]` | — | still suppresses (no switch exists) | — | tool directive |
| `# pyright: basic` at the top of a file | — | still lowers the mode (no switch exists) | — | tool directive |
| `warnings.catch_warnings(action="ignore")` | `TID251` | — | the warning is swallowed and the test passes | warning filter (`action` keyword outside the helper) |
| `raise unittest.SkipTest("x")` | `TID251` | — | reported as skipped | — |
| `item.add_marker("skip")` in `pytest_collection_modifyitems` | nothing (a string, not a name) | — | the test is skipped | string marker, unlisted hook |
| `config.addinivalue_line("filterwarnings", "ignore")` in `pytest_configure` | nothing | — | a warning passes under `filterwarnings = ["error"]` | runtime configuration change, unlisted hook |
| a root `pytest.ini` with `filterwarnings = ignore` | — | — | replaces `[tool.pytest.ini_options]` entirely; a warning test passes | shadows `pyproject.toml` |
| a nested `ruff.toml` with `select = []` | silences every rule for its subtree | — | — | shadows `pyproject.toml` |
| a root `pyrightconfig.json` with `typeCheckingMode = "basic"` | — | takes precedence over `[tool.pyright]`; zero diagnostics | — | shadows `pyproject.toml` |
| `PYTEST_ADDOPTS="-p no:warnings"` in the environment | — | — | warnings pass | not detected; CI configuration under owner review |

## Repository test

`tests/architecture/test_no_suppression.py` walks the repository from its root, skipping dotted directories, `__pycache__`, `node_modules`, `measurements`, `dist`, `build`, `web/dist`, `web/build`, `web/node_modules` and `src/kora/api/static`, and the listed generated files (`uv.lock`, `web/pnpm-lock.yaml`; the test asserts they exist, so the list cannot rot; the generated TypeScript client enters it, and the ESLint `ignores`, in the change that generates it). Python and `.pyi` files go through `tokenize` (every comment token except a line-1 shebang is a comment or, when it matches the directive patterns, a tool directive; string contents never match) and `ast` (every bare string statement, `monkeypatch` and `mocker` parameters, `add_marker`/`applymarker` with a string constant, `addinivalue_line`, `catch_warnings` with an `action` keyword outside the helper, `__import__`/`import_module`/`resolve_name` of `pytest`, `_pytest`, `warnings` or `unittest`, blind handlers beyond the pinned counts, `conftest.py` hooks beyond `pytest_report_header` and `pytest_generate_tests` in `tests/conftest.py` and none in `tests/observability/conftest.py`, and the module variables `collect_ignore`, `collect_ignore_glob` and `pytest_plugins`). Shell (`.sh`, `.bash`, `.zsh`, `.ksh` and extensionless files with a shell shebang), SQL (`.sql`, `.psql`, `.pgsql`) and the Alembic template (`.mako`, where `#`, `##` and `<%doc>` all count) report every comment after quoted strings are removed; TypeScript and JavaScript (`.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`) report directives only, because ESLint owns their comment ban. The configuration check compares the whole `[tool.ruff]`, `[tool.pyright]` and `[tool.pytest.ini_options]` tables with recorded mappings, states the invariants in words (every `filterwarnings` entry is `error`, `--strict-config` and `--strict-markers` present, `ignore` empty, `typeCheckingMode` strict, `enableTypeIgnoreComments` false), refuses `pytest.ini`, `.pytest.ini`, `tox.ini`, `setup.cfg` and `pyrightconfig.json` at the root, `ruff.toml`, `.ruff.toml` and a second `pyproject.toml` anywhere, and `.eslintrc*` under `web/`, and requires `compilerOptions.strict = true` in `web/tsconfig.app.json` and `web/tsconfig.node.json`. The tree check runs in well under a second.

Directive patterns, case-insensitive and whitespace-tolerant: `noqa`, `type: ignore`, `pyright:` (every Pyright comment), `pragma:`, `fmt:`, `ruff:`, `isort:`, `mypy:`, `pylint:`, `flake8:`, `coverage:`, `nosec`, `eslint-disable`, `eslint-enable`, `eslint-env`, `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, `@ts-check`, `prettier-ignore`, `biome-ignore`.

## ESLint

`web/eslint.config.mjs` defines the plugin object `kora` with the rule `no-comments`, which reports every comment token except a shebang and applies to every linted file (`eslint.config.mjs` and `vite.config.ts` included); `globalIgnores(["dist/"])` keeps a local build output out of the lint; `linterOptions.noInlineConfig = true` makes every inline `eslint-disable` comment ineffective and reported as a warning, which `--max-warnings 0` turns into a failure; `linterOptions.reportUnusedDisableDirectives = "error"` is inert while `noInlineConfig` is on and is the layer that remains if it is ever turned off; `@typescript-eslint/ban-ts-comment` bans `@ts-check`, `@ts-expect-error`, `@ts-ignore` and `@ts-nocheck` (the strict preset allows `@ts-expect-error` with a description). Probe on the installed ESLint, a scratch `src/Probe.tsx` with a plain comment, an `eslint-disable-next-line` and a `@ts-expect-error`: five errors (three `kora/no-comments`, one `no-unused-vars` the directive had tried to hide, one `ban-ts-comment`) and one `noInlineConfig` warning, exit 1; the tree passes. No dependency was added: `@eslint-community/eslint-plugin-eslint-comments` would duplicate what the local rule and `noInlineConfig` give.

## Residuals

| Form | Status |
|---|---|
| names built from strings (`getattr(pytest, "sk" + "ip")`) | not detected; visible in review |
| `PYTEST_ADDOPTS`, `PYTHONWARNINGS` and the step commands of `.github/workflows/ci.yml` | not machine-checked; CI configuration under the explicit owner review that [kora-verify](../../.claude/skills/kora-verify/SKILL.md) requires |
| the ESLint `ignores` list | not machine-checked; review item |
| a directive word inside a TypeScript string, regex literal or JSX text | the scanner may report a false positive to look at, never a false negative |
| code placed in a dotted directory | not scanned by the repository test; not importable as part of the package, and Ruff also excludes dotted directories by default |
| `.pyw`, `.pyx` | not scanned; not importable on the supported platforms without a compiler |
| deleting `tests/architecture/test_no_suppression.py` | visible only in the diff, as for the other architecture tests |
| `logging.disable` | not a check bypass (diagnostics only); not banned |

## Limits of this record

The probes show what the pinned tool versions catch on 2026-10-20; a Ruff, Pyright, pytest or ESLint upgrade can change a row and is checked by rerunning `tests/architecture` and the probes. A green check is not proof that every rule of AGENTS.md is met; the rules apply to every form, including the ones above.
