# Scaffold lane durations, 2026-11-12

This record holds the measured basis of the duration-balanced scaffold lanes under [issue #1348](https://github.com/realkarych/kora/issues/1348), checked on 2026-11-12. The numbers are hosted-runner observations from one run, not a portable benchmark.

## Source run

[Run 43116408596](https://github.com/realkarych/kora/actions/runs/43116408596) is the most recent complete green `CI` run after the daily-lane move of #1397. It tested pull-request merge `f6434603aa88372f77ba1366c16f937f462b7bed` with all fourteen scaffold lanes successful, before balancing. The master run [43116004152](https://github.com/realkarych/kora/actions/runs/43116004152) of #1397 itself was cancelled at the ten-minute deadline in the macOS remainder lane.

`scripts/ci/lane_durations.json` was generated from that run's `scaffold-tests-*` artifacts. It sums the JUnit `time` of every test in the conformance, remainder and integration lanes per module; storage and local-runtime lanes are excluded.

| Environment | General tests | Recorded seconds | Modules | Largest modules |
|---|---:|---:|---:|---|
| macOS 15, Python 3.11.17 | 15,706 | 2,512.9 | 488 | `tests/cli/test_conformance.py` 270.6 s, `tests/conformance/test_conformance_cases_detect_broken_providers.py` 198.0 s |
| Ubuntu 24.04, Python 3.11.17 | 16,643 | 2,884.6 | 488 | `tests/cli/test_conformance.py` 226.9 s, `tests/engine/test_graph_execution_contract.py` 164.6 s |
| Ubuntu 24.04, Python 3.15.0 | 16,643 | 2,696.3 | 488 | `tests/cli/test_conformance.py` 233.9 s, `tests/engine/test_graph_execution_contract.py` 153.7 s |

Lane jobs on the same run, in seconds:

| Lane | macOS 3.11 job / test step | Ubuntu 3.11 job / test step | Ubuntu 3.15 job / test step |
|---|---:|---:|---:|
| conformance | 443 / 409 | 293 / 260 | 260 / 240 |
| remainder | 565 / 505 | 497 / 470 | 489 / 451 |
| integration | | 369 / 342 | 352 / 329 |
| storage | 316 / 272 | 187 / 166 | 228 / 194 |
| local-runtime | 300 / 159 | 275 / 172 | 274 / 168 |

Setup before the test step took 15–33 s, and 91–122 s in the local-runtime lanes, which prepare host fixtures. Each child's JUnit total was 1–17 s below its pytest wall time; the difference is the child's own collection. The slowest children were macOS remainder measurements, 487.9 s recorded, and Ubuntu 3.11 remainder engine, 460.0 s.

## Plan from the table

The planner estimates each test as its module's recorded milliseconds divided by its recorded test count, rounded up. A module without a record uses the environment's mean recorded time per test: 0.160 s on macOS, 0.173 s on Ubuntu 3.11 and 0.162 s on Ubuntu 3.15. The median of the per-module rates is 0.056 s on macOS, so a median default would underestimate a new heavy module by about a factor of three.

With three children per lane and a 270-second target, the planner uses four general lanes in each environment on the source run's collections:

| Environment | Estimated seconds | Lanes | Largest planned child |
|---|---:|---:|---:|
| macOS 15, Python 3.11.17 | 2,521.9 | 4 | 210.2 s |
| Ubuntu 24.04, Python 3.11.17 | 2,893.1 | 4 | 241.1 s |
| Ubuntu 24.04, Python 3.15.0 | 2,705.0 | 4 | 233.9 s |

`tests/cli/test_conformance.py` exceeds the target on macOS, so its tests are packed individually there. A 240-second target would need five lanes on Ubuntu 3.11. Four children per lane would need three lanes per environment for the same twelve children per environment, with one more concurrent child on each runner.

## Node-ID argument files

On 2026-11-12, a local collection on macOS with SQLite selected the same 15,706 general tests as the source run's macOS lanes. All 15,706 node IDs, written one per line to a file and passed as `pytest --collect-only -q @file`, were collected in the same order in 9.4 s, against 10.0 s for the path-based collection. Parameter IDs with spaces, `::`, escaped bytes and lengths up to 4 MiB round-tripped unchanged. A node ID that no longer exists is reported by pytest as `ERROR: not found`, not skipped.

## Reading rule

Recorded time comes from runners with three or four concurrent children and the source run's runner speed. A child that later records more than 25% above its plan indicates a stale table, not a test failure; refresh the table from a newer green run. The ten-minute job deadline and every test deadline remain the hard limits.
