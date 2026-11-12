# Scaffold lane durations, 2026-11-12

This record holds the measured basis of the duration-balanced scaffold lanes under [issue #1348](https://github.com/realkarych/kora/issues/1348), checked on 2026-11-12, and the owner decisions of that day. The numbers are hosted-runner observations from single runs, not a portable benchmark.

## Planning basis

[Run 43116408596](https://github.com/realkarych/kora/actions/runs/43116408596) was the most recent complete green `CI` run after the daily-lane move of #1397 when the plan was made. It tested pull-request merge `f6434603aa88372f77ba1366c16f937f462b7bed` with all fourteen scaffold lanes successful, before balancing. The master run [43116004152](https://github.com/realkarych/kora/actions/runs/43116004152) of #1397 itself was cancelled at the ten-minute deadline in the macOS remainder lane. The target and child count were chosen from this run.

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

Setup before the test step took 15–33 s, and 91–122 s in the local-runtime lanes, which prepare host fixtures. Each child's JUnit total was 1–17 s below its pytest wall time; the difference is the child's own collection.

The mean recorded time per test was 0.160 s on macOS, 0.173 s on Ubuntu 3.11 and 0.162 s on Ubuntu 3.15. The median of the per-module rates was 0.056 s on macOS, so a median default for unrecorded modules would underestimate a new heavy module by about a factor of three; the planner uses the mean. With three children per lane and a 270-second target, this run's table gave four general lanes in each environment. A 240-second target would have needed five lanes on Ubuntu 3.11; four children per lane would have needed three lanes per environment for the same twelve children, with one more concurrent child on each runner.

## Committed table

The refresh accepts only the artifacts of one green `master` push run, so the committed `scripts/ci/lane_durations.json` comes from master [run 43132494211](https://github.com/realkarych/kora/actions/runs/43132494211), revision `90968d663e591b24ac7feaed4cc36d4c9d3e4388`, which ran the earlier fourteen lanes:

| Environment | General tests | Recorded seconds | Modules | Mean per test | Largest modules |
|---|---:|---:|---:|---:|---|
| macOS 15, Python 3.11.17 | 15,679 | 2,600.8 | 482 | 0.166 s | `tests/cli/test_conformance.py` 289.3 s, `tests/conformance/test_conformance_cases_detect_broken_providers.py` 227.1 s |
| Ubuntu 24.04, Python 3.11.17 | 16,616 | 2,257.2 | 482 | 0.136 s | `tests/cli/test_conformance.py` 185.2 s, `tests/engine/test_graph_execution_contract.py` 91.0 s |
| Ubuntu 24.04, Python 3.15.0 | 16,616 | 2,414.2 | 482 | 0.145 s | `tests/cli/test_conformance.py` 198.5 s, `tests/engine/test_graph_execution_contract.py` 83.5 s |

Against the planning basis, its totals are 22% lower on Ubuntu 3.11, 10% lower on Ubuntu 3.15 and 4% higher on macOS, which shows the size of run-to-run variance in a single-run table. `tests/cli/test_conformance.py` exceeds the target on macOS, so it is divided into two contiguous chunks of its sorted tests.

## Lane counts are computed per run

The planning job computes each environment's lane count in every run from that run's collection, so the counts and the number of scaffold jobs change between pull requests as tests are added or removed. They are not a fixed layout. As an example, the planning job of revision `1241f24fcec8a8558c53da72e138f8a2abb8ea39`, after master had added about 92 tests, planned:

| Environment | Estimated seconds | Lanes | Largest planned child |
|---|---:|---:|---:|
| Ubuntu 24.04, Python 3.11.17 | 2,278.8 | 3 | 253.2 s |
| Ubuntu 24.04, Python 3.15.0 | 2,436.3 | 4 | 203.0 s |
| macOS 15, Python 3.11.17 | 2,651.7 | 4 | 227.2 s |

That is seventeen scaffold jobs. With three lanes, Ubuntu 3.15's largest child would have been 270.7 s, just over the target, so it took a fourth lane. Three lanes of three children hold at most 2,430 s, so Ubuntu 3.11 moves to four lanes after about 6.6% estimate growth. A planning run on a developer machine collects the Linux selection on that machine and can differ from the Linux runners by a few tests, enough to change a count at such a boundary.

## First balanced run

[Run 43129307528](https://github.com/realkarych/kora/actions/runs/43129307528) tested the first balanced revision with the planning-basis table, four general lanes per environment and tests of an oversized module packed individually. Every lane and test passed; the evidence join refused the run because pytest ordered the parameters of `tests/cel/test_cel_checker_binding.py::test_cel_checker_overloads_evaluate_in_binding` differently in different processes. The parametrisation iterates a frozenset of enum members, whose hashes follow string hash randomisation. The join now compares the sets of collected tests, still refuses duplicates and different membership, and plans from the sorted tests.

Diagnostics of that run, never passing guarantees:

- Recorded child time divided by planned child time ranged from 0.39 to 1.24, with a mean of 0.97 over 36 children. No child exceeded the 125% notice threshold.
- The longest general test step took 334 s (Ubuntu 3.11 `general-3`), the longest general job 361 s and the longest scaffold job 389 s (macOS local-runtime), against the 600 s deadline.
- The planning job queued for 289 s after `Classify verification scope` finished and ran for 51 s; the lanes were created 340 s after the classification. The plan had estimated about one minute.
- The six macOS scaffold jobs ran from 560 s to 2,243 s after run creation, with gaps of up to 284 s between one macOS job ending and the next starting. On green master run 43132494211, the four macOS jobs of the earlier layout ran from 256 s to 1,482 s.

## Owner decisions of 2026-11-12

- The head-revision planner, cross-checked by the head-revision evidence join, replaces the issue's constraint that trusted planning inputs come from the base revision. Review still guards pull requests that edit the planner, the join or the workflow trust tests.
- Blocking for the third acceptance criterion: the deterministic planner bound and green final-head CI. The bound is that every planned child estimate is at most 270 s, except a child holding a single test that exceeds the target by itself, with at most eight lanes per environment. Diagnostic only: lane test-step and job durations, queue time and headroom to the 600 s deadline. They are recorded and never reported as a passing guarantee. A lane job prints a GitHub warning when its test step runs longer than 450 s. Following the performance review of #1411, the warning measures the time since the lane job started, at the end of its test step, so setup and tool acquisition count; it never fails the job.
- The planning job stays after `Classify verification scope`. Planning in parallel with the classification is a possible later change, triggered if the planning latency hurts feedback.

## Reading rule and refresh trigger

Recorded time comes from runners with three or four concurrent children and one run's runner speed. Refresh the table from a newer green master push run when the evidence join's over-plan notice or a lane's 450 s headroom warning appears on a master run, or when a test module is added or removed. The committed table comes from a run that was about 22% faster than the planning basis on Ubuntu 3.11, so on a day with basis runner speed the 125% over-plan notice is expected there; it alone is not a defect. Do not chase a single fast or slow run: prefer a source run whose recorded child times divided by planned times are close to 1, and compare the previous plan's notices before replacing the table. The ten-minute job deadline and every test deadline remain the hard limits.

## Node-ID argument files

On 2026-11-12, a local collection on macOS with SQLite selected the same 15,706 general tests as the planning-basis macOS lanes. All 15,706 node IDs, written one per line to a file and passed as `pytest --collect-only -q @file`, were collected in the same order in 9.4 s, against 10.0 s for the path-based collection. Parameter IDs with spaces, `::`, escaped bytes and lengths up to 4 MiB round-tripped unchanged. A node ID that no longer exists is reported by pytest as `ERROR: not found`, not skipped.
