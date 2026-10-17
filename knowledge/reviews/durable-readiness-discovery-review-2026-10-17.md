# Durable readiness discovery review

Date: 2026-10-17. Issue: [#70](https://github.com/realkarych/kora/issues/70). Disposition: confirmed core discovery-contract gap; resolve before scheduler implementation. The project owner selected a durable run boolean and approved the implementation plan. [ADR-0003](../../docs/decisions/0003-scheduler-and-workers-over-http.md) records the decision; the project owner accepted the final wording on 2026-10-17.

## Counterexample and qualifications

The previous [scheduler discovery contract](../specs/scheduler.md#8-restart-and-multi-scheduler-safety) visited runs touched since the last tick, using `updated_at`. Atomic changes and per-run locking do not ensure that an enabling commit is visible before a scan advances its timestamp cutoff.

On PostgreSQL, take a settled failed run with no active attempt, wait or deadline. The scheduler's watermark is 5. A manual-retry transaction acquires the run lock, writes running/pending state with timestamp 6, and remains uncommitted. A scan at 7 sees the old settled row and advances its watermark. The retry commits at 8, exposing timestamp 6. With notifications absent, ordinary discovery can omit the run forever. Pending promotion does not cover a run already returned to running. Locking only after candidate selection cannot repair an omitted candidate.

Independent review confirmed the trace and its limits. SQLite standalone serializes the exact overlapping transactions, so its test must use a serialized equivalent rather than claim the PostgreSQL interleaving. More than ten local rounds and restart were underspecified, not inevitable failures of every possible implementation. Safe timestamp cutoffs might address some cases, but no such proof covered the whole contract. An allocated sequence is not commit order either. This is design analysis; no database/runtime experiment has run.

## Selected mechanism

`run.needs_scheduling` is set in the transaction of every graph-relevant change. Candidate discovery queries that durable predicate without timestamp cutoffs, in finite rotating sweeps. The final fresh fixed-point check and acknowledgement share a run-locked transaction; reaching the round cap with work remaining preserves the marker. Restart reads the same predicate. A writer after acknowledgement sets it again; a writer before it must be visible to the fresh locked check.

Timed waits/admission rechecks, promotion, observations/delivery, deadlines and execution recovery retain independent discovery. Applying their enabling changes marks the run. Waiting for an external observation can be a local fixed point without abandoning that observation's own durable scheduling state. The marker carries no execution/publication authority and does not replace clear-downstream or recovery barriers.

Recurring sweeps of all active runs were considered but not selected: the marker avoids repeated evaluation of unchanged waiting runs. This trades simpler repeated discovery for a complete marking obligation, which is explicit in [data-model section 2.1](../specs/data-model.md#21-durable-run-scheduling-marker). Notifications and timestamps remain useful but are not correctness mechanisms.

## Verification boundary

The future conformance matrix in data-model section 2.1 covers late commits, settled failed retry, round-cap continuation, restart, page-cursor wrap and concurrent acknowledgement. Tests use real databases, injected time and barriers. Both databases must achieve the same progress and no-duplicate-effect outcomes despite different transaction interleavings.

Notification-disabled latency must include full discovery, local processing and worker claiming; a one-second tick is insufficient evidence. NFR-3.3 remains an incremental-query requirement on a 1,000-node graph. Neither requirement adds a global starvation guarantee under overload. These runtime tests are not implemented or reported as passing here.

Current executable checks are Markdown, links, Mermaid, IR examples and GitHub templates. They validate document integrity only.
