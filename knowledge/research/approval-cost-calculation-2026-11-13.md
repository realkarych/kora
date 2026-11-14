# Approval-cost calculation: method and implementation limits

Checked on 2026-11-13 for #1517 against the existing [accounting contract](../specs/data-model.md#74-immutable-approval-cost). This record describes the read-only calculator. It does not activate the approval writer, channel or public cost surfaces.

## Delivered boundary

The engine calculates one complete `RunCost` from all persisted attempts and usage rows of the selected run under the original managed writer transaction and run lock. Namespace selection and fresh writable PostgreSQL READ COMMITTED reads are mandatory. Source payloads, current prices, live counters and artifact lineage are excluded. Exact integer and Decimal addition preserve the existing model, monetary and serialization limits. Overflow is distinct from unusable input and from a representable unavailable accounting figure; all refusals leave the ledger intact.

`attempt.started_at` is the first-execute fact of the supported grant producer. Null start plus contradictory grant/execution facts refuses calculation. The calculator does not infer affirmative no-execution evidence from arbitrary recovery JSON. Integration with actual grants, usage and closure remains #1514/#1451/#1452; #1447 owns request creation, one calculation followed by atomic save and transaction exit, overflow failure, delivery and replay.

## Blocking checks

| Boundary | Executable evidence |
|---|---|
| Whole-run history and exact arithmetic | `test_approval_cost_includes_all_attempt_history`, `test_approval_cost_integer_addition_preserves_the_exact_bound`, `test_approval_cost_money_addition_ignores_ambient_precision` |
| Missing reports, gaps, billing and lifecycle | `test_approval_cost_preserves_missing_and_gap_coverage`, `test_approval_cost_unsettled_execution_caps_model_completeness`, `test_approval_cost_preserves_subscription_and_subtotals`, `test_approval_cost_preserves_unknown_dimensions_and_billing_sets` |
| Scope, original locks and observation ordering | `test_approval_cost_requires_original_run_lock`, `test_approval_cost_requires_the_current_writer_connection`, `test_approval_cost_requires_fresh_writable_reads`, `test_approval_cost_reads_after_the_competing_writer_commits`, `test_approval_cost_refuses_observations_after_snapshot_time` |
| Honest unavailable history and bounded refusal | `test_approval_cost_preserves_predecessor_distinction`, `test_approval_cost_refuses_contradictory_grant_history`, `test_approval_cost_refuses_invalid_saved_accounting`, `test_approval_cost_enforces_model_and_serialized_bounds`, `test_approval_cost_exact_serialized_boundary`, `test_approval_cost_money_overflow_preserves_ledger` |
| Client and native resources | `test_approval_cost_cursor_release_on_every_exit`, `test_approval_cost_cancelled_acquisition_closes_delivered_cursor`, `test_approval_cost_native_portals_end_with_the_caller_transaction` |
| Scoped native work and batches | `test_approval_cost_streams_only_owned_history` |
| Safe diagnostics and sink independence | `test_approval_cost_diagnostics_exclude_content`, `test_approval_cost_survives_unavailable_diagnostic_sink` |

Integration tests use real SQLite and PostgreSQL through the shared fixture. Events and observed lock waiters synchronize competing writers and cancellation. Product limits, exact resource ownership and the declared query method are blocking checks. Elapsed times below are diagnostics, not acceptance thresholds.

## Query workload and observed result

The native-plan fixture contains 13 owned nodes, 130 attempts and 260 usage rows, plus 1,300 nodes, 13,000 attempts and 26,000 usage rows in another run of the same namespace. Each node has ten attempts and each attempt two models. The fixture performs one `ANALYZE` per participating table before observing the unchanged statements. It captures SQLite `EXPLAIN QUERY PLAN` and PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` with the exact generated queries.

The local run returned 130 and 260 owned rows and batches `[128, 2, 128, 128, 4]`, with the exact cost `26`. PostgreSQL used the existing node, attempt and usage indexes; its two-row run table used a sequential scan. The attempt stream emitted 130 rows and the usage stream 260. Observed execution times were 0.394 ms and 0.612 ms on the local shared host. SQLite used indexed owner traversal. Two native-plan cases passed in 4.34 s including fixtures; this is not a latency guarantee. These local observations came from an uncommitted snapshot without a saved complete input identity; they are not final-head measurement evidence. Revision-matched CI must establish the same blocking native and resource assertions before merge.

Logical consumption is linear in owned attempts and usage rows; owner-index traversal also visits the run's node inventory, including nodes without attempts. The correlated existence check uses the existing per-attempt usage index in the same statement; it adds no application round trip or model-count-sized coverage inventory. The fixture establishes this indexed method at its declared scale, not arbitrary database scratch space, planner choices at every scale or a fixed total lock duration. Only one application result is active at a time. Fetch size bounds each application batch; it is not a claim that driver internals contain no additional copies.

## Resource ownership

The calculator owns each streamed result and closes it explicitly in `finally`, using the existing cancellation-safe acquisition and release helpers. The caller owns the writer transaction, run lock and connection. Tests inspect actual SQLAlchemy result closure, cleared adapter cursors, native SQLite refusal after cursor closure, pool units and lock reacquisition across success, failure, cancellation and shutdown. Acquisition cancellation waits for the delivered cursor and closes it before propagating cancellation.

The pinned asyncpg driver's server portals survive client cursor closure until transaction completion. One full calculation creates two portals; after the caller exits, a reused connection has none. This is a per-calculation count, not a bound across arbitrary repeated calls inside one transaction. The future approval writer must calculate once and exit after its atomic write. Existing release helpers preserve cancellation cleanup but provide no bounded shutdown latency for a stuck driver and no fixed task count under a cancellation storm.

## Scope and follow-up

No schema, normalization format, dependency, public operation, accounting limit or acceptance gate changes. The existing threat-model controls for scoped data, retained content, immutable accounting and bounded processing remain applicable. The next relevant master mutation audit supplies postmerge follow-up: the inspected audit had no executed pricing finding, with its pricing entries still pending. No additional PR mutation campaign is inferred.

Final revision checks, independent review and CI links belong to the pull request. Atomic approval persistence, producer-driven no-start evidence, late-report replay, delivery and surface parity remain unimplemented obligations of their named owners; this calculator alone proves none of those end-to-end capabilities.
