# Attempt inspection source and delivery inventory

Checked 2026-11-15 against `5c582a69461b032720f73ed1b64e28ec208eb055`, after native metadata deadlines merged through #1568. This is planning evidence for the [proposed operation](../specs/attempt-inspection.md) under #1570, not runtime qualification or permission to change persisted formats.

## Existing sources and missing producers

| Boundary | Inspected implementation | What it establishes and what remains |
| --- | --- | --- |
| Attempt identity and state | `src/kora/db/tables.py`, `src/kora/db/types.py` | Scalar identity/status/times/runtime/worker/domain/recovery flag/version exist. Attempt has `node_instance_id`, not `run_id`. Current node pointers cannot enumerate history. |
| Pinned execution contract | Run → saved graph version and `execution_contract_digest` | A scalar digest needs no IR/plugin load. Historical policy revision needs the saved attempt policy composition, not live namespace revision. |
| Complete deadline | `src/kora/engine/deadline_records.py`, data-model section 16.3 | Unrecorded, explicitly unlimited and exact recorded bindings are distinct; scalar legacy values do not recover determining scope or uncertainty. The public seconds field needs lossless conversion from saved microseconds. |
| Recovery reason | `src/kora/engine/lease_recovery.py`, `src/kora/engine/expiry.py` | Existing writers add `lease_expired`/`deadline_expired` reasons and scalar uncertainty/recovery flags. They do not establish a complete public recovery-observation format. |
| Dispatcher history | `src/kora/db/grant_history_v1.py`, dispatcher rows and `src/kora/protocols/evidence.py` | Saved attempt/domain/worker/incarnation/epoch/mode/times and evidence exist; open and prefix indexes exist. Process identity and raw evidence remain private. Protocol DTOs do not by themselves define native metadata projection/admission of the stored JSON. |
| Replacement and permitted actions | ADR-0017, API recovery contract, runtime evidence types | Full closure/publication and pending-action facts must have a reviewed saved producer. Neither the flag nor the newest lease supplies them. No complete public projector exists in this revision. |
| Claim admission | `src/kora/db/claim_sources_v1.py` and native Claim readers | Conservative whole-value/physical-read charges have actual execution consumers. They are not exact selected-field byte descriptors, and do not automatically cover recovery/observation JSON. Reuse requires exact source and charge identity. |
| Recovery counting | `src/kora/db/attempt_capacity.py` | Internal `read_run_recovery` uses `count().over()` and a 20-ID sample. This is not a public budgeted enumeration and cannot be exposed as one. |
| Native lifecycle | `src/kora/db/connection.py`, `src/kora/db/maintenance.py` | Explicit-deadline snapshots now own SQLite interruption/restoration and PostgreSQL statement timeout/cancellation/rollback. They do not implement query admission, a public DTO or client parity. |
| Active public routes | `src/kora/api/routes.py` | Health/schema/deploy/trigger and configured worker registration exist. Personal attempt detail/dispatchers/observations/selected content and this collection remain inactive. |
| Shared client | `src/kora/client/transport.py`, `asgi.py`, `network.py` | Current master has bounded ordinary JSON transport. Pending Worker history #1523 adds common query pairs plus worker-specific admission; reuse only after its actual merge and input review. |
| API/CLI/MCP integration | `src/kora/api/run_trigger_route.py`, `src/kora/cli/graphs.py`, `src/kora/mcp/operating.py` | Existing operations supply thin-client/diagnostic patterns, not the new closed query/cursor, history semantics or response composition. |

Execution-content #1557/#1558 supplies a design for request/result owners, not implemented saved bytes. Storage #1341/#1561 and retention #216 retain their owners. Worker Claim/heartbeat/report and scheduler settlement must not be duplicated by a public reader. A partial producer is an activation gap even if a fixture could synthesize its future fields.

## Explicit design choices

The proposed collection/detail division keeps one bounded collection row and one required ordinary metadata-detail reference. Full descriptors, actual observed identities with provenance/time, recovery explanations, exact dispatcher counts/closures, retained scopes and permitted actions remain required at that detail. The collection cannot activate ahead of it. This division is explicit owner-review wording, not an inference that the previous broad history/recovery clauses never applied.

The exact `recovery_required` selector is added to the run-wide catalogue; it compares the saved flag and is distinct from `replacement_blocked`. `node` means public instance ID. A worker filter without mode includes retained participation, while `worker_attempts=held|recovery` adds the accepted current-assignment predicate. Incarnation is a retained same-row worker/incarnation pair. These choices make prior holders discoverable without attributing today's registration to old grants.

The initial native access plan traverses run-owned node groups, then `(node_instance_id,id)` attempt ranges, retaining only the smallest `limit+1` matching IDs. It needs concrete `(run_id,id)` node and `(node_instance_id,id)` attempt indexes or qualified equivalents. It requires no denormalized run identity or second history owner. The price is bounded per-page scanning: a large run may exhaust the shared 100,000-row budget before producing a small page. This is an explicit refusal, not a constant-cost claim. Sparse/late-page plans and cancellation still need actual qualification.

