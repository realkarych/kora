# Development

Scope and implementation plan approved by the project owner under [issue #86](https://github.com/realkarych/kora/issues/86) on 2026-10-18; wording pending line-by-line owner review.

The repository contains an importable typed Python package, a minimal React shell, distribution builds and packaging integration tests. The execution engine, HTTP API, CLI and full web console are subsequent implementation work. Read [AGENTS.md](../AGENTS.md) before contributing and [ADR-0013](decisions/0013-implementation-stack.md) for the stack decision.

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
uv sync --locked --group build --no-install-project
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
pnpm --dir web run typecheck
pnpm --dir web run lint
pnpm --dir web run build

KORA_BUILD_OFFLINE=1 uv build --offline \
  --python "$PWD/.venv/bin/python" \
  --build-constraints "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt" \
  --require-hashes --clear
uv run --no-sync pytest -q tests/packaging

uv sync --locked --group build
git diff --exit-code -- pyproject.toml uv.lock web/package.json \
  web/pnpm-lock.yaml web/pnpm-workspace.yaml
```

The packaging suite inspects both archives and installs the real wheel into a new environment. Its inspection runs outside the checkout with isolated Python so source imports cannot hide missing files. A missing distribution or absent tests is a failure. Generated archives are under `dist/`.

Vite generates `src/kora/api/static/THIRD_PARTY_LICENSES.md` from the bundled frontend dependencies. The wheel includes this inventory and their license text alongside the generated assets. The installed-wheel inspection requires notices for React, React DOM and the bundled scheduler, and checks each dependency entry for substantive copyright, permission, notice-retention and warranty text. The project's Apache-2.0 metadata and license remain separate. The [dated packaging record](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#packaging-implementation-record) links the generation and upstream license references.

The final sync installs kora as editable for Python development. Editable setup requires Python and uv, skips the frontend hook and does not package production assets. For frontend development, use `pnpm --dir web run dev`. A normal wheel build requires Node and pnpm: its hook performs a frozen install, type-checks and builds the frontend, then embeds `src/kora/api/static/`. The sdist carries those source inputs and toolchain files, so building a wheel from an sdist has the same Node and pnpm requirements. Installing an already-built wheel requires only supported Python; it does not invoke frontend tools.

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

## CI and contributions

[CI](../.github/workflows/ci.yml) runs every source, build and packaging check in three lanes: the supported Python floor on Linux and macOS, and the upper tested Python release on Linux. Exact interpreters and runner images are in the workflow and dated research. An explicitly selected interpreter takes precedence over the repository default; local alternate-interpreter checks can set `UV_PYTHON` before the initial sync. Each lane exports hashed constraints and primes registry metadata online, then builds offline from the sdist using the same constraints, checks a real wheel installation, and verifies editable setup and unchanged dependency files. Documentation, links, Mermaid diagrams, graph examples and GitHub templates remain separate required jobs.

CI caches dependency stores using lockfile-derived keys, with separate uv cache suffixes for each Python lane and documentation job. It does not cache virtual environments; the pnpm setup action owns the sole pnpm store cache. Check actual restore/save logs when comparing a cold run with a warm repeat. The complete workflow must finish in under ten minutes, measured from the earliest job start to the latest job completion; individual job timeouts do not prove that budget.

Setup tooling must install the pinned pnpm as a native executable before project commands. A legacy bootstrap can add package-manager entries to the lockfile even during frozen installation; the final dependency-file diff checks that setup and builds preserve it. The [dated correction record](../knowledge/research/repository-scaffold-and-ci-2026-10-18.md#pnpm-bootstrap-lockfile-correction) identifies the compatible action and reproduction evidence.

Every change needs an issue, an owner-approved implementation plan before nontrivial edits, and a pull request closing exactly that issue. Keep specifications and implementation together, include each acceptance criterion and its verification evidence in the PR, and run all commands above before requesting review. New runtime dependencies enter only with an implemented consumer and a recorded reason in the [stack inventory](../knowledge/architecture/detailed.md#stack-per-component).
