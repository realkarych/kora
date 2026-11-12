# Static-check jobs, 2026-11-12

Checked on 2026-11-12 for [issue #1414](https://github.com/realkarych/kora/issues/1414), part of [issue #1347](https://github.com/realkarych/kora/issues/1347). The numbers are hosted-runner observations from single runs. They are not a portable benchmark and set no gate.

## Measured static-step durations

The `Check Python and frontend sources` step ran `uv lock --check`, the Ruff format and lint checks, Pyright, and the frontend typecheck, lint and build. It ran inside the `local-runtime` scaffold lane of each environment, before that lane's tests, under the lane job's ten-minute deadline.

| Observation | Value |
|---|---|
| macOS step duration in the 30 most recent completed `CI` runs, when the step completed | 134–265 s |
| Pyright alone on macOS in [run 43125567561](https://github.com/realkarych/kora/actions/runs/43125567561) (PR #1370), 00:39:39–00:47:30Z | 7 min 51 s, 0 errors |
| Outcome of that run's macOS local-runtime lane | reached the ten-minute deadline while building the frontend; no local-runtime test ran |

The Pyright wrapper runs its bundled Pyright with the job's Node, so the slow run reflects runner speed, not a download. Pyright has to run on macOS: it treats `sys.platform` branches for other platforms as unreachable and does not check them, so a Linux run would leave macOS-only code unchecked.

## Owner decision and placement

On 2026-11-12 the project owner decided that the static checks run in their own job per environment: macOS with Python 3.11.17, Ubuntu with Python 3.11.17 and Ubuntu with Python 3.15.0. Each job keeps the unchanged ten-minute deadline and the unchanged commands, and the protected `Scaffold (…)` status names stay unchanged.

The planning job emits a `static-checks` row first among each environment's rows of its platform's lane matrix. The row reuses the lane job's checkout, acquisition cache, verified Node and uv/Python setup, locked installs, final dependency check and completion guard. Its guard requires the static checks to succeed, and requires the test step and the evidence upload to be skipped. The protected joins require it through the shared evidence job, exactly as they require the lanes.

The macOS row runs in the macOS lane matrix, which starts at most two jobs at a time. This keeps the [issue #1278](https://github.com/realkarych/kora/issues/1278) rule of at most two macOS scaffold jobs at once, and no queued job is ever cancelled.

## Expected cost and diagnostics

Each run gains three jobs, one of them on macOS. On macOS, the static-check job adds setup and dependency installation, about one to two minutes on the observed runs, plus a static step of the duration above. The local-runtime lane loses the static step. With two macOS slots, the macOS wall-clock of a run can grow by a few minutes, more when a runner is slow to be acquired. This and the static-check job durations in each environment are diagnostics against the ten-minute feedback target, not gates.

Revisit when a static-check job approaches its ten-minute deadline, or when the macOS wall-clock regularly misses the feedback target because of the extra macOS job.
