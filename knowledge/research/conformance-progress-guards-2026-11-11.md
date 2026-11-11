# Conformance test hang guards measured from progress

Recorded 2026-11-11 under [#1352](https://github.com/realkarych/kora/issues/1352), part of [#1347](https://github.com/realkarych/kora/issues/1347). [Plan and per-test diagnosis](https://github.com/realkarych/kora/issues/1352#issuecomment-6791696330).

## Owner decision, 2026-11-11

For the conformance tests below, the hang guard keeps its existing 5- or 10-second value. The guard is measured from the last observed progress: the start of the next owned helper or process, or a received response. It is no longer measured from the start of the whole sequence.

A real hang still fails the test with the same exception and diagnostics. A real hang here means no progress for longer than the unchanged value. The total test time is then bounded by the CI job deadline.

## Why

Product deadlines in these tests run on a controlled lifetime clock that only the test advances. The failing guard was the test's own real-time bound. Each such bound covered a whole sequence of real helper processes started one after another:

- 25 in the cooperative transport cases;
- 7–11 in the command and listener cases;
- one probe child that runs its own helper sequence in the journal-recovery readiness case.

Locally, on macOS arm64 with CPython 3.11.17, the longest interval between process starts was 0.10–0.24 s, against totals of 0.7–2.6 s. Hosted runners run several pytest groups concurrently, and green runs used 40–100% of the guard. For example, the transport `eof` and `terminal` cases passed in 8.35 s and 8.28 s of 10 s on Ubuntu with CPython 3.15. No product or test race was found.

A diagnostic run oversubscribed 12 cores with 72 busy-loop processes and executed the 19 affected parametrizations twice:

- master `08e0fcd`: 10 failed, with the hosted failure types (`TimeoutError`, `ChannelError: deadline` and the journal assertion);
- the progress-bounded version: all 19 passed.

This run is evidence, not a gate.

## Mechanism

- `tests/conformance/progress_guard.py` provides `Progress.within(seconds)`, an `asyncio.timeout` scope.
- `Progress.advanced()` moves every active scope forward to `now + seconds` and never revives an expired scope.
- `ProgressLauncher` is an `IOHelperLauncher` injected through the owner's constructor. It delegates to the real launcher and records each helper start. Helper starts are serialized behind the previous helper's completion, so a stalled helper stops all progress.
- The recovery probe can write one byte per existing stage report to an optional pipe owned by `retained_live_invocation`.
- Regressions: `tests/conformance/test_progress_guard.py`.

## Limits

- A livelock that keeps starting helpers is not failed by the in-test guard. The job deadline still stops it.
- A single step that stalls longer than the unchanged value still fails. One recorded transport `cancel_frame` failure is this case. Its acquisition waited more than ten seconds on one live helper in a journal publish. In the same run, a concurrent helper-only test took 12.95 s, against 0.50–1.23 s in seven other runs: the whole runner stalled. No race was found, and no value was changed.
- Other tests keep their whole-sequence guards unless converted under a later change.
