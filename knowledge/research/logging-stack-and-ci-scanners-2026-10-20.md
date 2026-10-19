# Logging stack and CI scanners

Checked: 2026-10-20, for [issue #147](https://github.com/realkarych/kora/issues/147), against `master` at `0a8f4d4` with the package-layers branch of [#143](https://github.com/realkarych/kora/pull/143) merged. This record carries the versions, the selection evidence and the first local measurements of the logging bootstrap and the CI scanners. The contract is [structured component diagnostics](../specs/observability.md); the decisions are the owner's approval of the implementation plan on the issue. Nothing here is a decision.

## Runtime and development dependencies

| Package | Version | Released | Role | Transitive packages |
|---|---|---|---|---|
| `structlog` | 26.1.0 | 2026-06-06 | context variables, the filtering bound logger and the processor chain behind `kora.observability.bootstrap` ([ADR-0013](../../docs/decisions/0013-implementation-stack.md)) | none on Python 3.11 and later |
| `typer` | 0.27.3 | 2026-10-06 | the `kora` command root ([ADR-0013](../../docs/decisions/0013-implementation-stack.md)) | `rich` 15.0.0, `shellingham` 1.5.4, `annotated-doc` 0.0.5, `markdown-it-py` 4.2.0, `mdurl` 0.1.2, `pygments` 2.21.0 (`rich`'s Markdown and highlighting support, dormant with `rich_markup_mode=None`), and `colorama` on Windows only, which kora does not support |
| `pytest-asyncio` | 1.4.0 | 2026-05-26 | the asynchronous context tests, in strict mode with function-scoped loops | `typing-extensions` 4.16.0 below Python 3.13 (also a `pyright` dependency) |

Typer 0.27 vendors its own Click fork under `typer._click`; no `click` distribution is installed. Two consequences shaped the composition root `kora.__main__`: the public `typer.TyperException` is a base class of the vendored `ClickException`, so usage errors can be caught without importing the private module, and `Typer.__call__` installs Typer's own `sys.excepthook`, so the root runs the command through `typer.main.get_command`, `make_context` and `invoke` instead, with `pretty_exceptions_enable=False`. The vendored Click prints usage errors through `sys.stderr`; the root formats them itself and writes them with `os.write`, so that a full stderr pipe cannot block interpreter finalization.

The `structlog` changelog between 25.1.0 and 26.1.0 (checked 2026-10-20) adds `Self` return types on the binding methods, `is_enabled_for` and `get_effective_level` on the bound logger, exception-group support in `structlog.tracebacks`, Python 3.14 and 3.15 support and drops 3.8 and 3.9; the context-variable helpers are unchanged since 22.3.0. kora uses `structlog.contextvars.bind_contextvars`, `reset_contextvars`, `get_contextvars` and `merge_contextvars`, `structlog.make_filtering_bound_logger` and `structlog.wrap_logger`, and renders with the standard library `json` module.

## Ruff rule changes

The `flake8-bandit` family `S` is selected project-wide. Two configuration entries accompany it, both recorded here as the [security profile](../../.claude/skills/kora-verify/references/security.md) requires:

- `S603` is allowed in `hatch_build.py` and under `tests/`, the only places that start subprocesses today. It fires on every `subprocess` call regardless of its arguments; these calls pass fixed argument lists (`hatch_build.py` resolves `pnpm` through `shutil.which`, which also satisfies `S607`; the tests start `sys.executable`). The rule stays live under `src/`, where a future subprocess site built from user-influenced data must be reviewed.
- `S101` (the `assert` statement) is allowed under `tests/`, where assertions are the test mechanism.

## Scanners

| Scanner | Candidate alternatives | Chosen release | Pinned commit | What it covers | What it does not cover |
|---|---|---|---|---|---|
| CodeQL, advanced setup through `github/codeql-action` | Semgrep Community Edition: `semgrep scan --config auto` fetches its rule set from the registry at run time, so the rules are not pinned, the run needs the network and sends metrics by default; pinning would mean vendoring rule files | `v4.39.0`, released 2026-10-15 | `6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee` | the default suite for `python`, `javascript-typescript` and `actions`, with `build-mode: none` and the `paths-ignore` list of `.github/codeql/config.yml` (generated and vendored trees); alerts upload to the code-scanning view, available without a license on a public repository | a finding does not fail the job; merge blocking needs the ruleset rule "Require code scanning results" |
| OSV-Scanner reusable workflows | `pip-audit` 2.10.1 (released 2026-06-10): Python environments only, no `pnpm` lockfile | `v2.7.0`, released 2026-10-12 | `b1d267aea5211fa648d9d442fa64b90dbdcbdd36` | known advisories in `uv.lock` and `web/pnpm-lock.yaml`, both listed as supported lockfiles; on a pull request only the vulnerabilities the pull request introduces, on `master` and weekly the whole set; results upload as SARIF | crates bundled in native wheels; the scan sends package names and versions to osv.dev at run time |
| Gitleaks through `gitleaks/gitleaks-action` | TruffleHog (AGPL-3.0, verifies live credentials against providers); GitHub secret scanning alone (provider patterns only) | action `v3.0.0`, released 2026-05-30, scanner `8.30.1` (released 2026-03-21) through `GITLEAKS_VERSION` | `e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e` | regex patterns over the first 30 first-parent, non-merge commits of a pull request, a pushed range on `master`, and the whole history on the weekly schedule and on `workflow_dispatch` | on a pull request the action reads at most the first 30 commits of the pull request through the API and scans them with `--first-parent --no-merges`, so a longer pull request or one with merge commits is scanned partially; the action is distributed under the Gitleaks end-user licence (the scanner itself is MIT) and downloads the scanner binary from GitHub releases at run time without a digest check; a personal-account repository needs no license key, an organization-owned one does; the weekly full-history scan and the full OSV scan run from the default branch only, so they activate after the change lands |

Workflow permissions: CodeQL `contents: read`, `actions: read`, `security-events: write`; the OSV-Scanner caller grants what the reusable workflows declare (`actions: read`, `contents: read`, `security-events: write`); Gitleaks `contents: read` and `pull-requests: read`, with pull-request comments disabled. Fork pull requests run with a read-only token; their scanner behaviour is unverified until the first fork pull request.

Repository settings observed through the GitHub API on 2026-10-20: secret scanning enabled, push protection enabled, non-provider patterns and validity checks disabled, Dependabot alerts disabled (`GET /repos/realkarych/kora/vulnerability-alerts` returns 404), code-scanning default setup not configured, `sha_pinning_required` false, branch protection on `master` requiring only the four documentation jobs (Markdown lint, Links and anchors, Mermaid diagrams, Example graph documents) with no required reviews. The owner actions that follow from the issue plan (Dependabot alerts, required status checks for the scaffold lanes, the templates job and the three scanner jobs, the code-scanning rule, enforced full-length pins) are recorded in the pull request until done.

The baseline row "No dedicated jobs" for dependency, secret and static-analysis scanning in the [threat-model structure research](threat-model-document-structure-2026-10-18.md#repository-baseline-and-evidence) is superseded by this record.

## Measurements

`tests/measurements/test_logging_overhead.py` declares its workloads and criteria before running and keeps every sample in the artifact `logging-overhead-measurement`. The local run of the final harness on 2026-10-20 (macOS 15 on Apple silicon, CPython 3.11.17, a laptop, not an uncontended runner) gave the figures below. The hosted Linux runner is about 2.5 to 3 times slower per record (the first CI run measured 92 µs for the INFO workload against 34 µs locally with the same harness); its figures from the pull request's final revision are the baseline for any future throughput budget, which is not set in this change.

| Workload | Median µs per record | Note |
|---|---|---|
| disabled level, 16 fields | 0.8 | the filtering bound logger returns before any processor |
| INFO, 16 string fields | 21 | policy walk, redaction scan, JSON rendering, enqueue |
| DEBUG, 16 string fields | 21 | |
| text renderer, 16 string fields | 25 | |
| maximal record under the default 16 KiB limit (12 strings of 1024 bytes and an exception chain) | 97 | |
| maximal record under the 1 MiB limit | 93 | |
| adversarial nested mapping, rejected by the value budget | 122 | the walk stops at 256 values, before redaction |
| 64 KiB string, rejected by the string budget | 5 | the length check runs before encoding |
| maximal registry (256 values, 65536 bytes) against maximal records, no match | 96 | one compiled alternation per string |
| maximal registry against maximal records, many matches | 107 | |
| 50 registrations rotated 100 times with fresh values | 512 per registration | each round recompiles the alternation and purges the `re` cache; see below |
| bridged standard-library record | 11 | no message formatting |
| bind, emit and reset with all eleven context keys | 19 | |
| four producer threads against a saturated queue | 28 | queue records and bytes never exceeded the configured limits |
| writer throughput with a fast reader (50,000 records) | 22 per produced record | the single-process Python producer outpaces the writer and reader threads under the interpreter lock, so a burst of this size drops records; the writer batches queued records up to 64 KiB per `write` |

The control-character escape moved from a per-character Python loop to one compiled pattern substitution, which brought the INFO workload from 34 µs to 21 µs locally.

Lifecycle and process figures from the same run: idle shutdown 0.1 ms; the real entry point writing into a full pipe reports a shutdown of 1.01 s against the 1 s drain deadline and exits 1.1 s after start; 200 configure-and-shutdown cycles in one process grow the traced heap by 79 KiB in the first hundred cycles and 6 KiB in the second hundred, within the gate (the second half may not exceed the first half or 64 KiB, and the total stays under 1 MiB), and return threads and descriptors to their pre-cycle counts; resident memory is read from `/proc/self/statm` and is unavailable on macOS; `python -m kora --version` cold start about 70 ms. Two gates failed in earlier local runs for harness reasons and were corrected in the harness: the drain wait did not wake when the writer emptied the queue, which the bootstrap now does, and the counts were taken after the measurement's own reader thread had been closed.

The step's declared wall-clock budget is five minutes in the Linux 3.11 lane. The local run of the final harness took 77 s with 50,000 records per repeat and three repeats for the per-record workloads, two for the maximal and redaction shapes. The issue plan's first sizing of 100,000 records and five repeats took 525 s locally before the drain fix; the first CI run of the reduced sizing took 458 s against the two-minute budget declared then, 204 s of which went to the rotation workload (50 registrations rotated 1,000 times, dominated by recompiling the alternation and purging the `re` cache after every registration). The rotation workload now runs 100 rounds with fresh values per round, which keeps the recompilation cost visible without dominating the step, and the budget was set to five minutes so that the whole step fits on the hosted runner with the measured slowdown and the pipeline stays under its ten-minute limit.

## Limits of this record

The figures above come from one laptop run; the hosted runner's measurement artifact, the whole-pipeline duration with the three scanner workflows, cold and warm, and the CodeQL analysis time are recorded in the pull request from its final revision, since a run of the final revision cannot exist before that revision is pushed. Scanners report patterns and known advisories; a clean run is not absence of vulnerabilities. The console script inside a built wheel is not tested, since the installed-wheel tests were removed under [issue #112](https://github.com/realkarych/kora/issues/112).
