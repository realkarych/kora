# Scheduler expiry native query work

Checked on 2026-11-13 for [#1444](https://github.com/realkarych/kora/issues/1444). The project owner approved the [proposed method](https://github.com/realkarych/kora/issues/1444#issuecomment-6847991192), including the clarification of separate candidate and fact work; [the approval record](https://github.com/realkarych/kora/issues/1444#issuecomment-6848208706) records its bounds. This is a fixed-fixture MVP regression criterion, not a general constant-time or scheduling-latency guarantee.

## Workload and observation

`tests/db/test_expiry_query_work.py::test_expiry_query_work_is_bounded_for_declared_history_sizes` uses the real metadata database with 150 active attempts and either 300 or 3,000 finished attempts on the same node. Finished attempts have corresponding historical dispatchers. Foreign nodes also have open and cancelled waits and closed approval deliveries. One owned open wait and pending delivery exercise cancellation alongside attempt expiry.

The test captures SQL emitted by a real expiry transition, rolls back its effects, then observes those statements with PostgreSQL `EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)` or SQLite `EXPLAIN QUERY PLAN`. Each observed statement runs inside a rolled-back savepoint. PostgreSQL statistics are refreshed with `ANALYZE` after seeding. This deliberately describes refreshed statistics, not arbitrary production statistics or cache state. The same test runs on SQLite and PostgreSQL; native plans are retained in its `native-plans.txt` artifact. No elapsed-time comparison or process-memory measurement is a blocking assertion here.

## Blocking criteria

| Owner or query | Criterion |
|---|---|
| Candidate pages and retained rows | At most 100 per page; cursor parameters remain bounded across repeated pages. Cancellation has a limited candidate-ID subquery before fact projection. |
| Due discovery | An active-attempt partial index excludes finished history; at most 150 active rows in this fixture produce the first 100 candidates, with one scan loop. |
| Cancellation, 300 finished attempts | Candidate index scan returns at most 100 with at most 300 filtered rows and one loop. A separate fact scan may read at most 450 attempt rows with one loop; indexed exact-ID reads also qualify. The two permitted scans can therefore visit 400 plus 450 rows, not 450 total. |
| Cancellation, 3,000 finished attempts | The cancellation partial index returns at most 100 candidates with one loop and no filtered rows. Fact retrieval uses at most 100 exact-ID lookups, each returning at most one row. No unrelated full-history scan qualifies. |
| Dispatcher presence | An indexed probe under `LIMIT 1` for each selected attempt, at most 100 probes for the measured page. This is not one probe for the whole transition. |
| Wait and delivery | Owner/identity indexes select open waits and update deliveries for the selected wait identities, without sequential scans of foreign history. |
| Transaction and resources | Rollback preserves original cancellation intent; database units return to zero. Deadline authority, sticky cancellation, exact closure evidence and capacity accounting remain separately blocking behavior checks. |

PostgreSQL row counts describe plan nodes and loops, not total physical reads. The larger observed fixture reports 200 heap fetches for its index-only cancellation scan in addition to the candidate rows and exact-ID lookups. These internal operations remain visible in the retained plan. The method does not claim a 200-read physical bound, history independence at every scale, behavior under stale statistics, or a bound on the duration of the complete atomic cancellation cascade.

## Prior evidence and follow-up

The earlier native test required the cancellation partial index at both sizes. Its final execution before approval produced three passing cases and one failed PostgreSQL small-fixture case. That failure remains failed evidence under the earlier criterion. The observed small plan used a primary-key candidate scan with 100 returned and 300 filtered rows, followed by a separate 450-row fact scan. The larger fixture used the cancellation partial index and 100 exact-ID fact lookups. The approved criterion explicitly admits the small-fixture plan; it does not reinterpret the earlier test as passing.

Run the changed test on both databases after implementing this method, and record its revision, command and result in the pull request. Final-head CI remains mandatory. A later audit or query-plan regression exceeding these declared bounds, or evidence of history-proportional work outside this bounded method, reopens investigation through the normal issue lifecycle. This record creates no additional campaign or release gate.
