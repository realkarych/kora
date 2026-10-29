## Research: repository scaffold and CI

Checked on **2026-10-18**, against `master` at `80c1bb9d8b3214ff5e8d852503d47c1f62dd941b`. The repository observations below are the planning baseline. The implementation record identifies the selected Python tools and the checks performed for issue #86.

### Repository findings

- ADR-0013 already settles the language, package manager, build backend and frontend stack. The current architecture reference places packaged frontend output at `src/kora/api/static`.
- The repository contains no Python/frontend manifests, executable tests or `LICENSE`. The current five CI jobs check documentation and schemas.
- Existing [master CI run 39067610021](https://github.com/realkarych/kora/actions/runs/39067610021) succeeded. Its earliest job started at 2026-10-17 20:39:08 UTC and its latest job completed at 20:39:40 UTC: **32 seconds**. This is a documentation-only baseline, not evidence that the proposed build meets the budget.
- The reference deliberately leaves shared-package placement and health serving open. Creating empty domain modules or an invented health endpoint would expand this change.
- Scenario-testing work in [#46](https://github.com/realkarych/kora/issues/46) concerns a later functional slice. This scaffold needs actual packaging tests, not fake providers or metadata fixtures.

### Findings that shape the plan

| Finding | Consequence |
|---|---|
| Default `uv build` builds a wheel from the sdist; explicitly requesting both targets changes that path. | Use the default path to expose missing source-package inputs. |
| An isolated PEP 517 build has its own dependency resolution. A full dev environment can conceal missing build requirements. | Export a hashed build-only constraints file from `uv.lock` and retain build isolation. |
| Ignored generated files need explicit Hatch inclusion. | Mark `src/kora/api/static/**` as wheel artifacts and inspect the resulting archive. |
| Vite transpiles TypeScript without checking types. | Run strict `tsc` separately from `vite build`. |
| Hatch hook documentation and moving upstream source differ in generic interface shape. | Pin Hatchling, type against that installed release, and include the hook in strict Pyright checks. |
| Current pnpm uses `allowBuilds`; old template settings are not interchangeable. | Pin pnpm and explicitly record only the dependency build scripts needed by the actual lockfile. |
| An editable import can resolve the source tree and hide omitted wheel files. | Install a real local wheel into a new environment and run Python in isolation outside the checkout. |
| Broad local Markdown scans encounter installed dependencies and generated files. | Discover repository Markdown through Git, retaining existing intentional research exclusions and coverage of all other project documents. |

Primary references: [uv builds and constraints](https://docs.astral.sh/uv/concepts/projects/build/), [uv lock/sync](https://docs.astral.sh/uv/concepts/projects/sync/), [Hatch build inclusion](https://hatch.pypa.io/latest/config/build/), [Hatch hook interface](https://hatch.pypa.io/latest/plugins/build-hook/reference/), [Vite TypeScript behavior](https://vite.dev/guide/features.html#typescript), [Vite output cleanup](https://vite.dev/config/build-options.html#build-emptyoutdir), [pnpm build settings](https://pnpm.io/settings/build), [pytest integration guidance](https://docs.pytest.org/en/stable/explanation/goodpractices.html).

### Toolchain baseline

- Python **3.11** is the repository's supported floor. The current stable release is **3.15.0**; the proposed matrix checks both endpoints and both operating systems. [Python releases](https://www.python.org/downloads/)
- Node **24** retains the existing CI major version. Select one compatible exact patch for the frontend version file during implementation.
- **uv 0.13.1**, published 2026-10-14, and **pnpm 12.14.0**, published 2026-10-15, are verified release baselines. Dependency resolution and smoke tests still have to establish compatibility. [uv release](https://github.com/astral-sh/uv/releases/tag/0.13.1), [pnpm release](https://github.com/pnpm/pnpm/releases/tag/v12.14.0)
- Keep existing action SHAs. The initial pnpm setup selection was v6.0.2 at `71c92474e7e4f5bca283fb17ef80fba9cdb2b4b1`; the [hosted bootstrap correction](#pnpm-bootstrap-lockfile-correction) replaces it with the compatible v6.1.0 release. The action selects the exact version from `web/package.json` and owns store caching. Avoid a second cache for the same store. [Action documentation](https://github.com/pnpm/action-setup), [uv Actions guide](https://docs.astral.sh/uv/guides/integration/github/)

Exact package versions are selected with uv/pnpm during implementation and recorded with their rationale. No moving `latest` tool version is part of the new CI contract.

### Scope recommendation

Build the complete packaging path with a minimal React shell and no Python runtime dependencies. Install only Hatchling and source-check/test tooling on the Python side, and React/React DOM plus the frontend build/check dependencies actually used. Add database tooling, provider SDKs, UI libraries and optional runtime extras when their first consumers arrive.

An unrelated distribution already owns [the PyPI name `kora`](https://pypi.org/project/kora/). That does not block local artifacts under the accepted name, but public publishing and generic `pip install kora` instructions must wait for a naming/ownership decision.

The project owner approved the implementation plan in issue #86 on 2026-10-18. This research records implementation evidence rather than changing any architecture decision.

## Python implementation record

Checked on **2026-10-18**. The package starts at `0.1.0.dev0`, requires Python `>=3.11`, and has no runtime dependencies or advertised extras. Runtime libraries and optional extras enter the distribution with their first implemented consumer. Package metadata uses the SPDX expression `Apache-2.0` and includes the complete upstream [Apache license text](https://www.apache.org/licenses/LICENSE-2.0.txt).

| Tool | Exact version | Reason and primary source |
|---|---|---|
| Local Python and minimum CI lane | 3.11.17 | Supported language floor with the current security patch; [official release](https://www.python.org/downloads/release/python-31117/) |
| Upper CI lane | 3.15.0 | Exercise the current stable Python endpoint; [official release](https://www.python.org/downloads/release/python-3150/) |
| uv | 0.13.1 | Approved dependency manager, lock generator and isolated distribution builder; [release](https://github.com/astral-sh/uv/releases/tag/0.13.1) |
| Hatchling | 1.32.4 | ADR-0013 build backend, pinned identically in `build-system.requires` and the `build` dependency group so locked constraints also cover isolated builds; [release metadata](https://pypi.org/pypi/hatchling/1.32.4/json) |
| Ruff | 0.17.1 | One formatter and linter for handwritten Python, including imports, modern syntax, common bugs and annotations; [release metadata](https://pypi.org/pypi/ruff/0.17.1/json) |
| Pyright | 1.1.414 | Strict static typing required by contributor rules; [release metadata](https://pypi.org/pypi/pyright/1.1.414/json) |
| pytest | 9.1.1 | Integration-test runner from ADR-0013; strict configuration/markers, importlib collection and warnings as errors; [release metadata](https://pypi.org/pypi/pytest/9.1.1/json) |

All Python dependencies were added and locked through the pinned uv release. Build and development dependencies are separate groups, with no build backend in the runtime requirements. `uv.lock` records the full transitive graph and hashes. The Python wrapper for Pyright uses Node already required by the frontend build toolchain.

Strict Pyright source inclusion grows with the scaffold: the initial package is included immediately, and the build hook and packaging tests enter the same configuration when those files are implemented. Ruff discovers Python sources, stubs and notebooks throughout the checkout and checks `pyproject.toml`, using the generated-file exclusions. Its default Markdown-fence discovery is deliberately outside the source-code check: specifications and research snippets are checked as documents. No Python source path or lint rule is suppressed.

## Packaging implementation record

Checked on **2026-10-18**, against the installed Hatchling 1.32.4 source. Its hook interface is generic over a builder configuration, so the custom hook subclasses `BuildHookInterface[BuilderConfig]`, importing `BuilderConfig` from `hatchling.builders.config`. Its `initialize(self, version: str, build_data: object) -> None` override accepts the backend's build data without reading or modifying it. The hook is registered only for the wheel target and is included in strict Pyright checks.

Standard wheel builds remove prior generated frontend output, run the frozen pnpm install and the frontend build from the source-package root, and require `index.html` plus every local `src` or `href` file it references. A missing tool, failed installation, failed frontend build, missing entry point or missing referenced file aborts packaging. Only an unset value, `0` or `1` is accepted for `KORA_BUILD_OFFLINE`; `1` adds `--offline` to the pnpm installation. Editable builds return before checking that option or invoking frontend tools, and their wheels contain no production frontend files.

The wheel explicitly selects ignored static output through `artifacts = ["src/kora/api/static/**"]`. The sdist explicitly includes Python sources and typing marker, the hook, package metadata and lockfile, README, license, Python version file, and the complete frontend source/configuration/manifests/lockfile/Node version file. Its exclusions also apply when rebuilding outside Git: generated static output, frontend dependencies and Vite caches do not enter the source archive.

Dependency-notice verification on **2026-10-18** found that the default frontend bundle omitted its dependencies' license text. Vite's existing [`build.license`](https://vite.dev/config/build-options#build-license) option now generates `THIRD_PARTY_LICENSES.md` relative to the static output directory; the existing wheel artifact rule carries it into the installed package. The inventory is generated from bundled dependencies rather than maintained as a handwritten license copy. The installed `web/node_modules/react/LICENSE` and `web/node_modules/react-dom/LICENSE`, consistent with the [official React license](https://github.com/react/react/blob/main/LICENSE), require retention of their copyright and permission notices. The installation check requires inventory entries for `react`, `react-dom` and the bundled transitive `scheduler`, and checks each emitted entry for its MIT identifier and substantive copyright, permission, notice-retention and warranty text. Removing generation reproduces a failure against a real wheel installation. The project's Apache-2.0 license and distribution metadata are unchanged.

The isolated build was verified with `uv export --locked --only-group build --no-emit-project --format requirements.txt` and default `KORA_BUILD_OFFLINE=1 uv build --offline --python "$PWD/.venv/bin/python" --build-constraints <exported-file> --require-hashes --clear`. The export retains exact transitive versions and hashes. Build output explicitly reported both “Building source distribution” and “Building wheel from source distribution”; the latter performed a fresh frozen offline pnpm installation and production build. Archive inspection confirmed all required source inputs in the sdist, no generated static files there, and the generated HTML, JavaScript and CSS together with Python sources, typing marker, metadata and license in the wheel. A direct backend editable invocation must first provision Hatchling's dynamically requested editable-build requirement; ordinary uv editable installation handles that backend requirement automatically.

## Frontend implementation record

Checked on **2026-10-18**. The private `web/` package contains only a React shell identifying kora. It has no API calls, product screens or domain data. The production build writes `index.html` and hashed JavaScript/CSS assets into `src/kora/api/static`; Vite's explicit `emptyOutDir` setting clears only that generated output directory. The `build` command runs the strict browser and Vite-configuration type checks before bundling. The lint command rejects warnings.

| Dependency or tool | Exact version | Reason and primary source |
|---|---|---|
| Node | 24.22.0 | Approved Node major with an exact patch in `web/.node-version`; official macOS ARM64 archive verified against [release checksums](https://nodejs.org/dist/v24.22.0/SHASUMS256.txt) |
| pnpm | 12.14.0 | ADR-0013 frontend dependency manager, pinned in `packageManager` and package engines; [release](https://github.com/pnpm/pnpm/releases/tag/v12.14.0) |
| `react`, `react-dom` | 19.3.1 | ADR-0013 rendering runtime and browser mounting; [React metadata](https://registry.npmjs.org/react/19.3.1), [React DOM metadata](https://registry.npmjs.org/react-dom/19.3.1) |
| `vite` | 8.3.4 | ADR-0013 development server and production bundler; [metadata](https://registry.npmjs.org/vite/8.3.4) |
| `@vitejs/plugin-react` | 6.1.2 | Vite's React integration and refresh support; its peer range includes Vite 8; [metadata](https://registry.npmjs.org/@vitejs/plugin-react/6.1.2) |
| `typescript` | 6.0.3 | Strict application and build-configuration checks; compatible with the parser's `>=4.8.4 <6.1.0` peer range, unlike the current TypeScript 7 release; [metadata](https://registry.npmjs.org/typescript/6.0.3) |
| `@types/react`, `@types/react-dom` | 19.3.0 | React and browser-renderer type definitions aligned with runtime major; [React metadata](https://registry.npmjs.org/@types/react/19.3.0), [React DOM metadata](https://registry.npmjs.org/@types/react-dom/19.3.0) |
| `@types/node` | 24.19.2 | Node types matching the build runtime major; [metadata](https://registry.npmjs.org/@types/node/24.19.2) |
| `eslint` | 10.13.0 | Mandatory frontend source checks; [metadata](https://registry.npmjs.org/eslint/10.13.0) |
| `@eslint/js` | 10.0.1 | Recommended JavaScript rules for the maintained configuration and source; [metadata](https://registry.npmjs.org/@eslint/js/10.0.1) |
| `typescript-eslint` | 8.71.2 | Strict type-aware TypeScript linting using the project service; [metadata](https://registry.npmjs.org/typescript-eslint/8.71.2) |
| `globals` | 17.13.0 | Browser globals for application code and Node globals for build/lint configuration; [metadata](https://registry.npmjs.org/globals/17.13.0) |
| `eslint-plugin-react-hooks` | 7.1.1 | Recommended React hook correctness rules; [metadata](https://registry.npmjs.org/eslint-plugin-react-hooks/7.1.1) |
| `eslint-plugin-react-refresh` | 0.5.7 | Vite refresh-safe component export rules; [metadata](https://registry.npmjs.org/eslint-plugin-react-refresh/0.5.7) |

All frontend dependencies were added with pinned pnpm and exact direct versions. The generated lock records transitives, integrity hashes and platform-specific optional native packages. Vite 8 uses Rolldown and Lightning CSS with published native optional dependencies; this scaffold does not install esbuild. Inspection of the installed manifests found no `preinstall`, `install`, `postinstall` or `binding.gyp` build obligations. Some registry packages retain `prepare` scripts used before publication; these do not execute when installing published registry artifacts.

Consequently, `pnpm-workspace.yaml` explicitly sets `strictDepBuilds: true` and `allowBuilds: {}`. No dependency script is approved or required by this graph. A newly introduced script requires a reviewed decision before installation can succeed. These settings follow the pinned pnpm [build policy](https://pnpm.io/settings/build). An initial inspection install disabled scripts; a subsequent fresh frozen offline install used the committed policy normally, installed all 161 host-compatible packages and left both the lockfile and policy byte-for-byte unchanged. TypeScript and ESLint checks passed with no diagnostics.

## CI and documentation check record

Checked on **2026-10-18**. The implementation retains the existing pull-request/master-push triggers, read-only contents permission, concurrency cancellation and all five documentation/schema jobs. The scaffold matrix adds Ubuntu 24.04 / Python 3.11.17, Ubuntu 24.04 / Python 3.15.0 and macOS 15 / Python 3.11.17, with `fail-fast: false` and no allowed failures. Every lane runs the same locked setup, Python/frontend checks, constrained offline sdist-to-wheel build, real packaging integration tests and final editable sync. The [development guide](../../docs/development.md) contains the same local command contract.

| CI tool or action | Pin | Evidence and reason |
|---|---|---|
| Checkout | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1) | Existing pin retained; every checkout disables persisted credentials. |
| Node setup | `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020` (v7.0.0) | Existing pin retained; [pinned interface](https://github.com/actions/setup-node/blob/820762786026740c76f36085b0efc47a31fe5020/action.yml) accepts the Node version file and disabling automatic package-manager caching. |
| uv setup | `astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7` (v10.2.0) | Existing pin retained; [pinned interface](https://github.com/astral-sh/setup-uv/blob/c18668ad3cf93ea998bef934396af7bb5c839dc7/action.yml) supports the selected uv/Python versions, dependency-glob cache invalidation and separate cache suffixes. |
| pnpm setup | `pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413` (v6.1.0) | Compatible native pnpm bootstrap, pinned to the commit to which the [annotated tag object](https://api.github.com/repos/pnpm/action-setup/git/tags/d9184bf108216479bc5a137cc391f4d7b14c870b) dereferences. The [pinned interface](https://github.com/pnpm/action-setup/blob/ea17c68df8912ef543352723c149a84f56e3d413/action.yml) reads the exact package manager from `web/package.json` and owns the pnpm store cache keyed by `web/pnpm-lock.yaml`. |
| Link action | `lycheeverse/lychee-action@e7477775783ea5526144ba13e8db5eec57747ce8` (v2.9.0), lychee 0.24.2 | Existing action and binary pins retained. Its [entrypoint](https://github.com/lycheeverse/lychee-action/blob/e7477775783ea5526144ba13e8db5eec57747ce8/entrypoint.sh) evaluates arguments; each discovered filename is escaped with Bash `printf '%q'` before passing one output value to the action. |
| Markdown lint | `markdownlint-cli@0.45.0` | Existing version and repository configuration retained, including the `knowledge/research/` exclusion. |
| Mermaid | `@mermaid-js/mermaid-cli@12.0.0` | Existing version and Puppeteer configuration retained; every discovered Markdown file with a Mermaid fence is rendered. |
| Schema validation | `check-jsonschema@0.38.2` | Existing version and all graph-example/issue-form/issue-config inputs retained. |

Documentation discovery uses `git ls-files --cached --others --exclude-standard -z -- '*.md'`. Local and CI paths both include non-ignored untracked documents, retain every tracked document (including research for links and Mermaid), and omit ignored dependency/build/scratch directories. NUL delimiters and quoted argument handling preserve filenames containing whitespace or shell metacharacters. An empty discovery fails rather than producing a successful empty check. Local lychee uses `xargs -0`; the action receives shell-escaped arguments to account for its existing entrypoint implementation.

Setup downloads and installs dependencies before offline build and packaging checks. setup-uv caches its dependency store with an explicit per-Python-lane suffix, separated from schema-check jobs; no virtual environments are cached. The pnpm action is the only pnpm cache owner. Its pinned implementation uses an OS/architecture/lock-hash key, so the two Linux Python lanes can share a store. A new lockfile is expected to miss on the first run, but only the actual restore/save logs establish a cold run. Both a genuinely cold run and a warm repeat must be inspected on the final PR candidate; complete duration is latest job completion minus earliest job start and must be under 600 seconds. A ten-minute job timeout is only a guard. Actual remote run links, cache observations and measured durations belong in the PR evidence and are not established by this implementation record.

The editable path was also exercised by forcing an actual package rebuild into a fresh environment with `UV_PROJECT_ENVIRONMENT`, the absolute pinned uv/Python executables and `PATH=/usr/bin:/bin` (no Node or pnpm), after caching backend requirements. That `uv sync --locked --group build --reinstall-package kora --offline` succeeded even with `KORA_BUILD_OFFLINE=invalid`, confirming editable setup skips frontend option parsing and invocation. Source and packaging checks passed locally on both Python endpoints before CI integration; the Linux lanes and complete-workflow timing still require actual GitHub run evidence.

## Fresh-cache hosted CI correction

Checked on **2026-10-18**. The first [hosted scaffold run](https://github.com/realkarych/kora/actions/runs/39133378036) exposed two setup requirements masked by the local tool installation and populated caches. The Mermaid job exited with `xargs: rg: No such file or directory` before parsing. It now explicitly installs ripgrep through Ubuntu's package manager before the existing NUL-delimited discovery and rendering commands.

All three scaffold lanes installed Hatchling successfully during locked sync, then failed offline isolated build resolution because Hatchling was unavailable in the resolver cache. A local reproduction with new temporary `UV_CACHE_DIR` and `UV_PROJECT_ENVIRONMENT` directories confirmed the failure: `uv sync --locked --group build --no-install-project` installed the locked artifacts, but the identical offline build exited 2 before producing an sdist. The cache contained the wheel artifacts without the Simple API package metadata needed to resolve isolated build requirements. The pinned [uv registry client](https://github.com/astral-sh/uv/blob/0.13.1/crates/uv-client/src/registry_client.rs#L519) reads that metadata separately and allows cached metadata offline; [uv's cache documentation](https://docs.astral.sh/uv/concepts/cache/) describes registry and artifact caching.

The setup correction exports the build group with hashes from the lockfile, then runs `uv pip install --python "$PWD/.venv/bin/python" --target "$KORA_BUILD_CONSTRAINT_DIR/backend" --require-hashes --requirements "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"` online. The [pip install reference](https://docs.astral.sh/uv/reference/cli/#uv-pip-install) documents these options. The disposable target ensures resolution occurs even when the same requirements are already installed in the project environment. It adds no project dependency and supplies no packages to the subsequent build; its purpose is to populate registry metadata and artifacts in uv's cache.

In the fresh-cache reproduction, this single added command resolved six locked build packages in 329 ms and installed them into the target in 8 ms, without downloading further artifacts. Six previously absent Simple API metadata entries appeared: Hatchling, packaging, pathspec, pluggy, tomlkit and trove-classifiers. Repeating the unchanged `KORA_BUILD_OFFLINE=1 uv build --offline` command with the exported build constraints and `--require-hashes --clear` then built the sdist and a wheel from that sdist successfully. The frontend hook reused all 161 packages with zero downloads. Build isolation, hashes and offline resolution remained enabled. CI passes the temporary constraints directory between steps through `GITHUB_ENV`, so the offline build consumes exactly the requirements used to prime its cache. Actual corrected hosted runs and cold/warm workflow durations still require remote evidence.

## pnpm bootstrap lockfile correction

Checked on **2026-10-18**. The [next hosted run](https://github.com/realkarych/kora/actions/runs/39133953802) passed Mermaid and all three scaffold lanes' source checks, offline isolated builds, packaging tests and editable setup. The final dependency-file diff rejected changes to `web/pnpm-lock.yaml`: every lane added `@pnpm/exe@12.14.0` package-manager metadata alongside the existing `pnpm@12.14.0` entry.

The original action's [pinned bootstrap lockfile](https://github.com/pnpm/action-setup/blob/71c92474e7e4f5bca283fb17ef80fba9cdb2b4b1/src/install-pnpm/bootstrap/pnpm-lock.json) installs pnpm 11.0.0-rc.2. Its [installer](https://github.com/pnpm/action-setup/blob/71c92474e7e4f5bca283fb17ef80fba9cdb2b4b1/src/install-pnpm/run.ts) skips self-update when the project manifest alone specifies a version. That legacy bootstrap switches to the requested pnpm through `@pnpm/exe`, adding the extra lockfile metadata during frozen installation. Running the original committed bootstrap in a disposable directory against a clean copy of `web/` reproduced exactly the hosted diff. Earlier local verification used the native pnpm 12.14.0 binary directly, so it did not exercise this setup boundary.

The [v6.1.0 release](https://github.com/pnpm/action-setup/releases/tag/v6.1.0) explicitly adds pnpm v12 support. Its [pinned installer](https://github.com/pnpm/action-setup/blob/ea17c68df8912ef543352723c149a84f56e3d413/src/install-pnpm/run.ts) selects a native pnpm 12 bootstrap and updates it to the exact manifest version before project commands. Executing that release's real bundled action in a new temporary installation self-updated pnpm 12.3.4 to 12.14.0. Frozen installation into a second clean `web/` copy succeeded and left its lockfile byte-for-byte unchanged. The action pin changes to that verified commit; manifest-based exact version selection, frozen installation, store cache ownership and the dependency-file diff remain unchanged. No project dependency or lockfile change is needed.

## Scaffold test shards, 2026-10-26

This section records the first layout, which ran on its own revision. The merge subsection below supersedes its job names, its bash admission step and its `database` group.

Checked on 2026-10-26 against `master` at `442a2b699883172e20fb5651d2d299fe59b1d343`, under the [issue #268 plan](https://github.com/realkarych/kora/issues/268#issuecomment-6410429531).

At that revision the slowest Scaffold job, Linux with Python 3.11.17, took 8:50 to 9:32 against its 10-minute job timeout, because one runner ran all three ordinary-suite groups of #250. Master run [40278849670](https://github.com/realkarych/kora/actions/runs/40278849670) recorded these group times for Linux 3.11 / Linux 3.15 / macOS 3.11: `first` 205 / 145 / 135 s, `second` 186 / 133 / 108 s and `other` 439 / 295 / 338 s. Within `other`, `tests/measurements` and `tests/db` took 157 s and 98 s on Linux 3.11.

The `scaffold-shard` job now runs six matrix entries, two shards for each platform and interpreter:

- `conformance` runs the unchanged `first` and `second` groups, the source checks and the offline build.
- `remainder` runs three concurrent groups: `measurements` (`tests/measurements`), `database` (`tests/db`) and `other` (all remaining tests).
- Both shards collect the complete suite and all five groups. `tests.support.ordinary_inventory` must prove nonempty, pairwise disjoint groups whose union equals the full collection before any child starts.
- Both shards keep the final environment and dependency-file check, so the measurement jobs' `final_sync` equality is unchanged.

The required names `Scaffold (<os>, Python <ver>)` now come from the aggregate `scaffold` job. Each leg requires `needs.scaffold-shard.result == success`, which holds only when all six shards succeed. Each leg also downloads its platform's `ordinary-tests-<os>-python-<ver>-conformance` and `-remainder` artifacts into separate directories and requires the exact evidence listing of each. Every file must be regular, non-symlink and nonempty, and the two `inventory.json` files must be byte-equal.

This check runs in a job that no shard controls. If the `remainder` entries were dropped from the matrix, the architecture tests that pin the matrix would never run, but every aggregate leg would still fail. Each required name therefore now means that every platform's shards succeeded and that its own two shards produced matching evidence. The gate's `needs: scaffold` is unchanged.

### Merge with the storage lanes, 2026-10-26

[Issue #258](https://github.com/realkarych/kora/issues/258) independently split each platform into a `general` and a `storage` lane. It replaced the single Scaffold job with `scaffold-tests` and a `scaffold` join running `tests.scaffold_evidence verify` over hashed, provenance-bound receipts. On master after #294, the serial `general` lane took 5:35 to 9:45, and the pipeline took 10:21 to 15:05.

Under the [approved plan amendment](https://github.com/realkarych/kora/issues/268#issuecomment-6423535663), the merged layout works as follows:

- **Lanes.** Each platform runs three lanes: `conformance`, `remainder` and the unchanged `storage`. The two general shards use the same general pytest arguments.
- **Join.** The verifier of #258 is the single join, extended to nine lanes, and the bash admission described above is removed. For each platform it requires:
  - each lane's receipt, with its hashes and provenance;
  - each executed group's JUnit equal to its collection;
  - disjoint groups whose union equals the general selection in both shards;
  - identical `inventory.json` and group collections in both shards;
  - general and storage partitioning the complete collection.
- **`test_ir_measurement_evidence.py`.** #258 had moved it into `second` to balance three cohorts on one runner. It returns to the `measurements` group of `remainder`.
- **Frontend steps.** The frontend install runs in both general shards, because `tests/browser` needs Node and the `web` dependencies. The source checks and the build run only in `conformance`.

The samples in the later "Scaffold test shard samples" section were taken before this merge, on the layout with two shards per platform. They are kept as the record of that revision.

#### Merged-head samples and the rebalance, 2026-10-26

These samples were taken serially with `gh run rerun`, under the same sample rules. Each pipeline is timed from the attempt's `run_started_at`, and shard times are job durations.

- The base is master run [40370322638](https://github.com/realkarych/kora/actions/runs/40370322638) at `05f4d7a`, attempts 2 to 4.
- The head samples before the rebalance come from pull request run [40376563452](https://github.com/realkarych/kora/actions/runs/40376563452) at `95ed5d0`.
- The head samples after it come from run [40387948096](https://github.com/realkarych/kora/actions/runs/40387948096) at `c7330b6`.

| sample | pipeline | longest Scaffold test job | last gate input | result |
| --- | --- | --- | --- | --- |
| base attempt 2 | 9:01 | macOS `general`, 8:07 | Scaffold Linux 3.15 | failure |
| base attempt 3 | at least 10:24 | macOS `general`, 8:57 | Scaffold Linux 3.11, +9:23 | cancelled |
| base attempt 4 | 10:14 | macOS `general`, 9:23 | Scaffold Linux 3.11, +9:47 | success |
| `95ed5d0` attempt 2 | 8:31 | macOS `remainder`, 6:28 | IR model measurements (0), +8:06 | failure |
| `95ed5d0` attempt 3 | 7:38 | macOS `remainder`, 6:03 | logging job, +7:07 | success |
| `95ed5d0` attempt 4 | 7:34 | Linux 3.11 `remainder`, 6:02 | IR model measurements (0), +7:04 | success |
| `c7330b6` attempt 1 (push observation) | 6:43 | macOS `remainder`, 4:53 | Scaffold macOS, +6:20 | success |
| `c7330b6` attempt 2 | 8:38 | macOS `remainder`, 5:16 | IR model measurements (0), +8:12 | success |
| `c7330b6` attempt 3 | 6:26 | macOS `conformance`, 4:49 | pricing job, +6:00 | success |
| `c7330b6` attempt 4 | 8:50 | macOS `remainder`, 6:37 | IR model measurements (0), +8:24 | success |

**Failures**, kept as they occurred and not rerun:

- **Base attempt 2.** On macOS `general`, `tests/conformance/test_lifetime_helper_phases.py::test_selected_helper_keeps_original_scope_and_term_kill_phases[acquire-16]` failed with a `ChannelError` deadline.
- **Base attempt 3.** Console browser checks reached their 10-minute timeout.
- **`95ed5d0` attempt 2.** `tests/plugins/storage/test_pressure_controller.py::test_unknown_image_identity_refuses_cleanup_before_detach` failed on the unchanged macOS `storage` lane, and the joins refused that attempt.

**The rebalance.** At `95ed5d0`, every head sample missed the 6:00 per-shard target in `remainder`. There, `other` took 293 to 317 s, mostly in `tests/architecture`, `tests/plugins` and the new `tests/cli`. The one approved rebalance round replaced the `database` group with `integration`: `tests/db`, `tests/cli` and `tests/plugins` without `tests/plugins/storage`. Locally on macOS, the `remainder` shard went from 213 to 141 s, and the five groups are now 174 + 377 + 344 + 1,273 + 3,835 = 6,003.

**Results at `c7330b6`:**

- **Pipeline.** The median head pipeline is 8:38, against a base median of 10:14. No `Scaffold (…)` join finished last among the gate's inputs in any rerun sample. In the push observation the macOS join was last, at +6:20, and the pipeline took 6:43.
- **Coverage.** The final head adds 182 node IDs and removes 14 on every platform, all in the changed architecture test files. It collects 6,224 cases on Linux and 6,156 on macOS.

**Remaining diagnostic.** In `c7330b6` attempt 4, macOS `remainder` took 6:37 against the 6:00 per-shard target of #268.

- No other run on the account overlapped it.
- All three of its groups ran at about twice their usual time: `integration` 303 s, against 125 s locally, `measurements` 246 s and `other` 232 s. In the same attempt, macOS `conformance` took 3:25.
- The hard 10-minute job timeout and every required result held.

The project owner [accepted this miss as a recorded diagnostic](https://github.com/realkarych/kora/issues/268#issuecomment-6426246098). It is not a passing guarantee. Revisit the grouping when either of these happens:

- any Scaffold test job exceeds 7:00, or exceeds 6:00 in two consecutive master runs;
- #271 compares its measurement jobs against the slowest shard.

### Scaffold test shard samples, 2026-10-26

These samples were taken under the sample rules of the [issue #268 plan](https://github.com/realkarych/kora/issues/268#issuecomment-6410429531):

- Base is master run [40278849670](https://github.com/realkarych/kora/actions/runs/40278849670) at `442a2b6`, attempts 2 to 4.
- Head is pull request run [40286071298](https://github.com/realkarych/kora/actions/runs/40286071298) at `35fc97c`, attempts 2 to 5. Attempt 1 is the push run, kept as an observation with CodeQL, Gitleaks and OSV running alongside.
- The samples ran serially and interleaved. Each pipeline is timed from the attempt's `run_started_at` to its last job's end.

| sample | pipeline | longest shard | latest aggregate leg | last gate input | result |
| --- | --- | --- | --- | --- | --- |
| head attempt 1 (push observation) | 7:15 | 5:39, macOS `remainder` | +6:14 | logging job, +6:52 | success |
| base attempt 2 | 9:40 | — | — | Scaffold Linux 3.11, +9:21 | failure |
| head attempt 2 | 8:20 | 4:20 | +4:34 | IR model measurements (0), +7:59 | success |
| base attempt 3 | at least 10:36 | — | — | — | cancelled |
| head attempt 3 | 7:11 | 5:22, macOS `conformance` | +5:38 | logging job, +6:53 | success |
| base attempt 4 | 9:48 | — | — | Scaffold macOS, +9:26 | success |
| head attempt 4 (replaced) | 10:02 | 4:35 | +5:34 | IR model measurements (0), +9:34 | success |
| head attempt 5 | 7:14 | 4:36 | +4:46 | IR model measurements (0), +6:49 | success |

**Kept head samples** (attempts 2, 3 and 5):

- Median pipeline 7:14.
- Longest shard 5:22.
- No aggregate leg finished last among the gate's inputs.
- The aggregate added 9 to 12 s after the last shard.

**Base samples:** median 9:48.

**Replacement.** Head attempt 4 overlapped an unrelated pull request run, [40291377565](https://github.com/realkarych/kora/actions/runs/40291377565), from 08:18:45 to 08:28:56 UTC. Jobs queued for up to 98 s: IR model measurements (0) 98 s, IR (2) 86 s, Mermaid 85 s, the macOS shards 47 to 51 s. Attempt 5 replaced it as the one replacement allowed for an outside cause. Keeping attempt 4 instead would give a median of 8:20, so the replacement does not decide the outcome. Base attempt 4 overlapped the same run for its last 5:28, but it queued no job for more than 5 s, so it was kept.

**Base failures**, kept as they occurred and not rerun:

- **Attempt 2** failed `tests/conformance/test_lifetime_rpc_concurrency.py::test_real_client_subtree_close_fences_stalled_acquire_before_reply[16-tree]` with `CancelledError` on Linux 3.15. That happened in the old layout, where three processes share one runner.
- **Attempt 3** was cancelled when Scaffold macOS reached its 10-minute job timeout. Console browser checks also hit its timeout in "Install pinned browser tools", so 10:36 is a lower bound.

The "Install pinned browser tools" step took 466 s in head attempt 4 and 594 s in base attempt 3, against about 1:30 for the whole job normally. It appeared on both sides and does not depend on the shards.

**Per-group evidence.** Reruns reuse the artifact names, and the run keeps only the latest set. Per-group JUnit therefore survives for head attempt 1, retrieved before the reruns, and for attempt 5. Both have no failures or errors, and both shards of each platform have identical inventories: 5,540 cases on Linux and 5,473 on macOS. Against base `full.collection`, every platform differs by 41 added and 4 removed node IDs, all in the three changed architecture test files.

**Contention diagnostic.** Per-group pytest times in head attempts 1 and 5, against base `442a2b6`:

| runner | group | base | head attempt 1 | head attempt 5 |
| --- | --- | --- | --- | --- |
| macOS, 3 cores | `measurements` | 126 s | 175 s | 147 s |
| macOS | `database` | 31 s | 46 s | 37 s |
| macOS | `other` | about 176 s | 258 s | 213 s |
| Linux 3.11 | `first` | 205 s | 173 s | 166 s |
| Linux 3.11 | `second` | 186 s | 156 s | 148 s |

The lifetime conformance groups got faster on Linux, so they now see less contention. On macOS, three processes share three cores, and the `remainder` groups ran 17 to 48 % slower than in the base. That shard took 3:52 to 5:39, and so it is the one that limits the layout. Neither approved rebalance lever removes work from that runner. Revisit the grouping if any macOS shard exceeds 6:00, or when #271 compares its measurement jobs against the slowest shard.

### Installed conformance placement, 2026-10-27

This subsection supersedes two statements of the merge subsection above: the `storage` lane is no longer unchanged, and the frontend install now runs in all three lanes, not only in both general shards.

Checked on 2026-10-27 at PR #322 head `3a8cf06a1543d9b3ebef798cd0f2733b56162e7c`, base `abba434dcf4d34b54e071a3e7f3dbef371b3907b`, under the [issue #215 capacity amendment](https://github.com/realkarych/kora/issues/215#issuecomment-6442825462).

`tests/conformance/test_installed_conformance.py` holds 50 installed-child cases. Its module fixture builds the core wheel, including the frontend build, and installs it with a fixture provider into a fresh environment. Since #317 it ran in the `second` cohort of every `conformance` lane.

**Observed limit.** Before #317, master run [40453572343](https://github.com/realkarych/kora/actions/runs/40453572343) at `ace0850` took 278 to 340 s for the three `conformance` jobs. In a mixed-branch sample of twelve runs from `e1eae5a` to `56ea004` (40468386184, 40469734219, 40472816570, 40472848649, 40477396676, 40477773146, 40479106183, 40483593769, 40484132965, 40487256218, 40488245353, 40491028077), the macOS `conformance` job took 482 to 621 s: three successes, seven cancellations and two failures. Storage jobs took 61 to 87 s on Linux and 83 to 128 s on macOS. All three `conformance` jobs reached the ten-minute limit in run [40484132965](https://github.com/realkarych/kora/actions/runs/40484132965) at `3c5c6de`. In run [40503180264](https://github.com/realkarych/kora/actions/runs/40503180264) at `3a8cf06`, Linux 3.11 was cancelled at 608 s, Linux 3.15 took 581 s and macOS 546 s. Appending the module to `first` in #320 raised macOS `first` from 204.766 s in run [40472816570](https://github.com/realkarych/kora/actions/runs/40472816570) to 389.519 s in run [40477396676](https://github.com/realkarych/kora/actions/runs/40477396676). That difference, and the corresponding Linux 3.15 figure of about 255 s, compares two failing runs with different cohort contents and includes waits to the 30-second deadline. The Linux 3.11 growth from 420 s at `56ea004` to 608 s at `3a8cf06` remains unattributed.

**Change.** Each `storage` lane runs the module after the storage tests in the same pytest process; the general arguments and the `second` cohort ignore it. Node, pnpm and the locked frontend install run in every lane, because the wheel build needs them, and the storage completion check requires a successful frontend install. The nine jobs, test identities, collection/JUnit/provenance joins, protected names, artifact names, cache suffixes and every job, test, helper and cleanup deadline are unchanged. `tests/cli/test_conformance.py` stays in the `remainder` integration group.

**Security.** Storage lanes now restore and save the shared pnpm store, run the pinned setup-node and pnpm actions and a frozen install with dependency build scripts disabled, and execute tsc and Vite during the installed wheel build. The storage jobs now also contact the Node distribution host and the npm registry, which widens their network use but not their authority. Workflow permissions, secrets, credential persistence and cache scope are unchanged. The storage lanes lose their earlier property of running no Node toolchain; because all scaffold jobs already share these permissions and cache scope, no trust boundary is added.

**Local diagnostics.** Single runs on Linux with 2 CPUs, CPython 3.11.17 and SQLite, as root in a container:

| revision | selection | result | pytest time |
| --- | --- | --- | --- |
| `56ea004` | installed module alone | 49 passed | 288.61 s |
| `56ea004` | current storage selection | 157 passed, 1 failed | 74.40 s |
| `3a8cf06` | proposed storage selection | 207 passed, 1 failed | 373.76 s |
| `56ea004` | `tests/cli/test_conformance.py` alone | 28 passed | 215.45 s |

At `3a8cf06`, JUnit attributes 75.35 s to the storage tests and 297.95 s to the 50 installed cases running after them. The one failure, `test_user_owned_sticky_ancestor_is_refused`, comes from running as root, which makes its sticky directory root-owned and trusted. For the same reason these runs did not exercise the storage provider's foreign-owner and foreign-ancestor refusals; hosted runners, which do not run tests as root, do. Interaction with leftover macOS `hdiutil` state is unverified. These observations come from a smaller, shared host. They are not hosted predictions or guarantees.

**Alternatives not taken.** A fourth lane per environment would not shorten the longest job. It would add three jobs, one of them on macOS, change the receipts, verifier, join and documentation from nine lanes to twelve, and overlap #324's own fourth lane. Moving `tests/cli/test_conformance.py` into storage as well would push macOS storage to about 450 to 525 s. Appending the module to `first` failed in #320. A third concurrent cohort would add contention on the three-core macOS runner, where installed-storage cases already missed their deadline under contention in #318.

**Hypothesis for the implementing run.** Compared with `3a8cf06` (tests steps of 456, 524 and 498 s on macOS, Linux 3.11 and Linux 3.15), each `conformance` tests step should fall by roughly the module's share, and each storage job should stay under 7:00. A changed lane above 7:00 opens a follow-up. This is a diagnostic, not a gate. The hosted result is recorded in the PR #322 description.

**Revisit** when any storage job exceeds 7:00, or exceeds 6:00 in two consecutive master runs; when the macOS pressure failures of #302 are attributed to state shared with later tests in the same process; or when a generalized lane map lowers the cost of a separate installed lane.

### Remainder headroom, 2026-10-27

This subsection supersedes four statements above:

- The `measurements` group defined in the first shard layout (line 150) now also contains `tests/db` and the non-storage `tests/plugins`.
- The `integration` group defined by the rebalance (line 203) now contains only `tests/cli`.
- The installed conformance module fixture (line 278) no longer builds the core wheel. It copies the wheel that its pytest process builds once.
- For the macOS `remainder` job, the revisit conditions at the end of this subsection replace two 6:00 clauses: the one in the rebalance's revisit list (line 218) and the macOS one in the shard samples (line 270). Both clauses stay in force for the Linux `remainder` jobs and for the other lanes. #327 tracks macOS `storage`, and #328 tracks macOS `conformance`.

Checked on 2026-10-27 against `master` at `4e75a73bdc492ab471dcdc27bbcd317b8e6943b9`, under the [issue #326 plan](https://github.com/realkarych/kora/issues/326#issuecomment-6448899199).

**Trigger.** The revisit condition of the rebalance record fired after two changes: #322 added `tests/cli/test_conformance.py` to `integration`, and #256 enlarged `test_ir_measurement_evidence.py` in `measurements`. In master run [40469734219](https://github.com/realkarych/kora/actions/runs/40469734219) at `abba434`, the `remainder` jobs took 279, 302 and 266 s on macOS, Linux 3.11 and Linux 3.15. In master runs [40517496683](https://github.com/realkarych/kora/actions/runs/40517496683), [40520743424](https://github.com/realkarych/kora/actions/runs/40520743424) and [40525735804](https://github.com/realkarych/kora/actions/runs/40525735804), they took 510, 427 and 477 s on macOS, 347, 364 and 455 s on Linux 3.11, and 361, 429 and 439 s on Linux 3.15. Group times in the hosted logs were not retrieved.

**Local diagnostics.** Single runs on shared x86_64 Linux with 2 vCPUs, CPython 3.11.17 and SQLite only, as root. Groups were launched without `uv run`, the collection prefix or the record step.

| selection | before | after |
| --- | --- | --- |
| `test_ir_measurement_evidence.py` alone | 271.15 s | 11.87 s |
| longest group alone | `integration`, 373.22 s | `integration` (`tests/cli`), 269.70 s |
| new `measurements` group alone | — | 140.39 s |
| three groups concurrently | 485.4 s | 343.2 s |

- **Profile.** A `cProfile` run attributed about 94 % of the evidence module's time to three helpers that regenerate identical expected inventories. The evidence validator made about half of those calls.
- **Join.** On identical synthetic shards, `python -m tests.measurements.ir_measurement_evidence` produced byte-identical output before and after the change.
- **Core wheel.** `integration` built it twice before the change and once after; two consecutive builds were byte-identical.
- **Deadline-sensitive case.** The 18-case installed storage case of `test_installed_provider_supports_local_tooling` uses about 73 % of its 60-second command timeout alone. It reached the timeout in both concurrent runs of the old grouping. In the new grouping it ran after `measurements` had finished.
- **Root failures.** Six failures come from running as root or are unexplained on this host, and pass in hosted runs.

These observations are not hosted predictions or guarantees.

**Change.**

- **Validator.** The evidence validator `tests/measurements/ir_measurement_evidence.py` computes four values once per process: the cost-case identities (a payload-free `AccountingCase` projection), the schema manifest, the schema model names, and the VcsRef case names with their manifest. Its evidence tests reuse them. Every comparison keeps its operands. The workload modules that measured children import are unchanged.
- **Shared wheel.** A session-scoped `core_wheel` fixture in `tests/conftest.py` builds the core wheel once per pytest process. The session-scoped `provider_tooling_wheelhouse` copies it once into the session wheelhouse, which the listing tests share as before. `installed_conformance` copies it into each module's own wheelhouse. Every installed module or test still creates its own environment.
- **Grouping.** `measurements` runs `tests/measurements`, `tests/db` and the non-storage `tests/plugins`, and `integration` runs `tests/cli`.

The nine jobs, the five group names, three children in `remainder`, and the collection, JUnit and provenance joins are unchanged. So are the IR union counts, the protected and artifact names, the cache suffixes, and every job, test, helper and cleanup deadline. Existing test identities are unchanged, and three tests are added.

**Projection, a diagnostic.** Local factors of 0.707 to 0.723, applied to the samples above, project these `remainder` jobs:

- macOS 322 to 426 s, of which master samples 322 to 388 s;
- Linux 3.11 261 to 345 s;
- Linux 3.15 257 to 333 s.

The slowest macOS sample, at 556 s, projects to the 7:00 line. The 6:00 condition is expected to keep firing on macOS. `tests/cli`, at about 270 s alone, is now the longest group, and no grouping by whole directories goes below it. The hosted result is recorded in the description of PR #329.

**Revisit** in any of these cases:

- any `remainder` job exceeds 7:00;
- macOS `remainder` exceeds 6:30 in two consecutive master runs;
- a change moves source checks or builds into `remainder`;
- a moved or co-scheduled test fails a deadline in a hosted run;
- the expected IR evidence values stop being deterministic within a process.

### Required measurement balance, 2026-10-29

Issue [#271](https://github.com/realkarych/kora/issues/271) preserves six IR runners and moves provider-loading/protocol-model work into `provider-protocol-measurements`. The existing resource job key and check name remain for compatibility; it retains logging, metadata and all full-disk steps. Both jobs own locked setup, source receipts, final environment verification, cancellation refusal and complete required-step proofs. CEL requires both job results and both proofs. Artifact names, workloads and hard limits are unchanged. Scaffold source/build assignment stays unchanged; PR #301 owns its separate placement change.

The six partitions assign whole existing tests and validation-name families. Shard 0 retains configuration worker 0, VcsRef action/import, diagnostics and failure cases, plus dense validation. Shard 1 owns configuration workers 1/2, accounting timing and schema validation. Shard 2 owns accounting lifetime and control validation. Shards 3/4 pair schema workers 0/1 with remaining and configuration/document validation. Shard 5 pairs schema worker 2 with VcsRef lifetime and every other validation name, including unmatched future prefixes. Literal full parameter prefixes prevent substring overlap. Real collection and evidence checks require exact node, gate and sample-multiplicity ownership.

| Shard | Tests | Gates | Samples |
|---|---:|---:|---:|
| 0 | 22 | 132 | 2,517 |
| 1 | 16 | 183 | 2,675 |
| 2 | 57 | 639 | 848 |
| 3 | 65 | 68 | 964 |
| 4 | 114 | 117 | 1,914 |
| 5 | 85 | 88 | 1,750 |
| Complete union | 359 | 1,227 | 10,668 |

Comparison against base `e6c0730f1ebdd0e7d809e8f8a7780034cb0aba97` requires identical node/gate identities and sample Counter keys/multiplicities, not only totals. No measurement function, fixture, warm-up, cycle, numerical assertion, child deadline, output bound or resource-release check changes.

The [implementation plan](https://github.com/realkarych/kora/issues/271#issuecomment-6488313379) records source/proof ownership. The owner authorized implementation and ordinary shipping of this scope; this does not approve reduced acceptance. Independent correctness, security and performance design reviews found no blocking defect; source and executable acceptance still require their own evidence.

#### Sampling and retained outcomes

The owner-approved [MVP verification amendment](bounded-mvp-verification-2026-10-29.md) replaces the inherited [#268 method](https://github.com/realkarych/kora/issues/268#issuecomment-6410429531) and its repeated whole-workflow campaign. One complete successful final-head CI execution and exact workload/evidence preservation establish merge acceptance. Retain the actual revision, all outcomes and pipeline duration from attempt `run_started_at` to the last job completion, including queueing and dependent jobs. Historical failures remain failed; no unchanged rerun is selected for a favorable result.

Shard durations, imbalance and measurement-versus-Scaffold comparisons are diagnostics against the existing ten-minute feedback target. The former 30% imbalance, 450-second median pipeline and Scaffold-plus-30-second conditions no longer block merge. All mandatory final-head results, component assertions and hard deadlines remain required.

Pre-change [CI 40745060259](https://github.com/realkarych/kora/actions/runs/40745060259), at `e6c0730`, observed IR jobs 496/225/250/206/210/172 seconds and resource time 388 seconds. This single run motivates packing and establishes no comparison acceptance. It failed macOS `test_storage_case_runner_retains_failed_domain[task]`, tracked by #408 and existing PR #409; its short failed conformance duration, skipped build and refused joins are not healthy headroom evidence. #301's historical native-storage failures also retain their outcomes. Neither repair is part of #271.

After accepted #409 merged into base `cb9b882`, the initial split [CI 40822802978](https://github.com/realkarych/kora/actions/runs/40822802978/attempts/1), at `5e5603e`, passed all 33 jobs. Its pipeline took 3,344 seconds under external account contention; it is separate from the comparison campaign. IR jobs took 221/156/403/240/226/273 seconds. Retained JUnit attributes 213.925 seconds of shard 2 to accounting lifetime and 142.307 seconds to configuration worker 2; shard 1's dense validation takes 18.036 seconds. The [bounded packing adjustment](https://github.com/realkarych/kora/issues/271#issuecomment-6489464942) moves the complete configuration worker 2 to shard 1 and dense validation to shard 0, yielding the partition above. Single-run estimates of 239/280/261/240/226/273 seconds guide this adjustment and establish no qualifying median. Workloads, isolation and the sampling method stay unchanged.

Hosted comparison and final-head acceptance remain pending until their revision-matched runs and artifacts are recorded. Static balance or local passing collection/evidence tests establish no hosted performance guarantee.
