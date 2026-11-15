# Reading one saved graph version

Design delivery: [#1634](https://github.com/realkarych/kora/issues/1634). Status: proposed contract, pending final owner acceptance; no route or client is activated. Implementation requires a separately approved plan and the prerequisites in section 8. This proposal makes the single-version operation in [the API specification](api-and-agent-surface.md#2-resources-and-operations) precise. It does not implement graph lists, latest-version lookup, starts, execution, attempt reads or a console view.

The governing decisions are [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md), [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md) and [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md). Saved values and their identity follow [data-model section 11.1](data-model.md#111-graph-version-contract). General authentication, request correlation, methods and error envelopes follow [API conventions](api-and-agent-surface.md#1-conventions). The [source and delivery inventory](../research/graph-version-read-2026-11-15.md) records implementation gaps, not executable evidence.

## 1. Selector and authorization

`GET /api/v1/namespaces/{ns}/graphs/{graph}/versions/{gv}` selects exactly one immutable version. `ns` is the namespace name, `graph` the graph name and `gv` its public `gv_`-prefixed canonical ULID. An ordinal, digest, `latest`, query parameter, request body or additional selector is not accepted. There is no implicit version choice, fallback to another graph or cross-namespace search.

Apply these checks in order:

1. Authenticate using the ordinary personal-token boundary. Missing or invalid credentials produce `API009`. A service principal produces `API005`.
2. Check the explicit namespace against the personal principal's grants, before resolving its existence. A namespace outside those grants produces `API005`, including a nonexistent namespace.
3. Validate the bounded route selector. Invalid syntax or unsupported query/body selection produces `API003` without reading graph content.
4. Resolve namespace, graph and version through one namespace-scoped join. A missing graph/version, a version belonging to another graph, and an inaccessible version ID all produce the same `API001`. Knowing a digest or ID grants nothing.
5. Admit the content work and read one coherent snapshot. Recheck the exact scoped identity in that snapshot; a prior identity lookup is not authority for an unscoped value read.

Ordinary namespace access permits the authored graph IR and the public saved contract. It does not require `approve`, `platform` or `transcripts`. The operation never loads a sealed execution request, resolved runtime config, feedback, transcript, artifact payload or content-bearing attempt comparison. Those different sources retain their existing `transcripts` and owner checks. Authored source is not relabeled as rendered execution content.

Active and retired versions have the same read permission. A read never changes eligibility, retention, owners, original admission evidence or stored values. Authorization is evaluated for this request; a completed response supplies no later execution or read grant.

## 2. Complete response and lossless documents

HTTP 200 returns exactly these members, in this order. The HTTP content type is `application/json`; bytes are compact ASCII JSON with no content encoding. Responses and failures use `Cache-Control: no-store`; there is no ETag, range response or conditional-read shortcut.

| Member | Saved source and representation |
|---|---|
| `namespace` | The authorized namespace name. |
| `graph` | The graph name from the scoped graph row. |
| `graph_version_id` | `gv_` plus the selected stored version ID. |
| `number` | The selected positive version number. |
| `status` | The saved `active` or `retired` status. |
| `integrity` | `paired_verified` or `legacy_recorded`, as section 3 defines. |
| `ir` | A graph document containing the complete saved IR. |
| `execution_contract` | A graph document containing the allowlisted saved projection below. |
| `ir_digest` | The original saved lowercase 64-character SHA-256 digest. |
| `execution_contract_digest` | The original full saved contract digest, including excluded internal members. |
| `deployment_digest` | The original combined deployment digest. |

A graph document has exactly `{"format":"kora.graph-document.v1","json":"..."}`. `json` is an ASCII JSON text whose root is an object. It is intentionally a string in the outer response. Consumers must not silently replace it with a decoded generic JSON object: ordinary JavaScript numbers lose large integers, and ordinary encoders can reject lone surrogates. The format is a transport representation, not a persisted encoding or an execution-semantics version.

The inner text preserves the saved logical values, object member order, array order, omitted members, integer versus floating-point spelling, negative floating zero, escaped NUL and escaped lone surrogates to the extent those distinctions exist in the stored source. It contains no raw control characters, duplicate object names or nonfinite numbers. It does not normalize a historical schema into today's schema. Inspection and CLI display can keep the text opaque; an application choosing to decode it must use a codec that preserves its domain. The generated TypeScript surface exposes this string contract and never calls `JSON.parse` on the inner text implicitly.

The public contract projection permits only the following top-level members, in their saved order and only when recorded: `semantics`, `type_contracts`, `parameters`, `parameter_evaluation`, `outputs`, `retry_policies`. Each permitted member retains its complete saved value; omission never becomes null, an empty collection or an inferred default. The projection always excludes `cel_expressions`, `cel_projection_paths` and `command_resources`, and does not fetch physical storage references, resource owners, admission witnesses or verification evidence. An unknown top-level contract member is refused rather than exposed or silently treated as an internal member. No current registry or provider supplies a replacement definition.

This projection does not change identity: hashing its public text does not reproduce `execution_contract_digest`. The response contains no projected digest, execution eligibility claim, executable environment, download URL or storage authority. The API completes the full response before starting delivery; it never sends a successful prefix followed by an error document.

## 3. Historical compatibility and integrity

| Stored case | Required outcome |
|---|---|
| Physical encoding 1, recognized nine-member envelope | Verify the complete paired encoding and all three original digests, then project; `paired_verified`. |
| Physical encoding 1, recognized older eight-member envelope without `parameter_evaluation` | The same identity checks; keep the profile absent and do not synthesize v2 parameter behavior. |
| Recorded v1 type/schema policy or absent newer schema binding | Preserve it; no current evaluator, provider installation or CEL preparation is needed to read it. |
| NULL physical encoding, ordinary legacy object pair | Read the actual persisted objects through separately qualified bounded legacy acquisition; `legacy_recorded`. Preserve recorded omissions and original digest fields. |
| Retired version, missing current plugin or unsupported current execution profile | Read under the same historical rules; none of these conditions alone refuses history. |
| Missing original effect-read admission witness | It cannot authorize execution; historical acquisition must prove its own bounded read without manufacturing that witness. |
| Mixed paired/legacy values, unknown physical encoding, unknown envelope member, nonobject root, duplicate name, nonfinite value or malformed fixed metadata | Fixed `API010`, no partial response and no fallback decoder. |
| Encoding-1 digest mismatch or noncanonical paired text | Fixed `API010`; never recompute and overwrite a digest or return the damaged source as legacy. |

Encoding 1 uses the complete original canonical ASCII `I` and `C` from [physical graph-value encoding](data-model.md#graph-value-physical-encoding). Its identities are SHA-256 of `I`, of `C`, and of `{"ir":` followed by `I`, `,"execution_contract":`, `C`, and `}`. All checks precede public projection. Lexical scanning must preserve arbitrarily long admitted integer tokens without converting them to machine integers; finite float handling uses the established graph codec. Current execution validation is not an integrity check for a historical document.

NULL legacy does not mean encoding 1. Its stored digest fields remain historical recorded assertions: the reader does not claim to verify their original preimages with a new codec. In particular, PostgreSQL JSONB may already have discarded authored object order or number distinctions. Return the database's actual persisted value/order, and do not claim to reconstruct lost spelling. SQLite legacy text preserves only what its original writer stored. The `legacy_recorded` tag makes this limitation visible. A legacy source is still subject to object shape, duplicate/nonfinite rejection, closed top-level contract projection and all resource bounds. Integrity classification follows physical representation, not whether the version can execute today.

## 4. Native acquisition and snapshot release

Use a dedicated historical reader. `graph_version_by_number` loads an unbounded ORM row, `acquire_graph_snapshot` loads unrelated physical resources/owners, and complete execution-contract preparation depends on current execution semantics; none is a substitute for this operation.

Admission must precede each potentially large native materialization, detoast, conversion, driver transfer and detached parse. A `length()` predicate applied after JSON serialization is not such admission. Trust the migration-owned schema and authoritative writers as [data-model principle 7](data-model.md#1-principles) specifies; this operation adds no schema-attestation scheme.

The snapshot owner admits a finite source allowance before checkout, resolves the exact scoped row and reads compact metadata before large values. Original graph admission may support encoding-1 acquisition only where its complete query/projection qualification still applies. A changed subset projection does not inherit proof automatically. Witnessless paired and NULL legacy records require a separate ephemeral admission path; it writes no database row and grants no effect authority.

For SQLite, qualify metadata-only storage-class and octet-width inspection, including all preceding wide fields that a later field access can traverse. Admit the complete native record before projecting either document. Execute the admitted value projection as a subsequent statement in the same snapshot; do not rely on optimizer ordering of independent predicates. Include overflow records, UTF-16 databases and wide unrelated columns in the qualification.

For PostgreSQL, admit uncompressed native input before detoasting. Compressed size alone is insufficient. A qualified conservative expansion bound may admit known compression methods; unknown methods refuse before expansion. Legacy JSONB acquisition uses an incremental structural walk with bounded scalar conversion, not whole-container `::text`: native numeric values can have much larger decimal text than their physical storage. Charge every repeated source visit before it starts, bound the traversal stack and key storage, and admit each scalar's conversion allowance before serialization. The [research record](../research/graph-version-read-2026-11-15.md#native-qualification) distinguishes this design from primitives that exist today. Failure to qualify either database blocks activation; an unbounded fallback and a blanket legacy refusal are both unacceptable implementations.

PostgreSQL also requires the shared schema-continuity migration lease in [scheduler section 10](scheduler.md#10-standalone-sqlite-versus-postgres) on the original backend before the repeatable-read snapshot. Its single try-lock, acquisition transaction closure, snapshot rollback, exact unlock and ambiguous-holder retirement rules apply. The existing Claim/initial-admission walking-skeleton exceptions do not extend to this read. SQLite uses the existing deadline-bound deferred snapshot. All large values and compact identity fields come from one snapshot.

Copy admitted source text and fixed metadata out, end the snapshot and settle backend/lease cleanup before detached integrity checking, public projection, encoding or delivery. A failure to release safely prevents response publication. Connection cleanup may continue after the useful-work deadline; it retains its checkout and operation slot until actual rollback/unlock or verified backend retirement. Never return a backend with ambiguous lease state to the pool.

## 5. Finite resources and lifetime

These are proposed operation-specific product and deterministic accounting limits, not inherited graph-deploy limits or the separate [bounded-read projections](bounded-read-projections.md) defaults. MiB is 1,048,576 bytes. The limits do not assert a process RSS or wall-clock performance guarantee.

| Resource | Bound and admission point |
|---|---|
| Complete original inner document pair, `B` | 16 MiB of canonical ASCII source text, including excluded contract members; admit before detached scanning. Legacy rendering stops before exceeding the same allowance. |
| Fixed response metadata | 8,192 encoded bytes, including member names, tags and punctuation outside the two document strings. |
| Complete HTTP response, `H` | `2B + 8,192` = 33,562,624 bytes; outer escaping of ASCII source text costs at most twice its length. Check actual final framing too. |
| Native source admission | 128 MiB simultaneously retained native source allowance, including both documents and any record/tuple fields traversed to reach them. |
| Native repeated traversal | 256 MiB of cumulative charged source visits; charge each visit its conservative complete native source bound before issuing it. A small result does not reduce the charge. |
| Detached live workspace | 512 MiB of accounted capacity, reserved before allocation; count every live source, projection, encoded frame, scratch buffer, traversal stack and key index. Native-source allowance is separate and cannot borrow from this pool. |
| Structural depth | 1,024 containers in either inner document. Refuse before pushing beyond this limit. |
| Detached work | `64B + 1 MiB` charged byte/character visits; charge scanning, hashing, canonical verification, copying, escaping and key comparisons before execution. Do not materialize arbitrary-size integer objects or use uncharged sorting, repeated searches or collision-dependent work. |
| API concurrency | One active graph-version read per serving process, with no waiting queue. Its independent slot is reserved before large source acquisition and held through delivery and actual cleanup. Other API operation pools retain their existing limits. |
| API useful-work deadline | Absolute 15 seconds from request entry, including authentication, acquisition, CPU work and delivery. Database work also has a deadline no later than entry plus 5 seconds. No phase resets either deadline. |

The workspace accounting implementation must enumerate and test its own representation charges before activation. Byte buffers count allocated capacity; text counts four bytes per code point plus fixed object capacity; container/frame/index entries count their complete fixed allocations and referenced storage once per retained allocation. Fixed allowances must dominate the chosen Python/native representation on supported platforms. This is an owned allocation bound, not a small post-GC delta test. No full decoded generic JSON tree is required: retain texts, bounded indexes and token ranges for the selected projection.

One owned worker executes detached scanning and framing off the event loop. At most one work item is submitted; cancellation of an awaiting coroutine does not release the slot while that worker can still use source buffers. Cooperative checkpoints stop useful work; shutdown joins actual work and releases retained buffers before returning capacity. Closing a response or timing out a slow client retains the slot until its send task and all readers/workers have actually stopped. A cleanup failure cannot report a released resource. Slow delivery holds no metadata snapshot or database connection.

All deterministic source, representation, depth, work and frame refusals use fixed `API010`, retryable false, empty details. They do not expose sizes, digests, native paths or malformed source text. A busy read slot or an expired useful-work deadline before headers uses `API011`, retryable true, with respectively `details.reason = graph_read_busy` or `graph_read_deadline`. A recognized transient database failure uses the existing safe dependency disposition. Migration-lock contention follows the existing `API002`, retryable true, `lock_timeout` contract. Unexpected failures remain `API010`. After headers start, abort the incomplete stream; append no second response and claim no success. Cancellation is settled, not converted into a successful empty document. Clients perform no automatic retry.

## 6. Shared client, CLI and authoring MCP

The shared client owns the operation, its typed selector, framing validation, authentication and error preservation. Add a per-operation response allowance of `H` and a 20-second absolute default deadline that includes connection-pool wait, transfer and validation. An explicitly configured shorter caller deadline wins. The client does not parse inner graph texts through its ordinary JSON-object decoder. It checks the outer closed shape, document tags and framing off the event loop under one owned graph-read slot, with at most `4H` accounted live bytes and the same `64B + 1 MiB` work allowance. It retains the slot until validation settles and ownership of the complete result is handed to the caller. Excess local concurrency refuses before sending with existing client `CLT001`, reason `pool`, outcome `not_sent`; no invented API request ID. Existing `CLT002` deadline, `CLT003` response-limit/schema and `CLT004` stream failures remain client-origin failures. API errors are forwarded unchanged.

CLI: `kora graphs <graph> --version <gv_…> [--namespace <ns>] [--json]`. Namespace resolution uses the existing explicit flag/environment configuration. `--version` is required for this delivery and accepts only the exact public version ID. One invocation makes one GET; no deploy, trigger, plugin discovery or local execution validation occurs. `--json` writes the complete API response bytes to stdout; human output displays identity followed by the two inert ASCII document texts without reparsing their numbers or interpreting control/terminal escapes. Diagnostics use stderr. A refused or interrupted read emits no successful result; interruption settles the client and exits 130. Existing ordinary CLI error and exit contracts otherwise apply.

Authoring MCP extends the already planned `kora_reference` tool with the closed selector `{"kind":"graph_version","namespace":"…","graph":"…","version":"gv_…"}`. All four fields are required; unknown fields and other modes are refused until their own implementations exist. It uses the configured personal MCP credential and exactly the shared client's GET. No operating-server mutation or additional curated tool is added.

This selector returns one text content block containing the exact complete API JSON response, with no `structuredContent` duplicate. The outer protocol envelope uses ordinary MCP framing. Its complete encoded frame is bounded by `M = 2H + 8,192` = 67,133,440 bytes, including JSON-RPC correlation and text escaping; oversized protocol metadata refuses before the API call. One graph-reference slot per MCP process remains owned through client work, frame serialization, stdio backpressure and actual final flush or abandoned-output cleanup. Frame serialization runs off the event loop under at most `H + 2M` accounted live bytes and `8M` charged encoding/copy visits. The total call deadline is 25 seconds from dispatch and includes the client deadline, framing and output. An MCP cancellation, input EOF or process shutdown closes the client call and settles serialization/output before releasing the slot. A second graph-reference call is refused through the existing tool-failure envelope without an HTTP call; unrelated tool dispatch and cancellation remain responsive. The serializer must not route this large result through the current synchronous dual text/structured-result helper.

HTTP, shared client, CLI and this MCP selector ship together, with the parity test and generated OpenAPI/TypeScript contract. No visible console feature is included. A generic object client must retain the tagged texts exactly; choosing to decode them is an explicit consumer responsibility.

## 7. Diagnostics and acceptance

Apply [observability](observability.md) from first implementation. The API owns one bounded completion/failure event with request correlation, operation name, safe disposition, admitted byte/work counters and stage durations. Shared client, CLI and MCP retain their own request correlation without copying credential, selector text, graph documents, contract values, raw parser exceptions, storage identities or digests into diagnostics. Diagnostic failure cannot block completion or cleanup. No durable domain/audit event is created for this read. The implementing issue adds the activation row and exact event catalog entries before the route is enabled.

The following are named future tests, not passing evidence of this design-only PR. Both metadata databases and Linux/macOS apply where native behavior or process lifetime is involved. Use real API/client/CLI/MCP boundaries, deterministic synchronization, no mocks and injected deadlines rather than sleeps.

| Future acceptance test | Required evidence |
|---|---|
| `test_graph_read_authorization_precedes_source` | API009/005 precedence, outside-grant nonexistent namespace, service principal, malformed selector, foreign graph/version IDs; no content acquisition on refusals; ordinary personal grant works without transcripts. |
| `test_graph_read_exact_saved_projection` | All six allowed members and all three excluded members, unknown member refusal, complete original digest verification, no physical owner/reference read, no current plugin/evaluator invocation. |
| `test_graph_read_historical_matrix` | Both physical forms; eight/nine-member contracts; old policies, absent newer bindings/profile, active/retired; legacy recorded identity limitation; malformed/mixed/unknown sources fail closed. |
| `test_graph_read_lossless_documents` | Huge integers, integer/float distinction, negative zero, Unicode, NUL, lone surrogates, key/array order and omission survive API, Python client, TypeScript outer decoding, CLI and MCP exactly. |
| `test_graph_read_native_admission` | Native refusal precedes large materialization; SQLite overflow/UTF-16 and wide preceding fields; PostgreSQL compressed/external values, expanding numerics and repeated visits; witnessed and witnessless paths; boundary values and one-unit excess. |
| `test_graph_read_snapshot_and_migration_lease` | Coherent scoped identity/value snapshot; actual migration exclusion; cancellation at acquire/setup/read/rollback/unlock; ambiguous backend retired or still held; no connection retained during detached work or slow transfer. |
| `test_graph_read_owned_capacity` | Native/workspace/work/depth/frame limits, busy refusal, actual allocation accounting and thread lifetime; no slot reuse while cancelled work can access buffers; buffers released on success/failure/cancellation/shutdown. |
| `test_graph_read_stream_failure_and_deadline` | One absolute deadline, stopped transfer, incomplete response never parsed as success, no appended error after headers, send backpressure without database retention, responsive unrelated requests. |
| `test_graph_read_client_cli_mcp_parity` | Exact operation/selector, one GET, matching API refusals, local client errors distinct, no implicit decode or retry; CLI interrupt; MCP one text block, full-frame bound, flush/EOF/cancellation ownership. |
| `test_graph_read_diagnostics_are_content_free` | No saved content, digest, credential or raw failure in emitted events; safe correlation and counters; failed sink cannot alter the result or resource release. |

Latency, throughput and process RSS are diagnostics. Deterministic owned limits, authorization, original identity, coherent snapshots and actual resource release are blocking. These obligations add no repeated CI campaign or new numerical performance gate.

## 8. Delivery and activation

#1634 delivers this design and its source inventory only. The final owner decision must explicitly accept the tagged-document public format, legacy integrity distinction, explicit version selector, operation limits and text-only MCP result. No persisted format, dependency, supported platform or existing migration exception changes here.

Runtime activation depends on qualified native historical acquisition on both databases, the shared PostgreSQL migration lease and end-to-end API/client/CLI/MCP ownership. Existing internal readers prove neither the complete history domain nor these new resource bounds. A future plan must name the implementing issues and record the actual production-line estimate before editing. It must include all native acquisition, codec/projection, lifecycle, client and transport glue, and preserve the 1,000-line limit for each independently useful deliverable. The [delivery inventory](../research/graph-version-read-2026-11-15.md#implementation-deliveries) identifies a reusable migration-coordination result and the unresolved size of the complete public operation. Do not start a knowingly oversized implementation or split route/client/parity into artificial intermediate deliveries. If the complete operation still exceeds the limit, return a bounded alternative or indivisibility request to the owner first.
