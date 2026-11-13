# Command stop timing investigation

Date: 2026-11-13. Tracking issue: [#1469](https://github.com/realkarych/kora/issues/1469). This record describes diagnostic evidence, not a runtime repair or a new acceptance method.

## Confirmed failure boundary

Master CI [43465798236](https://github.com/realkarych/kora/actions/runs/43465798236), revision `cc9a14d05cd0b07c25ab4555299ab4cc1eaea60d`, failed the duration and reader-bound `fits` command-observation tests in its Linux Python 3.11 local-runtime job. Both expected an observation and received `command_unobserved`. The assigned runner executed the tests; this is a kora failure, not an infrastructure-only run eligible for an unchanged rerun.

The earlier diagnostic revision `e1ea97931dd93b773672f54707d786bb775e14d2` in [PR #1477](https://github.com/realkarych/kora/pull/1477) retained incomplete census and stop-cutoff flags in [CI 43435479398](https://github.com/realkarych/kora/actions/runs/43435479398). Stop remained unresolved and capture sealing could not begin. That evidence does not distinguish late worker scheduling, reader construction, slow census work or delays between native operations.

## Local controls

The unchanged diagnostic revision was tested in isolated Linux containers with locked dependencies, a two-CPU quota, 2 GiB memory and no execution-time network. Every owned container, worker and population child was released.

| Observation | Result | Limit |
|---|---|---|
| Three focused cases, native ARM64 Python 3.11.17 | 3 passed, 0.53 s | Different interpreter build and host from CI |
| Complete observation module, native ARM64 | 20 passed, 2.51 s | No failure reproduced |
| Original census with 128 siblings, passive then busy waiter | 8.60 / 16.44 ms, both complete | Simple waiter differs from the observation query |
| Actual focused tests with 128 siblings | 3 passed, 0.71 s | Homogeneous container process population |
| Focused tests with CI's pinned PBS x86_64 Python archive | 3 passed, 2.22 s | x86 translation on a macOS Docker host |
| Original census with 128 siblings, pinned PBS x86_64 | 18.51 / 25.70 ms, both complete | Same translation and population limits |
| Fresh-process command, capture, observation module prefix | Command: 10 failed, 53 passed, 7 deselected; capture and observation passed | Command failures concern output snapshots and token redaction in this translated environment; the prefix is not a passing qualification |

The prefix returned to its initial process count after every module. This does not reproduce CI's systemd service boundaries or heterogeneous host population. The PBS archive was `cpython-3.11.17+20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz`, SHA-256 `3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522`. These observations leave both cross-test state and GIL contention unproven. Increasing populations or repeating unchanged local campaigns has no identified decision value.

## Hosted timing evidence

The test-only `StopTimingProbe` delegates to the original `CommandDomainStop` through its existing constructor injection. `ReapedSession` retains the original default. Only the three focused duration and journal-bound cases select the probe. Production code, native reader, stop outcomes, scan/stop/test deadlines, polling and required jobs are unchanged.

The probe records the delay from the original stop origin to factory entry in the injected clock's domain, constructor wall/thread-CPU duration, and stop wall/thread-CPU duration. A delegating clock returns each original sample unchanged and retains only the eight largest consecutive monotonic-check gaps, their corresponding thread-CPU deltas and sample ordinals. It also reports the total sample count. Numeric diagnostics are clamped to the nonnegative signed-64-bit range; these bounds never affect returned clock samples or stop decisions. Missing stages retain `None` and separate entered/returned flags. Evidence is read after the existing context owners finish, when the domain-stop executor has closed.

The fixed immutable snapshot contains only numbers, booleans and eight fixed gap records. It contains no process identity, path, environment, marker, command, exception, payload or arbitrary object representation. It retains no stopper or census. There is no logging sink in the measured worker. The original assertions remain, and reader-bound tests additionally require the completion captured before teardown to be `COMPLETE`, preventing an unresolved stop from satisfying the overflow case.

## Decision criteria and limits

- A large delay before factory entry localizes elapsed time to the combined leader-observation, event-loop and executor scheduling path. It is not a measurement of executor queue time alone.
- A large constructor duration distinguishes native reader construction from subsequent stop execution.
- Stop wall time much larger than thread CPU establishes elapsed time outside this thread's CPU execution. It does not distinguish intentional sleeps, native blocking, GIL waiting or descheduling.
- Largest gaps indicate concentrated versus distributed elapsed time. They cannot label census or syscall phases: the same clock covers census, member checks, signalling and intentional polling sleeps.
- Instrumentation adds work on every measured clock check. Fixed retention does not prove negligible cumulative overhead. A nonrecurrence is inconclusive; a failure supplies localization evidence, not an automatic cause attribution.

One ordinary changed-revision CI execution supplies the next observation, including existing changed-test repetition. No diagnostic value is a new pass/fail threshold. Existing behavioral failures remain failures, and no diagnostic-only revision establishes that master is repaired. A concrete repair requires cause-level evidence and retains normal review and final-revision CI obligations.
