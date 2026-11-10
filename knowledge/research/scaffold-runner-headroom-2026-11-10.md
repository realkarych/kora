# Scaffold runner and deadline headroom, 2026-11-10

## Observed input

Seven failures among the last 200 master `CI` runs were inspected on 2026-11-10. Two of the recent failures never started the macOS job: [42641850131](https://github.com/realkarych/kora/actions/runs/42641850131) left storage and local-runtime without a runner, and [42659594818](https://github.com/realkarych/kora/actions/runs/42659594818) left remainder without a runner. Each cancelled job has zero steps and the annotation `The job was not acquired by Runner of type hosted even after multiple attempts`. On the same runs the other macOS jobs did receive runners, so the hosted pool supplied about two macOS runners while four jobs asked at once.

[42730738213](https://github.com/realkarych/kora/actions/runs/42730738213), head `0938795880ece44bd093b7e5300741292b44157a`, passed and is the timing sample:

| Job | Wall | Test step |
|---|---:|---:|
| Ubuntu 24.04, Python 3.15, remainder | 578 s | 543 s |
| macOS 15, Python 3.11, conformance | 565 s | 522 s |
| Ubuntu 24.04, Python 3.11, remainder | 542 s | 512 s |

JUnit suite time on that run, not a portable benchmark:

| Group | macOS conformance | Ubuntu 3.15 remainder |
|---|---:|---:|
| first | 493.7 s | |
| second | 472.8 s | |
| measurements | | 482.6 s |
| other | | 536.8 s |

The six conformance modules moved into the third group account for about 348 s of the macOS case sum. The ten Linux `engine` modules account for about 335 s of the Ubuntu 3.15 case sum. Those sums motivate the split. They do not demonstrate the resulting wall clock. macOS still runs the `engine` modules inside its existing measurements and other groups, so that job's case sum is unchanged.

Storage detach failures on [42530361926](https://github.com/realkarych/kora/actions/runs/42530361926) and [42482408174](https://github.com/realkarych/kora/actions/runs/42482408174) remain [issue #302](https://github.com/realkarych/kora/issues/302). The Lychee download failure on [42467751494](https://github.com/realkarych/kora/actions/runs/42467751494) remains [issue #1073](https://github.com/realkarych/kora/issues/1073). Neither is changed here.

## Placement

macOS scaffold jobs share two concurrency groups inside one workflow run: conformance with storage, and remainder with local-runtime. Each group runs one job and holds one pending job. Linux lanes use a distinct group per lane and interpreter, so they do not queue behind macOS. The ten-minute job deadline is unchanged. Queued time is not a new product limit.

The third conformance group and the Linux `engine` group are concurrent children of the existing lanes. Fourteen lanes, both metadata databases and the evidence join stay in place.

## Integration follow-up

[42766494419](https://github.com/realkarych/kora/actions/runs/42766494419), head `1ca2d66f73f882ecdb09845bbaae7f2f81ce4aa7`, passed after that placement. Ubuntu 3.11 integration took 558 s, with a 528 s test step. Ubuntu 3.15 integration took 538 s, with a 516 s test step. Every other scaffold job on that run had at least 134 s left before the ten-minute deadline.

JUnit case time for the two longest integration files, not a portable benchmark:

| File | Ubuntu 3.11 | Ubuntu 3.15 |
|---|---:|---:|
| `tests/cli/test_conformance.py` | 202.7 s | 213.0 s |
| `tests/api/test_graph_execution_preparation.py` | 88.8 s | 78.4 s |

Those two files are the Linux `surface` group. The rest of `tests/api`, `tests/cli` and `tests/ir/test_structured_schema_string_lengths.py` stay in the integration group, which ignores the two files on Linux.

The same 512 integration tests show why macOS cannot keep both files in remainder. On [42766494419](https://github.com/realkarych/kora/actions/runs/42766494419) the macOS integration cohort took 370.1 s. On [42789487696](https://github.com/realkarych/kora/actions/runs/42789487696) it took 530.7 s and the remainder job was cancelled at 604 s, after a 536 s test step. On [42791568563](https://github.com/realkarych/kora/actions/runs/42791568563) the two files were 206.5 s and 78.5 s of that cohort. macOS conformance runs them as its fourth group, and macOS remainder ignores them. These observations are diagnostic. They add no numerical gate.