Alternatives considered: a global attempt-ID scan can spend its budget on unrelated tenants; a whole-run join/sort behind `LIMIT` does not establish admitted workspace; one query/heap entry per node grows with topology; a persisted run-wide identity could improve access but changes a producer/format and is not authorized here. Ordinary index additions remain explicit in the future implementation plan and include migration lines in its size count.

## Independently useful delivery outcomes

The design PR delivers an exact collection contract and an honest activation map with **0 production lines**. It closes #1570 only. It neither makes the public run list ready nor approves a partial attempt endpoint.

| Outcome | Existing owner or prerequisite | Activation evidence |
| --- | --- | --- |
| Complete typed recovery/publication/observation source used by actual reporting/settlement | Heartbeat #1535, request/report #1453/#1454 and scheduler settlement #1472, under their approved scopes; any additional source format needs an explicit plan | Real producer commit/replay and consumer tests, original-domain/evidence validation, native scalar admission and history preservation. A writer-less DTO/helper is not a separate deliverable. |
| Exact selected saved-field representations and byte owners | Execution-content design #1557, implementation owners #1453/#1454 and #216 | Actual descriptor publication, replay, current access/retention and bounded selected reads. Claim conservative charges cannot substitute. |
| Complete personal attempt metadata inspection with its necessary dispatcher/observation/selected continuations | API work item 11 and artifact/history work item 12; separate implementation issue/approval required before code | Complete required metadata/absence semantics, real authorized consumers and API/CLI/MCP parity, with all actual source dependencies. Worker-only history #1523 does not activate it. |
| Complete run-wide attempt collection | Separate implementation issue after the preceding source/consumer boundaries are concrete | Exact filters, one-page source/keyset/byte/work accounting, native release and all surface parity; every advertised detail reference works. |
| Complete public run listing | Accepted design #1555/#1556 plus its independent live-limit/target prerequisites | Exact recovery continuation through this collection, complete scalar summaries and its own final-head evidence. |

The source/consumer dependency is semantic: a truthful reference needs a consumer, and a consumer needs its producer. It is not a proposal to split one operation into models, SQL and CLI stages. Each future issue must deliver an independently usable operation or a producer with a real integrated consumer. Do not create placeholder successes, hide required fields or drop tests to meet a line budget.

## Size and verification limits

The following estimates describe the proposed compact collection after all required sources and personal detail exist. They exclude tests, documentation and generated files, and include handwritten wiring and any index migration. They are not measured code size or approval to exceed 1,000 lines.

| Integration portion | Estimated changed production lines |
| --- | ---: |
| Closed request/row/cursor and validation | 180–260 |
| Node-group traversal, source predicates, bounded selection and necessary indexes | 300–450 |
| Route, byte composition, errors, diagnostics and wiring | 150–230 |
| Shared transport integration, CLI and MCP parity | 150–240 |
| **Compact collection total** | **780–1,180** |

The earlier full-metadata collection assessment was 870–1,370 before missing consumers/producers. Explicit detail placement reduces row composition but adds a real prerequisite, not a free omitted obligation. The complete dependency stack has no honest bounded estimate yet: producer ownership must be settled against the actual merged Worker/storage/scheduler inputs, and personal detail/history is not implemented. No ≤1,000-line readiness claim follows from the lower end of either range. Runtime planning must stop if its complete independently useful result exceeds the limit; split by actual outcomes or seek the owner's prior explicit bounded exception with evidence of indivisibility.

Required executable traces are named in [the specification](../specs/attempt-inspection.md#6-dependencies-and-acceptance). They are pending. The existing DB deadline tests establish their native owner only; Worker history tests establish service authority and retained prefix semantics only. Neither qualifies a public user read. Query plans, latency and process memory remain diagnostics; no new performance gate or repeated qualification campaign is proposed.

The latest available daily master mutation audit, [43584820056 attempt 1](https://github.com/realkarych/kora/actions/runs/43584820056), has source/tooling `d623a105c17ac682e8017a469fc8c6535990ec28`. Its inspected summary/report are partial due to budget, with restored continuation, 150 executed, zero reused, 88 killed, 15 survived, 47 test errors and 330 pending. Only pricing/CEL paths are declared. Artifact `mutation-43584820056-1` has SHA-256 `f2aaf87eba9db98a28c849c3998c8151a27a688d6b8245e0e0d653d469b11929`; extracted report SHA-256 is `a16a15546e915a1196b860912f60d1414dca7694179edf884af27d441d80b392`. This supplies no applicable attempt-read finding or runtime evidence and adds no mutation campaign or merge hold.

Separate correctness, security and performance scope assessments identified the source, identity, continuation and access gaps. Conclusions on the actual proposed documents, document checks and final-head CI belong in the PR. A design-only CI success is not an endpoint test result.
