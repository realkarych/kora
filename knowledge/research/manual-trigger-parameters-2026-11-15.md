# Manual trigger parameter acceptance

Recorded 2026-11-15 for [#1635](https://github.com/realkarych/kora/issues/1635). The [approved plan](https://github.com/realkarych/kora/issues/1635#issuecomment-6900116436) composes the existing original-request, complete graph reader, saved parameter evaluator, CEL cut and manual-start writer. This record describes the implemented ownership and verification method; the normative boundaries are in [API](../specs/api-and-agent-surface.md#walking-skeleton-manual-start-issue-1463), [data model](../specs/data-model.md#1324-walking-skeleton-manual-start-acceptance-1461), [first slice](../specs/first-slice.md#skeleton-depth) and [schema validation](../specs/schema-validation.md).

## Selection, interpretation and persistence

Receipt selection precedes parameter evaluation. A matching accepted or skipped receipt retains its original result, including when the selected graph is no longer available for a new start; a different original request under that key remains API004. New accepted and skipped occurrences require the exact supported saved v2 parameter profile. An older graph without it needs redeployment; the reader never fills the gap from current defaults or changes historical interpretation.

The selected candidate keeps its original issuer and request/key association through acquisition, detached checking and the writer. The complete reader verifies the saved identity, canonical source, parameter binding, type inventory, physical resource transport and owner inventory before value validation. Malformed source cannot create a pending run. A completed schema mismatch is API003; unavailable interpretation or exhausted evaluator capacity is API010. Existing installation and agent-secret refusals remain.

The value evaluator receives original parameters. CEL admission uses the original candidate, checked contract and key to build the unconverted cut, then checks both original and converted CEL ingress. The original request and cut are separately preserved: integral floats, signed zero, signed-64 integers, Unicode and escaped NUL do not pass through a converted value on the way to storage. One writer transaction saves the existing pending run, exact original `params`, unconverted `params_projection`, receipt, event and promotion admission. Overlap skip has only its receipt and event. Rollback and cancellation keep the existing writer owner.

The CLI adds only strict JSON construction with repeatable literal `--param KEY=JSON`; MCP forwards an optional object. Both use the shared client and the same HTTP operation. Neither evaluates graph schemas locally. The operation's HTTP body already had `params`; there is no new operation, storage format, migration or generated-client shape.

## Resource inventory

`StartPreparationLimits.admit` checks the complete derived inventory before the native snapshot checkout. `run_trigger_limits` derives its supplied allowances from existing admitted source domains; it does not reuse the deployment's 1-GiB reader or 2-GiB writer allowance for the larger consumer operation. These are logical payload/entry/work reservations, **not physical Python heap, RSS, elapsed-time or service-throughput guarantees**. They do not allocate the allowance. Existing complete-reader and evaluator gates continue independently.

The representation coefficients reuse the [detached-source accounting method](admission-source-accounting-2026-11-14.md#complete-detached-source-preparation): decoding `12w`, rendering `36w`, typed conversion `292w + 45`, auxiliary traversal `12w`, and `32(w + 1)^2` work per operation. Scalar payload and object/member/frame slots are counted; allocator overhead and opaque library allocations are not. The [parameter evaluator](parameter-validation-2026-11-15.md) separately supplies its 16-unit native/decode envelope and exact profile certificate.

Let `P` be the existing 4-MiB original-parameter bound, `I` the smaller logical IR and graph-pair bounds, `D` the existing diagnostic byte limit, `C` the CEL ingress byte limit, `S` the saved parameter schema-source limit, and `T` the existing CEL typed-work allowance. The complete reader supplies `H` retained units and `W` work. Its retained inventory remains charged while its immutable checked graph and prepared schema holder survive, even though its transient peak has passed.

| Owner | Derived charge and reason |
|---|---|
| Native acquisition `N` | The larger of the SQLite native page/overflow bound over every supported page size at UTF-16 widths and `1024 * (pair + physical + metadata + 1)`. The latter conservatively covers the existing native-value scalar/container charges, including the largest fixed float charge; authentic graph publication uses the narrower canonical string transport. This is a native source domain, not an allocation estimate. |
| Owner transport `O` | Twice the logical IR bytes plus 64 times the physical-resource bound, two 64-character node/subgraph names and the longest owner-state spelling. All file-name payload appears in the admitted IR; fixed physical identity fields fit the existing resource transport allowance. The factor two covers SQLite UTF-16. Add 65 rows of 64 eight-byte slots for the bounded row/sentinel inventory. |
| Detached raw snapshot `R` | Existing two-value transport expansion, three UTF-16 digest widths, physical transport, `O` and 1024 fixed owner units. It survives through detached preparation and is destroyed before the writer receives facts. |
| Original request baseline `B` | `356P + 4096`: 16 units for retained original/request data plus decode 12, render 36 and typed 292. Keeping this conservative baseline throughout covers the admitted original input and HTTP model coexistence rather than assuming that their caller has already released them. |
| Decoded graph `G` | `352I + 45`: decode, canonical render, typed model and auxiliary traversal. Installation checking releases the document before parameter value work; CEL mapping may construct a fresh document later. Minimal timeout/retention settings remain charged at `I` afterward. |
| Diagnostics `J` | `328D + 45`: typed and rendered bounded diagnostics, including the installation-refusal handoff. |
| Original value validation `V` | `16(P + S)` plus the existing profile's variable-byte and auxiliary-entry capacities. Source and original value decode are covered before the stricter profile scan; no per-document reset or enlarged profile is introduced. |
| Projection `K` | `16P + cut_scratch + 48C`: original decode, metered path/trie/cut scratch, and cut decode/render envelopes. The cut's original encoded bytes survive into CEL checking. |
| CEL checking `L` | `G + 64T + 64C`: fresh document/mapping inputs; mapping memo, reference, active traversal, type and bound entries conservatively covered per charged work step; original decoded cut, converted tree, ingress walks and retained cut bytes. Schema keys/reference payload remain within the document charge. Mapping state is invocation-local and is destroyed before returning facts. |

The peak retained reservation is:

```text
B + max(N + O + R,
        R + H,
        R + H + G + J,
        R + H + I + V,
        R + H + I + K,
        R + H + I + L,
        2 * (C + J + I + 1024))
```

The last branch covers result/error handoff while the original request baseline remains. Previous facts are explicitly released before a stale latest selection is retried. No raw graph, checked holder or decoded document enters the writer.

Projection scratch is derived from the saved path-section bound `Q` and existing maximum data depth `d`: `16(d + 2)(3d + 4)(Q + 1) + 64P`. `ProjectionAdmission` charges parsed paths, ASCII ordering, trie traversal and copying monotonically; the expression covers cumulative path prefixes and per-value traversal state. Projection work is `32(P + Q + 1)^2`. This supplies a finite outer domain; internal refusal remains authoritative when the admission cannot complete.

Work adds native/owner/raw processing and `W`, eight document/installation traversals at `32(I + 1)^2`, parameter decode/validation `8(P + S + 1)^2` plus the exact profile score, original projection input traversal `32(P + 1)^2`, projection work, `T`, four cut/convert operations at `32(C + 1)^2`, two diagnostic operations at `32(D + 1)^2` and 4096 fixed units. It is summed across phases, not reused after a phase finishes.

The original request body/hash stage occurs before the single preparation slot. Independent callers may retain their admitted original requests or await writer settlement after releasing it. Neither those populations nor aggregate HTTP concurrency are globally bounded by this slot. Arbitrary exception graphs from trusted components also have a separate disposal owner: cleanup is proportional to their exception nodes, edges and traceback frames, rather than secretly borrowing a JSON-source bound. The slot remains occupied through actual cleanup.

## Cancellation and database continuity

One existing deployment preparation slot covers native acquisition through detached preparation and result/error disposal. The worker's lock-protected claim and caller abandonment select a single winner immediately before invocation. Abandonment that wins first prevents the callable from running; if invocation wins, cancellation does not preempt it, and settlement retains the slot until its result or exception graph is disposed. The API wrapper transfers the callable and snapshot into the reservation before awaiting completion, deleting its own snapshot reference. A caller-retained cancellation or fixed-error traceback therefore cannot keep that snapshot alive after the worker settles. Shutdown drains outstanding preparation before its executor/evaluator close. No new timeout or background resource owner is introduced.

The database unit ends before pure decoding and evaluation begin. The acquisition function deletes its local snapshot on every exit, including cancellation during native snapshot settlement after the value has been constructed. Its retained caller exception therefore owns no completed source while the slot becomes reusable. PostgreSQL acquisition uses the explicitly approved temporary walking-skeleton shared-migration-lease exception. Maintenance, restore and cutover must first stop new reads and drain existing reads, including cancellation work, before changing metadata. SQLite remains the shipped skeleton deployment; this exception does not qualify PostgreSQL for full first-slice acceptance.

The writer rechecks existing authenticated-principal scope and selection. It does not freshly authenticate the credential under a transactional revocation fence. Ordinary subsequent requests retain the authentication provider's freshness; revoke-during-preparation protection remains #1237/#593.

## Verification ownership

| Boundary | Focused evidence |
|---|---|
| Real deployment to API acceptance | `tests/api/test_trigger_parameters.py`: exact original and projection bytes, original numeric kinds and NUL, replay/mismatch ordering, invalid versus exhausted decisions, converted-ingress refusal, legacy unavailable-profile fresh refusal with preserved receipt replay; both databases. |
| Writer and promotion | `tests/engine/test_start_acceptance.py`: request/key association, complete corruption refusal before effects, atomic rollback, overlapping keys, stale latest retry, cancelled writer settlement and unchanged inputs through real promotion; both databases. |
| Source admission and detachment | `tests/engine/test_start_preparation.py`: retained/work one-unit-short refusal before checkout, acquisition allowances included, full source success and zero outstanding database units during the pure facts call. |
| Cancellation handoff | `tests/api/test_trigger_preparation.py` retains a real trigger preparation failure/cancellation traceback and checks that its acquired snapshot has disappeared before slot reuse on both databases. `tests/api/test_graph_preparation_completion.py`: deterministic real-thread entry observation on both sides of the invocation boundary, no callable execution when abandonment wins, actual late disposal and slot/resource release. Existing running-work, exception graph and shutdown cases remain. |
| Thin-client parity | `tests/cli/test_trigger_parameters.py`, `tests/cli/test_trigger_command.py`, `tests/mcp/test_stdio_trigger.py` and the existing operation parity test: strict grammar before HTTP, actual CLI/MCP original-value persistence, omitted MCP input, unchanged operation mapping. |
| Safe diagnostics | `tests/observability/test_start_acceptance_logging.py` and captured API debug events in `tests/api/test_trigger_parameters.py`: fixed result categories, no private parameter payload in events. No new diagnostic event is added. |

The PR records exact revisions, executed commands, all earlier failed checks, independent correctness/security/performance results and final-head CI. Local source accounting does not substitute for executable acceptance or supported-platform CI. The inspected daily master audit was partial with pricing/CEL-path scope and supplied no parameter-consumer finding; it adds no PR mutation campaign or merge hold.
