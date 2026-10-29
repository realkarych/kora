# Recovered settlement implementation evidence

Checked 2026-10-30 against base `62cc6a2e31d8555bf9346a428621922f2dbc0d8d` and the uncommitted implementation for [#369](https://github.com/realkarych/kora/issues/369). The runner source SHA-256 is `6448625ac988ab2527b756631ecb91902fee08af40a5f9993b87ceb281957190`. The [lifetime contract](../specs/conformance-lifetime.md#resource-establishment-and-ownership-transfer), [approved observation design](recovered-settlement-observation-2026-10-30.md) and [additional repair authorization](https://github.com/realkarych/kora/issues/369#issuecomment-6509391182) govern this implementation. Independent actual-source review and complete final-head Linux/macOS CI remain required before delivery.

## Behavior and ownership

Recovered observation supplies its existing public result and a private wait/refusal classification from one pass. A listed per-PID `ABSENT` member or a freshly validated complete dead member without pending native readiness permits bounded waiting only after the pass excludes terminal failures. A later identity, scope, readability, capacity or unexplained-anchor refusal dominates earlier collection waitability, including with no retained watches. A group entry for the registered leader must match its immutable registration even when the first leader read was absent.

The public result remains `UNKNOWN` after collection during native-watch establishment unless fresh absent-leader/empty-group confirmation succeeds. This race never creates signaling authority for a surviving leader. Public/native identity records, native-reader behavior and the live signaling predicate are unchanged. No numeric SID or complete dead identity is synthesized from registration or waiting.

Ordinary validated live watches keep native waiting. Eligible collection waits share a call-owned timer scheduled 100 ms after the preceding observation completes, capped by the original absolute deadline. Native notifications may cause additional observations; elapsed refresh intervals do not accumulate catch-up work. The original deadline is checked before and after observation and waiting, and retains exact `ChannelError(DEADLINE)` priority over success, refusal or disposal.

The recovered owner retains its existing native watches and one lazy disposal future. Each waiting call owns at most one timer, which is cancelled and joined on every exit. Explicit disposal completes the common future and closes watches without claiming closure. Cancelling one waiter preserves the common future and watches for another waiter or later cleanup. The shared watch helper retains its previous default behavior for gated direct-child consumers. The test cleanup helper now calls corrected recovered settlement instead of its polling workaround.

There is no new production consumer, dependency, persisted format, public operation or diagnostic event. Existing callers retain structured refusal reporting; no process content is added to logs. The cooperative local threat-model and native-syscall limits in the design remain active. Production activation must still evaluate aggregate scan work at its admitted concurrency.

## Local verification

The local environment was macOS 27.2 arm64 with Python 3.11.17, using this checkout's `.venv/bin/python`. Tests retained warnings-as-errors, real native children, injected decision clocks and explicit control/readiness barriers. There were no mocks, synchronization sleeps, skipped cases, raised deadlines or relaxed assertions.

| Command | Actual result |
|---|---|
| `.venv/bin/python -m pytest -q tests/conformance/test_recovered_process_lifetime.py` | 39 passed in 2.57 s. |
| `.venv/bin/python -m pytest -q tests/conformance/test_lifetime_journal_recovery.py tests/conformance/test_local_process_lifetime.py tests/conformance/test_lifetime_parent_exit.py tests/conformance/test_lifetime_recovery_claim.py tests/conformance/test_lifetime_readiness.py` | 67 passed in 32.05 s after replacing the cleanup helper. |
| `.venv/bin/python -m pytest -q tests/architecture` | 907 passed in 29.89 s. |
| `.venv/bin/python -m ruff check --ignore-noqa .` | Passed. |
| `.venv/bin/python -m ruff format --check .` | Passed. |
| `.venv/bin/python -m pyright` | Zero errors and warnings. |
| `git diff --check` | Passed. |

The recovered test source SHA-256 is `253bc5a940359888f54bc35ed57b08898541d3a16b01e5d58ea610d5758001ba`; the native child source is `81573241b2bf63c4d82e8d2eaeb54250288d9038edac5566defc43b5e27d2bc4`. The cleanup helper changed only after these recovered regressions passed; its unchanged imported task-join helper was used by that run. The subsequent caller and architecture runs inspected the updated cleanup helper, SHA-256 `74cac2d806882d3faf72858533709fc475c4999928e1711f9e5789598cc4b25d`. Final-head CI must run the complete current inputs.

The native cases cover held leaders first observed alive or after exit; exact deadline, cancellation, concurrent waiter and disposal behavior; a held nonleader while its leader stays live; completion-based refresh and return to native-only waiting; collection during establishment; terminal-refusal dominance with and without watches; and preserved observation/TERM/KILL refusal after a collection race. Actual absence/empty groups, joined timers, zero retained watches and post-GC descriptor restoration are blocking assertions. Durations above are observations, not performance guarantees. These local results cover macOS listed-PID `ABSENT`; Linux complete `PRESENT`/dead and complete supported-platform acceptance remain pending CI.

## Retained failures and limits

The first native-watch repair remains failed, as recorded in the [original macOS diagnosis](https://github.com/realkarych/kora/issues/369#issuecomment-6505109808). The second candidate's one recovered-file execution produced 35 passes and one failure: with no retained watch and an initially absent leader read, a changed start time in the group's leader entry failed to dominate another waitable member. Settlement reached the test's external guard instead of returning prompt `UNKNOWN`. That candidate was stopped without a favorable rerun; its patch, JUnit and source hashes remain retained as failed evidence in the [stop record](https://github.com/realkarych/kora/issues/369#issuecomment-6509369687).

The owner authorized one additional repair for that immutable-leader guard and preservation of public `UNKNOWN` after collection races. The successful local execution above tests those changed inputs; it does not reclassify either earlier candidate. There is no further repair authorization if this candidate fails behavioral acceptance. The 149 added plus deleted handwritten production lines are confined to `src/kora/runner/processes.py`; tests and this evidence record are excluded. No independent review or CI pass is claimed here. Dependent tests-only #368 remains pending actual delivery of #369.
