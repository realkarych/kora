# Development

Secrets and redaction amendment dated 2026-10-23: the measurement text for `StreamMasker` and the per-form bound under [issue #192](https://github.com/realkarych/kora/issues/192); wording pending line-by-line owner review.

Diagnostic value measurement amendment dated 2026-10-23: reviewed and accepted by the project owner under [issue #219](https://github.com/realkarych/kora/issues/219) on 2026-10-23.

Browser verification amendment dated 2026-10-22: the project owner approved the [benchmark removal and matching documentation](https://github.com/realkarych/kora/issues/179#issuecomment-6329273349), with delegated independent review and merge.

Scope and implementation plan approved by the project owner under [issue #86](https://github.com/realkarych/kora/issues/86) on 2026-10-18; wording pending line-by-line owner review.

CEL evaluator amendment dated 2026-10-21: infrastructure and verification added under [issue #166](https://github.com/realkarych/kora/issues/166); wording accepted by the project owner on 2026-10-22.

CEL cost-regression amendment dated 2026-10-23: the project owner approved lightweight validation and a 100 ms target under [issue #167](https://github.com/realkarych/kora/issues/167#issuecomment-6351849696); wording reviewed line by line and approved by the project owner on 2026-10-23.

Packaging-test removal amendment dated 2026-10-19: removal decided by the project owner under [issue #112](https://github.com/realkarych/kora/issues/112); wording pending line-by-line owner review.

Import-layer test amendment dated 2026-10-19: the pytest source check added under [issue #104](https://github.com/realkarych/kora/issues/104); wording pending line-by-line owner review.

Logging and scanner amendment dated 2026-10-20: the measurement step, the Ruff `S` family and the scanner commands added under [issue #147](https://github.com/realkarych/kora/issues/147); wording pending line-by-line owner review.

Provider-loading amendment dated 2026-10-21: the provider loading measurement added under [issue #150](https://github.com/realkarych/kora/issues/150); wording pending line-by-line owner review.

Metadata database amendment dated 2026-10-20: the `postgres` extra, wheel-only installation, the database test selection, the PostgreSQL container, `kora db upgrade` and the database measurement added under [issue #153](https://github.com/realkarych/kora/issues/153); wording pending line-by-line owner review.

CEL binding amendment dated 2026-10-21: the `cel` extra, the `wcwidth` constraint, the binding's crate lock in the OSV scans and the local scan command added under [issue #165](https://github.com/realkarych/kora/issues/165); wording pending line-by-line owner review.

No-suppression amendment dated 2026-10-20: `--ignore-noqa`, the Ruff families and banned-API table, the ESLint comment rule and the no-suppression architecture test added under [issue #155](https://github.com/realkarych/kora/issues/155); wording pending line-by-line owner review.

The repository contains an importable typed Python package with the logging bootstrap, the metadata database package with its migrations, the `kora` entry point (`kora --version`, `kora db upgrade`), a minimal React shell and distribution builds. The execution engine, HTTP API, the other CLI commands and full web console are subsequent implementation work. Read [AGENTS.md](../AGENTS.md) before contributing and [ADR-0013](decisions/0013-implementation-stack.md) for the stack decision.

## Tool setup

Use Linux or macOS, Git, Bash, ripgrep and the pinned uv, Python, Node and pnpm toolchain. The default development interpreter is recorded in [`.python-version`](../.python-version); Node is recorded in [`web/.node-version`](../web/.node-version), pnpm in [`web/package.json`](../web/package.json), and Python tools in [`pyproject.toml`](../pyproject.toml) and [`uv.lock`](../uv.lock). The [dated research](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md) records exact versions and selection evidence.

Install the pinned uv with its versioned installer, following the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/) to make it available in your shell:

```sh
curl -LsSf https://astral.sh/uv/0.13.1/install.sh | sh
```

Install the Node release recorded in the version file using a version manager or the official binary. With Node available, install the pinned pnpm:

```sh
npm install --global pnpm@12.14.0
uv python install
uv --version
node --version
pnpm --version
```

Run the following commands from the repository root in the same shell. Dependency setup may access package registries; retain the uv cache, pnpm store and temporary build constraints for the subsequent offline distribution build. The setup commands preserve both lockfiles:

```sh
uv sync --locked --no-build --group build --extra postgres --extra cel --no-install-project
KORA_BUILD_CONSTRAINT_DIR="$(mktemp -d)"
uv export --locked --only-group build --no-emit-project \
  --format requirements.txt \
  --output-file "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
uv pip install --python "$PWD/.venv/bin/python" \
  --target "$KORA_BUILD_CONSTRAINT_DIR/backend" \
  --require-hashes \
  --requirements "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
uv pip install --python "$PWD/.venv/bin/python" --no-deps \
  --build-constraints "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt" \
  --editable .
pnpm --dir web install --frozen-lockfile
```

The disposable target primes registry metadata for the isolated build resolver using the locked build requirements and their hashes. Locked sync installs cached artifacts without necessarily populating that metadata, so it is insufficient on a fresh cache. The target is separate from the project environment and is not used by the build itself.

## Source checks and distributions

Run every source check, then build the sdist and build a wheel from that sdist under isolated, hash-constrained build requirements:

```sh
uv lock --check
uv run --no-sync ruff format --check .
uv run --no-sync ruff check --ignore-noqa .
uv run --no-sync pyright
uv run --no-sync pytest -q
pnpm --dir web run typecheck
pnpm --dir web run lint
pnpm --dir web run build

KORA_BUILD_OFFLINE=1 uv build --offline \
  --python "$PWD/.venv/bin/python" \
  --build-constraints "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt" \
  --require-hashes --clear

uv sync --locked --no-build --group build --extra postgres --extra cel
git diff --exit-code -- pyproject.toml uv.lock web/package.json \
  web/pnpm-lock.yaml web/pnpm-workspace.yaml
```

`--no-build` refuses to compile a dependency from source, so a platform or interpreter without a published wheel for a native package fails at setup instead of building on the machine; `--extra postgres` installs the PostgreSQL driver on every platform, because the dialect is registered whether or not a PostgreSQL test runs. `--extra cel` installs the `common-expression-language` binding, which the CEL tests load; it has no source build in kora's setup, so setup fails on a platform without its wheel, such as Python 3.15 on macOS. The lock constrains `wcwidth`, which the binding's command-line dependencies import, to its last pure-Python release. An install outside kora's lock, such as `pip install kora[cel]`, never gets that constraint, and gets the wheel-only rule only with `--only-binary common-expression-language`. kora loads the binding only when the files of its `cel` package, bytecode caches excepted, match its RECORD and the package contains no symbolic links, so an install in uv's symbolic-link mode is refused. `test_cel_bundled_crate_lock_covers_installed_wheel` in `tests/cel_eval/test_cel_bundled_crates.py` knows the wheels of the CI platforms and Linux aarch64 only, and fails with the unknown wheel tag elsewhere.

The default test run needs a reachable Docker daemon: `tests/conftest.py` runs every database test on SQLite and on PostgreSQL, and the PostgreSQL parameter starts one container per session from the image pinned by digest in `tests/support/databases.py`, migrates a template database once and clones it for each test. `KORA_TEST_DATABASES` selects the parameters as a comma-separated subset of `sqlite` and `postgresql`; the selection is printed in the pytest header, an unknown name is a usage error, and a selected PostgreSQL without a daemon is an error, never a skip. The macOS lanes and a machine without Docker run:

```sh
KORA_TEST_DATABASES=sqlite uv run --no-sync pytest -q
```

The container runs without the reaper container that `testcontainers` starts by default, because the session fixture removes the database container itself on every exit path, readiness failure included; the fixture disables the reaper through the library's configuration when `TESTCONTAINERS_RYUK_DISABLED` is unset, CI sets the variable explicitly, and an explicit `false` re-enables the reaper, which mounts the Docker socket. Readiness is checked from the host with a content-free deadline, never through the library's `psql` probe, so no error carries the password. The container publishes a random port on every network interface of the host for the duration of the run, protected by a password generated for the session; a daemon configured with `"ip": "127.0.0.1"` keeps it on the loopback interface locally.

The default pytest run excludes the `measurement` marker. The logging overhead and resource-lifetime measurement runs serially, writes its samples to the path in `KORA_MEASUREMENT_OUTPUT` (with the commit named by `KORA_MEASUREMENT_REVISION`, which CI sets to the pull request's head) and gates only the contract-derived criteria (queue bounds, bounded heap and resident-memory growth with threads and descriptors back at baseline over repeated configure-and-shutdown cycles, bounded process exit and idle shutdown, and `StreamMasker` within 60 s without holding back ordinary output); its redaction rows include a 256-value registry against diverse text and `StreamMasker` over 100,000 lines fed per line and in 64 KiB chunks with 0, 16 and 256 values, plus an adversarial per-line row against a 4 KiB value; CI runs it in the Linux 3.11 lane after the parallel checks and uploads the artifact `logging-overhead-measurement`:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/logging-overhead.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_logging_overhead.py
```

The CEL evaluator lifetime suite runs serially in its own `cel-measurements` Linux 3.11.17 job, retaining three independent processes per main workload, all cycle samples and retaining paths in `cel-evaluator-lifetime-measurement`. It measures native success/errors, cache turnover, repeated handles, cancellation, terminal failure, idle/busy thread hops, and exit with draining, blocked and broken diagnostics at disabled/INFO/DEBUG levels. Its first baseline and gates are recorded in [the evaluator lifetime record](../knowledge/research/cel-evaluator-lifetime-2026-10-21.md). These native-memory and resource-lifetime checks remain required alongside the compact regression below. Run on an otherwise idle machine:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/cel-evaluator-lifetime.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_cel_evaluator_lifetime.py
```

The compact CEL regression runs serially in that same dedicated job and keeps its existing required status. Ten fixed cases each use a fresh child and four real `Evaluator.evaluate` calls: one cold cache miss and three warm hits. Every timed call includes binding, queueing, compilation when needed, native execution and delivery, and must stay at or below 100 ms. Expression checks and startup precede timing; result assertions follow it. The [protocol and retained history](../knowledge/research/cel-cost-calibration-2026-10-22.md#current-compact-regression-protocol) define all cases and report fields. Run locally with:

```sh
KORA_CEL_COST_OUTPUT=measurements/cel-cost-smoke.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_cel_cost_regression.py
```

The reference environment is Ubuntu 24.04 x86_64 with CPython 3.11.17, uv 0.13.1 and the pinned CEL wheel. The 240-second suite, ten-second children, 256 KiB per-stream caps and ten-minute whole-CI budget remain unchanged. Artifact `cel-cost-regression-measurement` retains `cel-cost-smoke.json` and `cel-checkout-revision.txt`; the report requires a clean, matching CI checkout and records provenance, all forty intervals, raw child output, exact results and cleanup counters. A macOS local report is `non_reference` functional data and cannot qualify as reference evidence. The golden corpus and specification consistency checks in `tests/cel/test_cel_cost_regression.py` run with the ordinary source tests.

The 100 ms value is an observed coarse regression target for the fixed cases. The 100,000,000,000,000-unit steps budget and 16 MiB logical-memory estimate remain provisional; they establish no universal time bound, RSS envelope, interruption deadline or full native crash margin. Full fitting, RSS/GIL calibration, crash-threshold searches and five-runner collection are removed under the [scoped verification exclusion](../knowledge/reviews/ai-development-setup.md#cel-numerical-calibration-exclusion), not passing or pending gates. Historical pilot/bounds commands are available only at their measured revisions. Existing evaluator, browser, logging, database and scanner checks remain required, as do real API/scheduler integration and their performance obligations when implemented. The existing `cel` diagnostic activation row and future consumer error ownership in [observability.md](../knowledge/specs/observability.md#7-component-coverage-and-activation) are unchanged.

The metadata database lifetime measurement runs under the same marker, on every database that `KORA_TEST_DATABASES` selects, with its own step budget and contract-derived gates (balanced pool checkouts, a free SQLite writer, no open transaction and no temporary file after every ending path, threads, descriptors and server connections back at baseline, bounded heap growth over repeated migrate, connect and transaction cycles, one statement per read call, every marked run visited once per sweep); CI runs it after the logging measurement in the same lane and uploads the artifact `metadata-database-lifetime-measurement`:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/metadata-database-lifetime.json \
  uv run --no-sync pytest -q -m measurement tests/measurements/test_metadata_database_lifetime.py
```

The provider loading measurement runs under the same marker in three fresh `python -I` child processes over the installed environment plus 52 generated distributions (50 with one entry point, one with 50, one whose factory raises). Each child records one cold `load_provider`, then 5 warm-up and 50 measured calls of `load_provider`, of a missing and a raising provider, and of `discover_providers`. It gates only the contract-derived criteria: no module added, descriptors back at baseline and bounded heap growth over the measured calls, and every generated provider discovered. The timings are a baseline with no time gate. CI runs it after the database measurement in the same lane and uploads the artifact `provider-loading-cost-measurement`:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/provider-loading-cost.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_provider_loading_cost.py
```

The protocol model measurement runs under the same marker: the cold import of the protocol modules in three `python -I` child processes, the validation of 64 grant bindings, and the admission and release of secrets in six workloads (8-byte, 1 KiB and values at the 4 KiB per-form bound, plain and needing JSON escapes). It gates only the contract-derived criteria: every workload registers its expected forms and leaves the registry empty, and heap growth stays bounded and does not grow from one cycle to the next. The timings are a baseline with no time gate. CI runs it after the provider loading measurement in the same lane and uploads the artifact `protocol-models-cost-measurement`:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/protocol-models-cost.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_protocol_models_cost.py
```

The IR model measurement runs under the same marker: `ToolPolicy` validation and serialisation of 250,000 patterns, a 1 MiB depth-16 extension object, a dense 1 MiB extension and one just over the limit, a refused input whose last pattern is invalid, `KoraProtocolError` details and a provider `config_schema`, and dense 1 MiB objects of small integers, short ASCII, non-ASCII and escaped strings, empty objects, keys and floats through the bounded JSON parser, with one just over the limit. Each workload has a warm-up, 30 samples (10 for the dense ones), a traced peak and retained bytes, then 1,000 cycles of refused inputs follow. It gates only that each workload accepts or refuses as expected and, with the refused cycles, leaves retained memory at baseline; the timings are a baseline with no time gate. CI runs it after the protocol model measurement in the same lane and uploads the artifact `ir-models-cost-measurement`:

The common configuration models add minimal and populated record round trips, command and MCP containers at 1, 1,000 and 10,000 entries, long constrained strings, 64 independently bounded resource extensions, and late or many-error refusals. Ordinary cases use three warm-ups and 30 samples; scaling and refusal cases use three warm-ups and ten samples. Three fresh processes each run 1,000 lightweight success/failure cycles, checking model release, thread and descriptor counts, allocation differences and current/peak RSS after quiescence. Every post-drop traced-memory checkpoint retains the existing less-than-65,536-byte gate. Timings and peak/native-memory values establish baselines without a new numerical gate; the extension bound is per record, not an aggregate configuration cap. The full materialized-graph workload remains pending [issue #218](https://github.com/realkarych/kora/issues/218).

```sh
KORA_MEASUREMENT_OUTPUT=measurements/ir-models-cost.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_ir_models_cost.py
```

The diagnostic value foundation in [issue #219](https://github.com/realkarych/kora/issues/219) extends that command and artifact with minimal and maximal value construction, JSON encoding/parsing/round trips, real CEL refusal conversion, and malformed/oversized preflight refusals. Fixed ASCII, non-BMP and JSON-escaping fixtures record both decoded and encoded lengths; inputs are prepared before action baselines. Each new action keeps three warm-ups and 30 timing samples. Three serial traced/untraced child pairs each run 1,000 success/refusal cycles, with quiescent points every 100 cycles and bounded Python retaining-path summaries at 500 and 1,000. Every action releases its result and exception references; garbage collection occurs only at baseline and each 100-cycle quiescent point. Persistent snapshot and fixed evidence storage exist before the retained-memory baseline. Retention counters are read before snapshot/comparison temporaries, which are discarded before work resumes, so evidence bookkeeping cannot alter the workload-retention gate. The measurement children have a ten-second deadline and 256 KiB per-stream output caps; their pipes and processes are closed and reaped.

The Linux reference lane records current RSS, fresh-image high-water RSS, descriptors, Python and OS thread counts, and traced current/peak bytes plus tracer-owned memory. RSS is native-inclusive whole-process memory, not an exact count of live native allocations. The existing strict 64 KiB retained-Python allowance and outcome gates remain; no new numerical latency/RSS gate or calibrated zero baseline is introduced. Native trajectories and retaining paths require independent explanation of continuing growth before readiness, and unavailable required counters or unexplained growth remain unverified. New diagnostic workloads are first-implementation baselines; unchanged workloads retain their comparisons. Local macOS data does not replace Linux native-inclusive evidence. This does not alter the CEL 100 ms compact/lifetime checks or the browser benchmark exclusion.

`kora db upgrade` applies every pending migration to the database named by `KORA_DATABASE_URL` and prints the head revision; the accepted forms are `sqlite+aiosqlite:///<path>` (relative paths resolve against the working directory, and a new file is created with mode 0600) and a complete `postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>`, whose query string, if any, carries the driver's connection options such as the TLS mode (`?ssl=require`); the driver's `PGSSL*` variables apply only when the URL sets no `ssl` option, and no other `PG*` variable or password file is consulted. A missing, malformed or incomplete URL exits 78 with one line on stderr that never repeats the value; an unreachable database is an unhandled error (code 70) with content-free records. There is no command-line option for the URL, so it never appears in a process listing.

```sh
KORA_DATABASE_URL=sqlite+aiosqlite:///./.kora-data/kora.db uv run --no-sync kora db upgrade
```

Ruff runs with `--ignore-noqa`, so a `# noqa` comment suppresses nothing, and selects, besides `E`, `F`, `I`, `UP`, `B`, `ANN` and `S`, the families `BLE` (blind `except`), `PT` (pytest style, including `pytest.raises` without `match` on broad types), `PGH` (blanket `noqa` and `type: ignore`), `ERA` (commented-out code), `T20` (`print`) and `TID` with a banned-API table that names the skip, xfail, exit, mock and warning-filter APIs of `pytest`, `unittest` and `warnings`, plus `contextlib.suppress` and `os._exit`; Ruff ignores no file that `.gitignore` lists (`respect-gitignore = false`), and pytest fails at collection on an empty parameter set. Pyright runs with `enableTypeIgnoreComments = false`; `# pyright:` comments are caught by the repository test only. ESLint carries the local rule `kora/no-comments`, `noInlineConfig` (inline `eslint-disable` comments have no effect and are reported) and `@typescript-eslint/ban-ts-comment` for every `@ts-*` form. `tests/architecture/test_no_suppression.py` scans every hand-written Python, shell, SQL and Alembic-template file for comments and docstrings and every hand-written file, TypeScript and JavaScript included, for tool directives (ESLint owns the TypeScript comment ban); flags string markers, `skipTest`, `monkeypatch` and `mocker` fixtures, dynamic imports of `pytest`, `_pytest`, `warnings` or `unittest`, `pluginmanager` access under `tests/` and `src/kora/conformance/`, pytest hook and `pytest_plugins` bindings outside the two allowed `conftest.py` hooks, and blind handlers, with pinned counts in `kora.__main__`, `kora.observability.bootstrap` and `kora.observability.sink`; and compares the `[tool.ruff]`, `[tool.pyright]` and `[tool.pytest.ini_options]` tables, the `web/tsconfig*.json` documents, the `web/package.json` scripts and the absence of shadowing configuration files (`pytest.ini`, `ruff.toml`, `pyrightconfig.json`, a second `eslint.config.*` and the like) against the recorded values, so any allowance or configuration change edits that test. The [no-suppression research](../knowledge/research/no-suppression-checks-2026-10-20.md) records every family, allowance and residual. The dependency and secret scanners run in CI through the workflows `osv-scanner.yml` and `gitleaks.yml`; contributors who install the same releases locally (OSV-Scanner 2.7.0, Gitleaks 8.30.1) run the equivalent scans with the commands below. On a pull request Gitleaks scans the first 30 first-parent, non-merge commits of the pull request and OSV-Scanner reports only newly introduced vulnerabilities; on a push to `master` Gitleaks scans the pushed range and OSV-Scanner every lockfile in full; weekly, and on dispatch for Gitleaks, OSV-Scanner scans every lockfile in full and Gitleaks the whole history. Besides `uv.lock` and `web/pnpm-lock.yaml`, OSV-Scanner scans `.github/osv/common-expression-language/Cargo.lock`, the CEL binding's own crate lock, whose bundled Rust crates `uv.lock` cannot show. The `osv-scanner.toml` beside it ignores only the binding's root crate, whose name and version collide with an unrelated advisory, and applies to no other lockfile; no scan passes `--config`. CodeQL (`codeql.yml`) runs in CI only, because its analysis needs the hosted toolchain, and uploads its alerts to the repository's code-scanning view. The [logging and scanner research](../knowledge/research/logging-stack-and-ci-scanners-2026-10-20.md) records the pinned versions and what each scanner covers.

```sh
osv-scanner --lockfile=uv.lock --lockfile=web/pnpm-lock.yaml \
  --lockfile=.github/osv/common-expression-language/Cargo.lock
gitleaks git --redact
```

The build writes both archives to `dist/`. No test inspects the archives or installs the wheel. The wheel build hook described below checks that every local `src` and `href` reference in the built `index.html` resolves to a file in the output directory; nothing checks that those assets reach the wheel.

Vite generates `src/kora/api/static/THIRD_PARTY_LICENSES.md` from the bundled frontend dependencies. The wheel includes this inventory and their license text alongside the generated assets. The project's Apache-2.0 metadata and license remain separate. The [dated packaging record](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#packaging-implementation-record) links the generation and upstream license references.

The first sync installs the locked dependencies without the project; the editable install of kora that follows, under the locked build constraints, lets the tests import the package and start the `kora` entry point with a build backend resolved from the lock; the final sync only confirms that the environment and the dependency files are unchanged. Editable setup requires Python and uv, skips the frontend hook and does not package production assets. For frontend development, use `pnpm --dir web run dev`. A normal wheel build requires Node and pnpm: its hook performs a frozen install, type-checks and builds the frontend, then embeds `src/kora/api/static/`. The sdist carries those source inputs and toolchain files, so building a wheel from an sdist has the same Node and pnpm requirements. Installing an already-built wheel requires only supported Python; it does not invoke frontend tools.

`KORA_BUILD_OFFLINE=1` makes the hook's pnpm install offline, while `uv build --offline` makes Python build dependency resolution offline. Complete the dependency setup, including registry metadata priming, first. The hook accepts only an unset value, `0` or `1`; a missing tool, install failure, build failure or missing frontend asset aborts a normal wheel build. Build output is generated and ignored, and should never be committed.

## Documentation and schema checks

The documentation jobs use pinned Markdown and Mermaid tools through npx, lychee as recorded in the [dated research](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#ci-and-documentation-check-record), and a pinned schema checker through uvx. Install that lychee release using its [official installation instructions](https://lychee.cli.rs/installation/). These tool setups may download dependencies; the link check itself uses the repository's offline configuration.

Run this block in Bash. Git discovers tracked and non-ignored untracked Markdown, so installed dependencies, generated assets and ignored scratch work are excluded. NUL-delimited paths preserve spaces and newlines. The Markdown linter retains the research exclusion in [`.markdownlintignore`](../.markdownlintignore); links and Mermaid diagrams still cover research.

```bash
set -euo pipefail
KORA_DOC_FILES="$(mktemp)"
git ls-files --cached --others --exclude-standard -z -- '*.md' > "$KORA_DOC_FILES"
test -s "$KORA_DOC_FILES"
xargs -0 npx --yes markdownlint-cli@0.45.0 -- < "$KORA_DOC_FILES"
xargs -0 lychee --config lychee.toml -- < "$KORA_DOC_FILES"

KORA_MERMAID_FILES="$(mktemp)"
xargs -0 rg --files-with-matches --null '^```mermaid' -- \
  < "$KORA_DOC_FILES" > "$KORA_MERMAID_FILES"
test -s "$KORA_MERMAID_FILES"
out="$(mktemp -d)"
while IFS= read -r -d '' file; do
  echo "parsing $file"
  npx --yes @mermaid-js/mermaid-cli@12.0.0 --quiet \
    --puppeteerConfigFile .github/puppeteer-config.json \
    --input "$file" --output "$out/${file//\//_}" --artefacts "$out"
done < "$KORA_MERMAID_FILES"

uvx check-jsonschema@0.38.2 --schemafile knowledge/specs/ir.schema.json knowledge/specs/examples/*.ir.yaml
uvx check-jsonschema@0.38.2 --builtin-schema vendor.github-issue-forms .github/ISSUE_TEMPLATE/change.yml
uvx check-jsonschema@0.38.2 --builtin-schema vendor.github-issue-config .github/ISSUE_TEMPLATE/config.yml
```

Mermaid rendering uses Puppeteer and its downloaded browser. `rg` (ripgrep) selects Markdown files containing a Mermaid fence; CI installs it explicitly before discovery. Empty discovery fails. In CI, the pinned lychee action receives the same discovered filenames after Bash escaping because its entrypoint evaluates its arguments.

## Local and native review

The component-review direction follows [issue #108](https://github.com/realkarych/kora/issues/108); this amendment awaits owner wording review in its PR. Use the repository’s [kora-verify skill](../.claude/skills/kora-verify/SKILL.md) before planning and before readiness. Codex discovers it through `.agents/skills/kora-verify`; Claude Code uses `.claude/skills/kora-verify`. Invoke the skill in the local coding-agent session with the base/head or working-tree scope, issue criteria and available check results. A human contributor can request the same local review; personal agent plugins are unnecessary.

The coordinator follows direct and transitive effects, splits affected component owners, and starts a fresh agent per component/profile. Descendants may delegate narrower work with inherited permissions. Performance review includes the component’s memory/resource-lifetime workload. Local reviewers run the applicable registered checks; native GitHub reviewers inspect revision-matched evidence without executing PR code. Preserve local commands and CI jobs together as new measurement suites activate.

On GitHub, native automatic review or a PR comment containing `@codex review` uses these same checked-in rules. Native delegation is an operating assumption. Configure the repository’s automatic review for all PR authors and new heads as described in the [verification workflow](../knowledge/reviews/ai-development-setup.md#github-review-entry-point). This service integration runs alongside CI; the skill does not enable its settings or turn an aggregate bot verdict into a required CI check. Record inaccessible settings as activation gaps and obtain missing scoped reviews before merge.

## CI and contributions

[CI](../.github/workflows/ci.yml) runs every source and build check in three lanes: the supported Python floor on Linux and macOS, and the upper tested Python release on Linux. Exact interpreters and runner images are in the workflow and dated research. An explicitly selected interpreter takes precedence over the repository default; local alternate-interpreter checks can set `UV_PYTHON` before the initial sync. Each lane exports hashed constraints and primes registry metadata online, then builds offline from the sdist using the same constraints, and verifies editable setup and unchanged dependency files. Documentation, links, Mermaid diagrams, graph examples and GitHub templates remain separate required jobs.

CI caches dependency stores using lockfile-derived keys, with separate uv cache suffixes for each Python lane and documentation job. It does not cache virtual environments; the pnpm setup action owns the sole pnpm store cache. Check actual restore/save logs when comparing a cold run with a warm repeat. The complete workflow must finish in under ten minutes, measured from the earliest job start to the latest job completion; individual job timeouts do not prove that budget.

Setup tooling must install the pinned pnpm as a native executable before project commands. A legacy bootstrap can add package-manager entries to the lockfile even during frozen installation; the final dependency-file diff checks that setup and builds preserve it. The [dated correction record](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#pnpm-bootstrap-lockfile-correction) identifies the compatible action and reproduction evidence.

Every change needs an issue, an owner-approved implementation plan before nontrivial edits, and a pull request closing exactly that issue. Keep specifications and implementation together, include each acceptance criterion and its verification evidence in the PR, and run all commands above before requesting review. New runtime dependencies enter only with an implemented consumer and a recorded reason in the [stack inventory](../knowledge/architecture/detailed.md#stack-per-component).

## Browser checks

The owner approved removal of the experimental numerical browser benchmark under [issue #179](https://github.com/realkarych/kora/issues/179#issuecomment-6329273349) on 2026-10-22, including matching documentation and delegated independent review before merge. The retained protocol is `console-functional-v1`.

Install the pinned Chromium distribution after the frozen frontend installation, then run the registered functional suite:

```sh
pnpm --dir web exec playwright install chromium
pnpm --dir web run test:browser
```

The command builds the current production assets and checks the real production and development servers at 1280, 768 and 375 pixels. Each viewport covers navigation, reload, visible content, document/font/paint readiness, basic semantics, keyboard focus and horizontal overflow, with zero unexpected browser diagnostics. Screenshots, terminal screenshots and traces are retained on the first failure. `--smoke` is a compatibility alias for the same suite; removed and unknown modes fail before any output directory, server or browser is acquired.

Six real failure scenarios cover startup, navigation, assertion, deadline, cancellation and cancellation during browser acquisition. Ordinary runner cancellation and a report-path collision prove that owned descendants and temporary contents are released before parent fallback. Forced cancellation checks the parent's bounded cleanup. Exact-origin HTTP/WebSocket routing, the local proxy, blocked service workers, sanitized child environments and artifact-limit fixtures remain active. Cleanup evidence includes exact owned PIDs, Linux descriptor/process counters, proxy sockets and temporary directories; unavailable native counters on macOS are reported as unverified.

Every invocation writes a fresh directory under `measurements/browser/`; `KORA_BROWSER_OUTPUT` changes its parent. Reports record checkout/head identity, dirty state, locked tools/browser distribution, asset hashes, functional outcomes and bounded completion diagnostics. `NODE_ENV` must be unset or `production`. Artifacts remain bounded to 32 MiB per file and 512 MiB in total, with reserved report capacity.

CI runs one `Console browser checks` job and uploads `console-browser-checks`, retaining failure evidence. Read-only permissions, pinned tools/actions, other lanes and the ten-minute whole-pipeline budget remain. Numerical base/head loading and memory comparisons, browser heap snapshots, calibrated RSS/heap/loading budgets and five-runner training/holdout stages have been removed. Their historical failures remain failures, with no current numerical regression or leak-freedom guarantee. This scoped removal does not change backend, CEL or database performance obligations. The [current protocol and historical evidence](../knowledge/research/console-browser-checks-2026-10-21.md) record the distinction. Full accessibility scanning remains unverified; API-backed journeys activate with their implementing components.
