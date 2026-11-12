# CI tool acquisition through a digest-keyed cache, 2026-11-12

Checked on **2026-11-12** for [issue #1350](https://github.com/realkarych/kora/issues/1350), part of [issue #1347](https://github.com/realkarych/kora/issues/1347). Between 2026-11-06 and 2026-11-11, 2.6% of analysed `CI` attempts failed while downloading a pinned tool. On 2026-11-11, hosted Ubuntu runners could not resolve the Ubuntu package mirrors; `apt-get` failed in two jobs and ran until the job deadline in both Linux local-runtime lanes. The project owner decided that every pinned tool is restored from a GitHub Actions cache keyed by its exact pinned identity, and on a miss downloaded with bounded attempts inside the job deadline. Size and digest verification stays strict, and acquisitions end with a clear error instead of reaching the deadline.

## Helper and bounds

[`scripts/ci/acquire.py`](../../scripts/ci/acquire.py) uses only the Python standard library and curl, and runs under the runner's system `python3`, 3.9 or newer, because macOS jobs call it before setup-uv.

| Constant | Value |
|---|---|
| Attempts | 3 |
| Spacing between attempts | 5 s |
| Connection limit per request | 15 s |
| Time limit per attempt of a pinned download (both requests share it) | 30–60 s, per artifact below |
| Time limit per attempt of a bounded command | 1–150 s, given by the caller |
| Stop of a timed-out command | SIGTERM to its process group, SIGKILL 5 s later |
| Longest accepted redirect target | 8,192 bytes |

`fetch STORE NAME` verifies a file already in the store, such as one restored from the cache, before printing its path. It must be a regular file reached without symbolic links, with the exact size and SHA-256. A mismatch exits 1 with no download. A missing file is downloaded by curl with `--disable`, a private home and temporary directory, `--proto =https`, a byte ceiling equal to the pinned size, and the URL on standard input. Transport failures and non-200 statuses are retried. A size or digest mismatch, a body above the ceiling, a second redirect, or a redirect that is not HTTPS on exactly the admitted host is refused after that attempt. No credential is sent: the inherited environment, curl configuration files and netrc are not used.

`run SECONDS -- COMMAND` runs an idempotent acquisition command with the same attempts and spacing. A command that needs root runs the helper as root, so the helper can stop the command's whole process group.

## Pinned artifacts

| Name | Source | Bytes | SHA-256 | Redirect host | Limit |
|---|---|---:|---|---|---:|
| `node-24.22.0-linux-x64` | `nodejs.org/dist/v24.22.0/node-v24.22.0-linux-x64.tar.gz` | 58,256,361 | `28ea0a438de9a13bd7891fd758eee6c7819ee8af5442dfd8d45bd65e56d4228c` | none | 60 s |
| `node-24.22.0-darwin-arm64` | `nodejs.org/dist/v24.22.0/node-v24.22.0-darwin-arm64.tar.gz` | 53,049,576 | `6a9724d9b50a5f22b4816cded441b5a8d9ddae546191a5e5b7efa5a5e2ee26d5` | none | 60 s |
| `uv-0.13.1-x86_64-unknown-linux-gnu` | uv 0.13.1 GitHub release asset | 16,738,706 | `eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220` | `release-assets.githubusercontent.com` | 30 s |
| `uv-0.13.1-aarch64-apple-darwin` | uv 0.13.1 GitHub release asset | 14,280,170 | `cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2` | `release-assets.githubusercontent.com` | 30 s |
| `cpython-3.11.17-x86_64-unknown-linux-gnu` | python-build-standalone 20261013, `install_only_stripped` | 30,823,613 | `3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522` | `release-assets.githubusercontent.com` | 45 s |
| `cpython-3.11.17-aarch64-apple-darwin` | python-build-standalone 20261013, `install_only_stripped` | 27,000,974 | `5e656e09fc73c7ae10e4df66e6aa253f1f5980509b3be8a242e883d67a5fdf26` | `release-assets.githubusercontent.com` | 45 s |
| `cpython-3.15.0-x86_64-unknown-linux-gnu` | python-build-standalone 20261013, `install_only_stripped` | 37,656,157 | `69740dcd9b3480b3d53760f2d3b75582adb14fa54434628dea32480ddede542a` | `release-assets.githubusercontent.com` | 45 s |
| `lychee-0.24.2-x86_64-unknown-linux-gnu` | GitHub release asset API, asset 409959156 | 7,626,970 | `1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a` | `release-assets.githubusercontent.com` | 30 s |

The Node and Lychee pins are unchanged from the [secret scan and tool acquisition record](secret-scan-release-asset-2026-11-08.md). The two uv digests equal the `.sha256` files published with the [uv 0.13.1 release](https://github.com/astral-sh/uv/releases/tag/0.13.1). The CPython digests and the 20261013 build equal the entries that uv 0.13.1 uses for these versions and platforms in `crates/uv-python-managed/download-metadata.json` at its release commit `62faed3351cfee2121b3c28b937b1f4b93233a93`. All byte counts and digests were measured on downloaded copies. On 2026-11-12, GitHub release downloads answered with an HTTP 302 redirect to `release-assets.githubusercontent.com`, and nodejs.org answered directly.

## Cache design and scope

Each job has one `actions/cache` step, pinned to [v5.1.0](https://github.com/actions/cache/releases/tag/v5.1.0) at commit `caa296126883cff596d87d8935842f9db880ef25` (2026-06-26). It restores `${{ runner.temp }}/kora-acquisitions` under the exact key `kora-acquisitions-<runner.os>-<runner.arch>-<content label>-<hashFiles('scripts/ci/acquire.py')>`. The file digest covers every pinned source, size and SHA-256, so any pin change selects a new entry. The content label names the archives the job stores, such as `node-uv-python-3.11.17`, so every job that shares a key stores the same set. There are no `restore-keys`. An architecture test refuses any other cache path, key shape or option.

GitHub keeps a cache entry in the scope of the ref that created it. A pull-request run writes only into its own merge-ref scope and can read the base branch's entries; a master push run reads only master entries, and an existing key cannot be overwritten. A pull request therefore cannot place an entry that master restores. Its own entries reach only its own runs, where restored bytes are still verified before use. The cache saves after a successful job only. The Node preparation stores the archive even when the runner image already provides the exact Node entry, so a saved entry is never missing an archive that another runner needs.

## Inventory of run-time acquisitions in `CI`

| Acquisition | Jobs | Treatment | Limit per attempt |
|---|---|---|---:|
| Node 24.22.0 | Scaffold lanes, console browser checks, Markdown lint, Mermaid diagrams | Cached `fetch`; `setup_scaffold_node.sh` extracts it into the exact tool-cache entry that setup-node v7.0.0 looks up before its manifest | 60 s |
| uv 0.13.1 | Every job using setup-uv | Cached `fetch`; `setup_uv_cache.sh` writes the exact `uv/0.13.1/<arch>` tool-cache entry. setup-uv v10.2.0 then skips both its manifest request, which allows 5 s per try, and its own download | 30 s |
| uv-managed CPython | Every job using setup-uv | Cached `fetch` into a `<build>/<file>` layout; `UV_PYTHON_INSTALL_MIRROR=file://…` makes uv install from the store, and uv checks the same SHA-256 | 45 s |
| Lychee 0.24.2 | Links and anchors | Cached `fetch` with the admitted redirect host | 30 s |
| apt `ripgrep`, `nfs-kernel-server` | Mermaid diagrams, local-runtime host fixtures | `apt_install.sh` runs `update` and `install` through `run` as root, with `Acquire::Retries=0` and 20 s HTTP and HTTPS timeouts | 45 s, 60 s |
| Playwright Chromium headless shell and its apt dependencies | Console browser checks | Bounded `run`. Not cached: Playwright publishes no digest that could verify a restored browser | 150 s |
| `markdownlint-cli@0.45.0`, `@mermaid-js/mermaid-cli@12.0.0` with Puppeteer's browser | Markdown lint, Mermaid diagrams | Bounded `run` of `npx --yes <tool> --version`, then the unchanged commands with `npm_config_prefer_offline`. Not cached: no lockfile pins their transitive packages, so there is no exact identity to key | 60 s, 120 s |
| PostgreSQL `postgres:16.15` by manifest digest | Linux test lanes | Unchanged. Docker verifies every layer against the pinned digest and retries layer downloads. A `docker save` archive restored by `docker load` is not guaranteed to carry the repository digest that the pinned reference resolves | — |
| PyPI wheels, npm packages, pnpm bootstrap, `uvx check-jsonschema` | Most jobs | Unchanged: lockfile-keyed caches of setup-uv and pnpm/action-setup, lockfile or registry hashes, and the tools' own bounded retries | — |
| Actions and checkout fetches | All | Unchanged: fetched by commit, with checkout's own retries | — |

The Gitleaks workflow's scanner download, the CodeQL bundle, the OSV scanner and setup-uv in the daily, storage, mutation and conformance-lifetime workflows are outside the `CI` workflow and the analysed failures; they can adopt the helper later.

The worst case of one acquisition is three times its limit plus 10 s, for example 190 s for Node. A resolution failure or an HTTP error ends an attempt within seconds, so an outage like the one on 2026-11-11 now ends with an acquisition error in well under a minute. No job deadline changes.

## Local observations

On macOS arm64 with the system Python 3.9.6 and curl 8.7.1:

- The helper downloaded the uv, CPython 3.11.17 and Lychee archives into an empty store. The unauthenticated Lychee asset request answered HTTP 403 on the first attempt; the second attempt, 5 s later, returned the pinned archive. A repeated `fetch` used the stored archive in 0.07 s without a request.
- `setup_uv_cache.sh 3.11.17`, run against a copied store and an empty tool cache, wrote `uv/0.13.1/aarch64` with its completion marker and the mirror variable. A second run reused the entry. The seeded uv 0.13.1 installed CPython 3.11.17 from the file mirror. After one byte was appended to the stored uv archive, the next run refused it before any extraction.
- The uv 0.13.1 file mirror expects the decoded file name, `cpython-3.11.17+20261013-…`, below the build directory. An archive altered by one byte was refused by uv's own SHA-256 check.

The Linux runner paths, the hosted cache save and restore, and the workflow changes are first exercised by the pull request's own CI run.
