# Owned-task cancellation during timer cleanup

Checked on 2026-10-28 under [issue #311](https://github.com/realkarych/kora/issues/311).
The owner approved the [bounded fix plan](https://github.com/realkarych/kora/issues/311#issuecomment-6473009126),
with [approval recorded here](https://github.com/realkarych/kora/issues/311#issuecomment-6473048835).
The [direct-consumer reconciliation](https://github.com/realkarych/kora/issues/311#issuecomment-6473372623)
preserves complete cleanup through cancellation.

## Confirmed defect

The former `kora.runner.channels.cancel_task` cancelled its target and suppressed
`CancelledError` whenever that target was cancelled. A cancellation of the caller
while it joined the target also left the target cancelled. The helper therefore
could suppress the caller's cancellation and let its operation continue.

A deterministic experiment at `b56a39a84899951fed31385a7ffa43345d79df9c`
cancelled a caller while its deadline task was closing. It reported a completed
caller and a closed timer, then failed the cancellation assertion. The helper
and its relevant consumers are byte-identical at
`bbfad857fbf5ca6719bf5b3c2400b573fec4114b`.

`InstalledPluginDiscovery.read_events` repeatedly uses `wait_future`, which joins
its cancelled timer through this helper. Discovery cleanup cancels its readers
and joins their combined result before starting process settlement. A reader
whose cancellation disappears can resume a receive with a frozen clock and
prevent process cleanup from registering the timer awaited by its test driver.

## Retained qualification failure

[Run 40710696173](https://github.com/realkarych/kora/actions/runs/40710696173)
tested `bbfad857fbf5ca6719bf5b3c2400b573fec4114b`. Fixture and installed-command
shards passed. Installed listing exceeded the unchanged ten-minute job deadline,
uploaded its evidence and caused the required join to fail. The qualification
remains failed.

Listing completed 408 of 420 cycles. Dataset 5 completed all three 70-cycle
parents. In dataset 256, two parents completed 70 cycles; the third stopped at
cancellation cycle 58. Its traceback waits in `ProbeClock.advance_to_timer` for
the five-second cleanup timer. Sustained samples show six tasks, five watches,
seven sockets and a live child. This is consistent with a diagnostic reader and
cleanup remaining active. The clock's private waiters are not counted by the
event-loop timer metric.

The post-abort `closed=true` receipt describes final abort cleanup, not earlier
completion. No task-stack snapshot captures the original interleaving, so its
connection to the confirmed helper defect remains an inference. The deterministic
regressions establish the defect independently of that attribution.

## Corrected ownership and outcomes

The helper joins the cancelled target through an aggregate that returns target
cancellation as a result. Caller cancellation propagates. Non-cancellation target
failure remains visible; when both occur together, the target failure is primary
and caller cancellation remains in its exception context.

The client writer uses nested cleanup so cancellation during the changed-task
join cannot abandon its sibling deadline task.

The endpoint retains its existing task owner through teardown. Each aggregate is
created once and shielded until complete, while the first incoming cancellation
is retained. Reader and writer settle before the pending-handler snapshot; every
handler settles before input charges, wire slots, transport bytes and resource
entries are released. Target faults are collected without interrupting remaining
joins. One target fault keeps its original type; multiple faults remain visible
in an exception group. Cancellation or faults propagate after accounting release,
with cancellation retained as context when a target fault supersedes it.

There is no additional background owner, persistent registry, polling wait,
restarted deadline or new execution authority. Temporary aggregate and result
storage is bounded by the admitted work it joins.

## Regression evidence and unchanged limits

The following regressions force the join window with the existing injected
`ProbeClock`, real futures and UNIX sockets, without sleeps or behavior patching:

- `test_channel_wait_keeps_cancellation_during_timer_cleanup`;
- `test_diagnostic_reader_keeps_cancellation_during_timer_cleanup`;
- `test_owned_task_fault_keeps_simultaneous_caller_cancellation`;
- `test_client_cancellation_joins_its_sibling_deadline_timer`;
- `test_conformance_fixture_resources_are_released_through_real_client_process[cancel_join]`.

The first three failed against the old helper and passed after its correction.
The latter two then failed against the old sequential consumer cleanup and passed
after its correction. Assertions inspect cancellation and actual settlement
before independent test finalizers can perform fallback cleanup. Existing
transport, process, recovery, repeated-cancellation and shutdown cases remain.

No public contract, persisted format, dependency, platform, workload, population,
sampler, numerical assertion, release gate or deadline changes. The fix implements
the existing cancellation and resource-release obligations. Fresh required CI,
the approved ten-execution unchanged-head campaign, local listing repetitions on
both operating systems, complete conformance-fixture qualification and all 420
installed-listing cycles remain merge acceptance requirements. Older failed,
cancelled or incomplete results are not relabeled.

The owner explicitly approved the [merge and release acceptance boundary](https://github.com/realkarych/kora/issues/311#issuecomment-6474819024),
with [approval recorded here](https://github.com/realkarych/kora/issues/311#issuecomment-6474966744).
Only the full installed-command qualification prerequisite for merging this repair
is removed. Full 270-cycle qualification remains blocking for the exact release
revision, with unchanged concurrent parents, workloads, instrumentation, assertions
and deadlines. Issues #349 and #395 remain open; the capacity work is revisited
before the next local-tooling release candidate.

Run 40723666026 at `62bc1791e6bdc56c52f82b1dfe4f21af86315de3` passed fixture
and listing qualification. Command completed 261 cycles with release and settled
observations, then all three parents were interrupted during recovery cycle 87.
Cycles 87 through 89, successful completion of all three 90-cycle parents and final
campaign validation remain unqualified. The workflow and command shard were cancelled;
the required evidence join failed. Completed-cycle observations and parent reaping
do not establish interrupted-resource release, full command qualification or leak
freedom. No safe production optimization is established by the retained timing
data, and no failed qualification is rerun for a passing result.


## UNKNOWN reports and test-owner disposal

Checked on 2026-10-29 under the [bounded continuation plan](https://github.com/realkarych/kora/issues/311#issuecomment-6477742188).

The final-source campaign [40728965629](https://github.com/realkarych/kora/actions/runs/40728965629)
passed attempts 1 and 2 at `e8589978128deb77a3c9db8a3937bf38fd8904d8`,
then stopped at attempt 3. This identifies the requested PR head; attempt 3 actually
checked out merge revision `e1d9f364d78f5d7238f74729c0e7af0dddc4c0aa`,
which also includes master `8d8b766`. Its macOS storage selection recorded two failures and
197 passes in 726.21 seconds. The blocked-native test received and acknowledged
feedback, then timed out during parent close. The broken-report test was still
waiting for feedback acceptance. Their common twenty-second observer covers
both the preceding installed cases and parent close. The retained evidence does
not attribute either timeout to the native-volume detach defect or establish
which phase consumed the budget.

An independent source review identified a separate disposal defect. An UNKNOWN
report may become ready while a cleanup effect is still settling. The test helper
waited for the report and helper collection, then immediately asserted zero
cleanup tasks. That assertion could mask the original test error and prevent the
remaining resource disposal. It is not evidence of a successful clean report.

`dispose_test_owner` now snapshots and joins the owner's finite recovery
dependencies after helper settlement and the recorded report, inside the same
five-second disposal timeout. The join does not cancel those dependencies or
reinterpret their expected failed outcomes. Existing task, process, helper and
descriptor assertions and remaining disposal remain unchanged. UNKNOWN remains
UNKNOWN, and retained recovery evidence is not reclassified as closed.

`test_disposal_joins_cleanup_effect_after_unknown_report` uses a real helper paused
after its successful publication response. A bounded coroutine-frontier observation
establishes that the parent effect awaits the helper's actual exit. The test
expires the parent decision deadline, observes UNKNOWN, then kills, observes and
collects the helper before disposal. Direct exit readiness resumes the test before
the effect's aggregate waiter, making the pending-effect assertion independent of
channel-EOF versus native-exit callback ordering. Two variants check ordinary
disposal and identity preservation of an original failure.

Only this new regression uses a fifteen-second cooperative input so its two
five-second setup guards finish before automatic escalation. Its complete close
budget is 18.1 seconds and is crossed by the forty-second injected advance.
No existing test, product, observer or disposal deadline changes. Both variants
failed the old cleanup-effect assertion, then passed after the join. The targeted
helper/control/phase/recovery/no-suppression selection passed 66 cases; six installed
storage cases and full Ruff check/format and strict Pyright also passed locally on
macOS with CPython 3.11.17 and the locked dependencies. These local results do not
replace required final-head Linux/macOS CI.

The installed feedback coroutine now attaches bounded parent-state metadata to
cancellation before its endpoint closes. Endpoint closure releases the native
fixture's socket barrier, so later snapshots cannot establish its earlier blocked
state. The note retains at most six real monotonic phase offsets and sixteen
owner-task top frames, count-only inventories, and separately named injected
clock values and absolute close deadlines. It collects no child content,
capabilities, paths, arguments, environment, exception text or frame locals and
adds no wait or output sink. The twenty-second observer and every existing closure
assertion remain unchanged. The original timeout cause remains unresolved; this
observation answers the phase-at-expiry question on recurrence rather than claiming
the disposal repair fixes the timeout.

Independent correctness, security and resource reviews inspected the actual
three-file delta and returned no findings. A changed final source still needs its
own ten complete successful CI executions; the two historical successes are not
transferred. The separate CEL-policy campaign [40729411466](https://github.com/realkarych/kora/actions/runs/40729411466)
passed five attempts for PR head `beaec8caa6cbf7368bacf6cde5153d0a2cdaddaf`,
then failed attempt 6, whose actual merge checkout was
`19ac0b909fad770c72b7f6131e5c45bc8e27c28e`, with cancelled macOS lanes and
failed required joins. Its storage artifact
retains partial progress without a completion code or JUnit; a particular failed
case is not established. Neither failed campaign is rerun for green. All exact
release qualification, populations, assertions and deadlines remain mandatory.


## Integration with accepted process-observer changes

The continuation integrates accepted master
`e6c0730f1ebdd0e7d809e8f8a7780034cb0aba97`. The identity-reader conflict keeps
both the bounded binary status reader and the accepted observer-namespace capture,
procfs visibility checks and fork-owner guard. It preserves byte-valued dead-state
checks and both the disappearing-status/reaping and non-dumpable-member regressions.
The accepted typed-frame methods continue using the same timer cleanup helper and
retain their cancellation and channel-closure behavior.

Independent correctness, security and resource source reviews inspect this bounded
integration separately from the unchanged disposal repair. Changed identity,
private-file and helper producer inputs prevent complete-input reuse of historical
fixture/listing qualification measurements. Fresh fixture and listing qualification,
supported-platform listing repetitions and complete final-head CI remain required.
The installed-command exact-release requirement retains its explicitly approved
release-only placement for this repair. No deadline, measurement method or workload
changes with this base integration.
