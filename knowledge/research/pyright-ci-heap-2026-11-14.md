# Pyright CI heap allowance, 2026-11-14

## Failure and approved change

The macOS static-check [job 132466001079](https://github.com/realkarych/kora/actions/runs/43600920540/job/132466001079), requested head `3bab8a2af73944803957ef97bff011981b10ea20`, failed while running Pyright 1.1.414 with Node 24.22.0 on `macos-15-arm64`. After about 135 seconds, Node reported `Ineffective mark-compacts near heap limit`; its last reported collection used about 1,996.5 MB in a 2,068.5 MB heap. Ruff and lock checks had passed; frontend checks had not run. This is a failed kora check, not an eligible infrastructure rerun.

On 2026-11-14, the owner [approved](https://github.com/realkarych/kora/issues/1541#issuecomment-6880913464) a process-scoped `NODE_OPTIONS=--max-old-space-size=4096` setting for the three existing Pyright command blocks in `.github/workflows/ci.yml`. The active jobs continue to check macOS with Python 3.11.17 and Ubuntu with Python 3.11.17 and 3.15.0. The copied block in the macOS general-lane job remains conditional on a static-checks row that its current matrix does not emit.

The setting supplies a 4,096 MiB V8 old-space allowance to Pyright's Node process. It is not a ceiling for total heap or process RSS. No other command receives the setting. The [static-job placement](static-check-jobs-2026-11-12.md), strict configuration, checked sources, tool pins, locked dependencies, supported platforms, concurrency, ten-minute job deadlines, required statuses and failure policy remain unchanged. No retry, exclusion or alternative checker is added.

## Bounded local comparison

The preceding core head `885fb12565fe6e3771ce3c188eea9fe01d65ae82` passed its hosted [macOS static check](https://github.com/realkarych/kora/actions/runs/43600444672/job/132465232970). The hosted checkout trees were checked against the requested heads: core merge `5e2195d96025ea83c44df73b3b0ce528ffc0b3f1` had tree `7e5a7d9bf548eb63d1b01d65739e6cbe3db496bd`; HTTP merge `befb3b40dc68ac2a4351e5fe199cc8a6ec0e9329` had tree `3cae19411a7556d26a052857764b3b943a021c34`. Each tree matched its requested head.

One serialized local comparison per revision and Node version ran `NODE_OPTIONS=--max-old-space-size=2048 uv run --no-sync pyright --stats` on macOS with CPython 3.11.17 and frozen dependencies. No source exclusion or checker configuration changed.

| Node | Revision | Files | Time | Result | Output SHA-256 |
| --- | --- | ---: | ---: | --- | --- |
| 26.13.0 | Core `885fb12` | 1,662 | 36.228 s | Zero-error summary; process exit not retained | `bb2fd61825c91cf8e4134f933876ed7a822877c23fa192557b31a750bb3b4ce1` |
| 26.13.0 | HTTP `3bab8a2` | 1,671 | 35.928 s | Exit 0 | `c3add328a11c4c78576b726f310e531864da5e8af672c05d43999b5f47b3f377` |
| 24.22.0 | Core `885fb12` | 1,662 | 33.095 s | Exit 0 | `76078d34555375756f210bf386dec34bc2ae0be755ff2476ef21bf303b868b59` |
| 24.22.0 | HTTP `3bab8a2` | 1,671 | 36.281 s | Exit 0 | `a5a9e3587c9e35f3ec341ff3721a32cae7e344bc1e857afe22be2adb8387bb9b` |

Node 24.22.0 came from the existing pinned acquisition path. Local total heap was about 2,240 MB, differing from the hosted report of about 2,068 MB. These observations do not reproduce the full hosted environment, identify the cause of its default heap, or clear the failed job. They establish neither a leak diagnosis nor a portable timing or memory guarantee. [Comparison evidence](https://github.com/realkarych/kora/issues/1452#issuecomment-6878390114), [checkout identity](https://github.com/realkarych/kora/issues/1541#issuecomment-6878743449).

## Validation and remaining limits

The existing CI gate, acquisition and no-suppression tests remain required without weaker expectations. Final native CI must execute the same strict checks on all three environments and finish every required job. Success on this small change does not qualify the later heartbeat composition; that revision still needs its own complete CI. The implementing pull request records the exact tested revision and results.

The setting changes capacity, not a numerical acceptance method. Available host memory, Node's other allocations and concurrent work can still cause failure. A future heap exhaustion or static-check deadline failure remains a failure requiring bounded investigation. Ordinary test-lane deadlines and the history probe's 60-second child deadline are outside this repair.
