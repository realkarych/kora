# Development

Scope and implementation plan approved by the project owner under [issue #86](https://github.com/realkarych/kora/issues/86) on 2026-10-18; wording pending line-by-line owner review.

Packaging-test removal amendment dated 2026-10-19: removal decided by the project owner under [issue #112](https://github.com/realkarych/kora/issues/112); wording pending line-by-line owner review.

Import-layer test amendment dated 2026-10-19: the pytest source check added under [issue #104](https://github.com/realkarych/kora/issues/104); wording pending line-by-line owner review.

Logging and scanner amendment dated 2026-10-20: the measurement step, the Ruff `S` family and the scanner commands added under [issue #147](https://github.com/realkarych/kora/issues/147); wording pending line-by-line owner review.

The repository contains an importable typed Python package with the logging bootstrap and the `kora` entry point (`kora --version`), a minimal React shell and distribution builds. The execution engine, HTTP API, the CLI commands and full web console are subsequent implementation work. Read [AGENTS.md](../AGENTS.md) before contributing and [ADR-0013](decisions/0013-implementation-stack.md) for the stack decision.

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
uv sync --locked --group build
KORA_BUILD_CONSTRAINT_DIR="$(mktemp -d)"
uv export --locked --only-group build --no-emit-project \
  --format requirements.txt \
  --output-file "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
uv pip install --python "$PWD/.venv/bin/python" \
  --target "$KORA_BUILD_CONSTRAINT_DIR/backend" \
  --require-hashes \
  --requirements "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
pnpm --dir web install --frozen-lockfile
```

The disposable target primes registry metadata for the isolated build resolver using the locked build requirements and their hashes. Locked sync installs cached artifacts without necessarily populating that metadata, so it is insufficient on a fresh cache. The target is separate from the project environment and is not used by the build itself.

## Source checks and distributions

Run every source check, then build the sdist and build a wheel from that sdist under isolated, hash-constrained build requirements:

```sh
uv lock --check
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync pyright
uv run --no-sync pytest -q
pnpm --dir web run typecheck
pnpm --dir web run lint
pnpm --dir web run build

KORA_BUILD_OFFLINE=1 uv build --offline \
  --python "$PWD/.venv/bin/python" \
  --build-constraints "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt" \
  --require-hashes --clear

uv sync --locked --group build
git diff --exit-code -- pyproject.toml uv.lock web/package.json \
  web/pnpm-lock.yaml web/pnpm-workspace.yaml
```

The default pytest run excludes the `measurement` marker. The logging overhead and resource-lifetime measurement runs serially, writes its samples to the path in `KORA_MEASUREMENT_OUTPUT` and gates only the contract-derived criteria (queue bounds, bounded heap and resident-memory growth with threads and descriptors back at baseline over repeated configure-and-shutdown cycles, bounded process exit and idle shutdown); CI runs it in the Linux 3.11 lane after the parallel checks and uploads the artifact `logging-overhead-measurement`:

```sh
KORA_MEASUREMENT_OUTPUT=measurements/logging-overhead.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_logging_overhead.py
```

Ruff's `S` family runs inside `ruff check`. The dependency and secret scanners run in CI through the workflows `osv-scanner.yml` and `gitleaks.yml`; contributors who install the same releases locally (OSV-Scanner 2.7.0, Gitleaks 8.30.1) run the equivalent scans with the commands below. On a pull request Gitleaks scans the first 30 first-parent, non-merge commits of the pull request and OSV-Scanner reports only newly introduced vulnerabilities; on a push to `master` Gitleaks scans the pushed range and OSV-Scanner both lockfiles in full; weekly, and on dispatch for Gitleaks, OSV-Scanner scans both lockfiles in full and Gitleaks the whole history. CodeQL (`codeql.yml`) runs in CI only, because its analysis needs the hosted toolchain, and uploads its alerts to the repository's code-scanning view. The [logging and scanner research](../knowledge/research/logging-stack-and-ci-scanners-2026-10-20.md) records the pinned versions and what each scanner covers.

```sh
osv-scanner --lockfile=uv.lock --lockfile=web/pnpm-lock.yaml
gitleaks git --redact
```

The build writes both archives to `dist/`. No test inspects the archives or installs the wheel. The wheel build hook described below checks that every local `src` and `href` reference in the built `index.html` resolves to a file in the output directory; nothing checks that those assets reach the wheel.

Vite generates `src/kora/api/static/THIRD_PARTY_LICENSES.md` from the bundled frontend dependencies. The wheel includes this inventory and their license text alongside the generated assets. The project's Apache-2.0 metadata and license remain separate. The [dated packaging record](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#packaging-implementation-record) links the generation and upstream license references.

The first sync installs kora as editable for Python development, so that the tests can import the package and start the `kora` entry point; the final sync only confirms that the environment and the dependency files are unchanged. Editable setup requires Python and uv, skips the frontend hook and does not package production assets. For frontend development, use `pnpm --dir web run dev`. A normal wheel build requires Node and pnpm: its hook performs a frozen install, type-checks and builds the frontend, then embeds `src/kora/api/static/`. The sdist carries those source inputs and toolchain files, so building a wheel from an sdist has the same Node and pnpm requirements. Installing an already-built wheel requires only supported Python; it does not invoke frontend tools.

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
