# Run-list design inputs and delivery limits

Checked on 2026-11-14 against `508006c66314cf7c1e59b18f334a686a7e2aa191` for [#1555](https://github.com/realkarych/kora/issues/1555). This is source/design evidence, not a runtime qualification. The proposed [operation contract](../specs/run-list.md) contains the complete behavior; accepted existing specifications prevail until owner acceptance of its wording.

## Existing inputs

| Input | Reuse and limit |
|---|---|
| `api.application`, `api.routes`, authentication/errors/exchange | Active inventory, personal/service separation, structured errors and correlation; no run-list route yet. |
| `db.connection.Database.read_snapshot` | Short coherent read-only snapshots and owned admission/cancellation/cleanup; no native projection-statement timeout installed by this interface. |
| `db.reads.RunRow`, `RUN_COLUMNS` | Include params, trigger, limits and retention; cannot be the public scalar projection. |
| `db.attempt_capacity` | Occupancy semantics inform the read. Full counts/window count do not satisfy the public operation's admitted-row budget. |
| `engine.approval_cost_totals`, `db.cost_reads`, `db.approval_cost_reads` | Exact monetary/coverage semantics and source-free projections. The writer-bound calculator and its per-run streams are not the list reader. |
| `db.tables`, `admission_wait_v1` | Run/node/attempt/event/wait/usage facts exist. Target origin resides in the original event; live cap fields may need dedicated bounded projection before public activation. |
| `client.transport` | Bounded JSON reception, origin controls and response lifetime. Add structural query parameters through HTTP and ASGI; the path-only method rejects `?`. |
| `cli.graphs`, `mcp.operating`, `mcp.results`, `tests/api/test_parity.py` | Existing thin-client conventions and structural parity; no implicit list implementation or generated operating reference. |

## Native deadline mechanism evidence

The [Python sqlite3 reference](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.set_progress_handler) documents a progress callback and nonzero interruption result. The [aiosqlite API](https://aiosqlite.omnilib.dev/en/stable/api.html#aiosqlite.Connection.set_progress_handler) exposes that callback and an interrupt operation. The proposed read unit combines them with its original monotonic deadline and cancellation owner. Callback cadence does not bound a stuck filesystem call. Actual cancellation, callback removal and connection reuse still need tests with the project's pinned driver on both supported platforms.

PostgreSQL documents [statement_timeout](https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-STATEMENT-TIMEOUT) as a per-statement timeout; zero disables it. [SET LOCAL](https://www.postgresql.org/docs/current/sql-set.html) ends with the transaction. The proposed design applies the remaining projection time to every statement rather than restarting five seconds for each query. Driver cancellation/rollback and native portal closure remain executable acceptance obligations. Documentation is not evidence that the current kora snapshot wrapper already implements them.

The native method must integrate inside `db`, through supported driver interfaces, with no dynamic patching, schema attestation or new dependency. Any source/API mismatch discovered during implementation requires a plan correction before activation. It does not justify replacing confirmed closure with a Python timeout.

## Index and source delivery

Use explicit indexed equality prefixes and ID keysets for the supported filters. Candidate run indexes are `(namespace_id,id)`, `(namespace_id,graph_id,id)`, `(namespace_id,status,id)`, `(namespace_id,rerun_of_run_id,id)` and `(namespace_id,target_key,id)`. Existing equivalent indexes should be reused. Do not install every cross-product of filter combinations: choose one selective access path and charge bounded residual filtering, under the same statement deadline.

Related traversals need run/node/attempt/usage and open-wait owner indexes, plus `(run_id,kind,id)` for selected target-origin discovery. The concrete migration must explain exact native plans for both databases and growing histories. A migration adds indexes only where required, preserves every stored value and receives its revision number against the actual merge base. It may not rename or overwrite the parallel scheduler/worker migration chain.

Run identity/timing and occupancy require no new persisted source. First binding is the saved `run.target_bound` event; a bounded scalar selection preserves it without whole-event transfer. Visible limit observations require compact selection of live namespace configuration and the selected graph version's concurrency. Shared global/runtime observations are explicitly redacted from the ordinary list; their own-run wait markers do not require cross-namespace aggregate queries. If the supported encoded graph representation cannot provide this without body acquisition, its producer must add a reviewed compact projection before activation. #1555 does not select a new persisted format or authorize changing graph publication. Current target-wait decoding similarly requires its owning producer's accepted representation. The public run-wide attempt collection must obtain owner acceptance for the proposed closed-catalogue `recovery_required` selector and implement its exact scoped continuation before the list activates. Existing `replacement_blocked` also matches healthy work and cannot substitute for that set.

## Delivery and size

The design PR is independently useful: it fixes the complete public operation, predicates, read ownership and activation dependencies. Its handwritten production diff is **zero**. It activates no capability and closes only #1555.

The complete runtime operation has the following planning estimate, excluding tests, documents and generated files but including handwritten migration/configuration changes:

| Integration portion of one complete operation | Estimated changed production lines |
|---|---:|
| Native deadline/snapshot integration and restoration | 250–400 |
| Scalar/keyset queries, budget accounting and necessary indexes | 400–600 |
| Batched state/admission/cost projection | 350–500 |
| Typed wire/query/cursor models, route/error/diagnostic integration | 250–400 |
| Shared query transport, CLI/MCP list parity | 180–280 |
| **Total** | **1,430–2,180** |

This is a source-based estimate, not measured code size. It excludes the separate public run-wide attempts operation and any new live-cap persisted producer. Existing simple helpers reduce duplication but do not remove their owning contract tests or complete parity.

The normal 1,000-line limit therefore stops runtime implementation planning as one approved PR at this point. Splitting models, database access and CLI exposure into separate steps would not provide independently complete list behavior. Before coding, the runtime owner must produce an actual independently useful split with real consumers, or seek a prior bounded owner exception demonstrating that the complete operation is indivisible. This design grants neither an exception nor permission to ship partial filters, summaries or missing resource protection.

Separate outcomes retain their own issues/approval: a complete run-wide attempt inspection operation (with owner-accepted exact recovery selector and CLI/MCP parity), any compact live-cap publication required by real consumers, then the complete run-list operation. Singleton/wait/content/event operations remain outside this track. Creating an implementation issue is not implementation approval; its acceptance must integrate the actual merged producers and full client chain.

## Evidence and remaining work

Daily master mutation audit [43584820056, attempt 1](https://github.com/realkarych/kora/actions/runs/43584820056) used source/tooling `d623a105c17ac682e8017a469fc8c6535990ec28`. Artifact `mutation-43584820056-1` reports partial completion due to budget, restored continuation, 150 executed and zero reused entries: 88 killed, 15 survived, 47 test errors, 330 pending. Only pricing and CEL paths were declared; this supplies no direct public-list or native-read-lifetime coverage. It adds no mutation campaign or score gate to this change.

Independent scope assessments supported a design-first operation boundary and identified the source, lifetime and size gaps above. Review of the actual proposed contract and document checks are recorded in the PR, not represented here as runtime proof. Future test names in the specification remain pending implementation.
