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
both operating systems and complete lifetime qualification remain acceptance
requirements. Older failed, cancelled or incomplete results are not relabeled.
