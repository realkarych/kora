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
