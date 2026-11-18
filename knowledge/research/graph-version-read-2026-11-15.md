# Single graph-version read: source and delivery inventory

Checked on 2026-11-15 against `812299e5c768ec4dc823aef7ca84256d7471dbb4`. This is the evidence and implementation inventory for [#1634](https://github.com/realkarych/kora/issues/1634) and its [accepted contract](../specs/graph-version-read.md). It is not runtime qualification. This design changes no production code, endpoint, migration, dependency or current execution eligibility.

## Existing sources and missing composition

| Boundary | Existing source | Reuse and limit |
|---|---|---|
| Authorization and errors | `api/authentication.py`, `api/routes.py`, `api/errors.py`, API conventions | Reuse personal authentication, scoped identity and fixed error contracts; add the exact GET catalog entry. |
| Graph identity and snapshot | `engine/graph_snapshot.py`, `db/graph_version_reads.py`, `db/graph_version_projection.py` | Existing effect-oriented acquisition needs original admission and also reads resource references/owners. Historical projection needs its own qualification and no physical-resource lookup. |
| Paired identity | `engine/graph_version_identity.py`, `db/graph_values.py` | Reuse the encoding-1 canonical preimage and three digest definitions. Detached legacy values are supported as a representation, not as qualified native history acquisition. |
| Logical JSON | `ir/graph_json.py` | Existing exact codec preserves order, integer/float kind, signed zero, escaped NUL and surrogates. It does not itself supply a safe native read, HTTP response policy or client ownership. |
| Legacy native storage | `db/types.py`, `db/tables.py` | `JsonDocument` has no general source-size bound; PostgreSQL JSONB and SQLite text have different historical preservation limits. Whole ORM rows and post-serialization length predicates do not solve pre-admission. |
| Current execution reader | `engine/graph_execution_contract.py` and its readers | These interpret executable semantics and installed behavior. Historical inspection must not depend on that composition or manufacture a missing parameter profile. |
| API resources | `api/graph_deployment_limits.py`, `api/application.py` | Producer limits include a 16 MiB document pair. Reusing its magnitude does not qualify a GET or let the GET borrow deployment's execution-preparation slot/budgets. |
| Shared client | `client/transport.py`, `client/errors.py`, `client/documents.py` | Existing deadline, streaming and client-origin error boundaries are reusable. Current per-operation response limits and ordinary JSON parser do not cover complete graph values. Tagged ASCII texts avoid decoding inner graph numbers/strings at this boundary. |
| CLI and MCP | `cli/graphs.py`, `mcp/stdio.py`, `mcp/results.py` | Add one read through the shared API client. Current synchronous MCP helper duplicates structured and textual content; the accepted large text-only result requires owned off-loop serialization and output settlement. |
| PostgreSQL continuity | `db/connection.py`, scheduler sections 10 and 14 | A deadline-bound read snapshot is not a qualified shared migration lease. Existing narrow first-slice exceptions do not authorize this operation. |

Paths in this table are relative to `src/kora/`. They identify places to inspect, not a commitment to add a new package or widen imports. Historical visibility and original identity stop the dependency traversal: no Worker report, execution validator, scheduler, artifact projection producer or trigger implementation changes merely because their saved metadata can be inspected.

## Native qualification

SQLite documents that `octet_length()` on a table's text/blob column can use metadata without reading its content. This supplies a promising preflight primitive, but it does not prove the whole record access path, overflow traversal or later projection. Qualification must cover all wide fields reached under kora's migrated schema, both database text encodings and the exact query sequence. [SQLite core functions](https://sqlite.org/lang_corefunc.html).

PostgreSQL documents that `pg_column_size()` reflects compression and that `pg_column_compression()` reports the compression method. Consequently, a small physical length cannot directly admit the uncompressed value. [PostgreSQL 17 administrative functions](https://www.postgresql.org/docs/17/functions-admin.html).

One candidate is a conservative expansion bound for the known compression formats followed by a bounded structural JSONB walk. PostgreSQL's pglz implementation caps one match at 273 bytes; the LZ4 block format extends a match by at most 255 bytes per extension byte. A factor of 512 over physical size plus framing is a conservative candidate inferred from those formats, not a published PostgreSQL API guarantee or a qualified kora bound. Pin the supported server source/layout and prove all headers, compressed/external cases and rejection of unknown methods before choosing it. [PostgreSQL pglz source](https://github.com/postgres/postgres/blob/master/src/common/pg_lzcompress.c), [LZ4 block format](https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md).

Native JSONB admission still does not bound whole-container textual expansion: compact numeric values can have long decimal output. The design therefore separates native admission, charged structural visits and admitted scalar conversion. The implementation must demonstrate that its query plan does not materialize an unbounded intermediate relation, whole-container text or recursive traversal before those checks. A SQL predicate written next to a conversion is not evidence that the executor evaluates it first. This remains a concrete unimplemented qualification obligation, not an assertion that existing PostgreSQL functions already satisfy the design.

For witnessless paired values and legacy objects, admission is ephemeral within the coherent historical snapshot. No original publisher admission witness is inferred, persisted or used for execution. If native proof fails, implementation stops for a revised plan; it cannot substitute an unbounded fallback, exclude all legacy versions or borrow the unrelated walking-skeleton migration exception.

## Transport and resource decisions

The accepted tagged JSON-text representation makes the original graph domain explicit without requiring ordinary Python, JavaScript and MCP JSON decoders to accept arbitrary exact integers or lone surrogates. It also separates the public projection from the complete saved digest preimage. The owner accepted this public wire choice on 2026-11-15; no existing implemented graph-read response is being migrated.

The 16 MiB original pair, 128 MiB native source, 256 MiB cumulative native visits, 512 MiB accounted detached workspace, depth 1,024, `64B + 1 MiB` detached work and one active operation are accepted finite product/accounting limits. The HTTP and MCP frame allowances are derived from ASCII string escaping; actual complete frame sizes must still be checked. The 5/15-second database/API deadlines and 20/25-second client/MCP deadlines bound useful work, not the time needed to prove that cancelled resources have stopped. None is a process RSS, latency or throughput guarantee. Implementers must supply actual representation charges and exact source/work/frame boundary tests; this design PR supplies no such measurements.

The current MCP dual-result helper is specifically excluded from the new large result's path. Keeping one text block avoids a second semantic payload copy, but the complete protocol frame and in-progress output remain separately charged and owned. Text-only output, the exact version selector and the legacy integrity distinction are part of the final public-contract decision.

## Implementation deliveries

No runtime work is authorized by this design approval. Estimates below include handwritten production additions and deletions, integration glue and migrations; tests, documentation and generated clients are excluded only from the size calculation, not from their deliveries.

| Independently useful result | Estimated production change | Dependencies and required scope |
|---|---|---|
| Shared PostgreSQL migration coordination for existing snapshot consumers | 600–950 lines, provisional | Complete reusable lease and migration-writer participation, real database qualification, startup/cancellation/cleanup ownership, and approved integration into an existing consumer. A dormant helper alone is not this deliverable. Preserve existing maintenance guarantees and exceptions unless the owner changes them. |
| Complete historical graph-version read | 1,100–1,600 lines, provisional | Native historical acquisition and qualification, exact projection/codec, API lifecycle and full framing, shared client, CLI, authoring MCP, generated contract and parity in one useful delivery; depends on qualified migration coordination. |

The second estimate exceeds the repository's 1,000-line production limit. The earlier tentative 600–950 estimate omitted complete witnessless/legacy acquisition and transport lifetime; it is not a basis for implementation approval. Before code, inspect actual reusable primitives and post a concrete reduced estimate, identify another independently useful result with its own consumer, or ask the owner for a bounded indivisibility decision. No size exception is selected here. Separating model, route and clients into incomplete PRs is not an acceptable workaround. This stop applies to runtime planning, not to the zero-production-line specification delivery.

## Verification ownership

| Affected contract and risk | Integration owner | Blocking evidence for this delivery | Future runtime evidence |
|---|---|---|---|
| API identity, historical projection and thin-client parity | Public graph-read delivery | Independent correctness; internally consistent exact selector/response/error matrix; repository documentation checks | The named API/history/lossless/parity tests in the specification, both databases and generated contract |
| Namespace authority and content boundaries | API authentication and graph-read delivery | Independent security; scoped lookup, complete exclusion set and content-free errors/logs | Real principal/foreign-ID refusal and diagnostics tests |
| Native source, snapshot/lease, CPU buffers and slow delivery | Historical acquisition and public transport owners | Independent performance; explicit ownership and admission limits without fabricated qualification | Native admission, migration lease, capacity, deadline and actual cleanup tests |

No visible console behavior changes, so independent browser review is not selected. Correctness includes simplicity and test value; there is no separate disputed architectural question requiring condensation review. The specification names observability evidence for activation; no executable logger changes in this delivery.

The applicable daily mutation audit was checked: run [43705847856](https://github.com/realkarych/kora/actions/runs/43705847856), source/tooling `2dff3824df3b93d2a0937300219a3978c770cec5`, was a partial, budget-limited continuation. Its reported result modules were `cel/paths.py` and `ir/pricing.py`, not graph history acquisition or this absent endpoint. Its 17 survivors and 90 test errors establish no graph-read result and add no PR mutation campaign. Ordinary final-head CI and the selected design reviews remain required.
