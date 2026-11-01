# Private StructuredOutput schema-call foundation

Date: 2026-11-01. Implementing issue: [#569](https://github.com/realkarych/kora/issues/569), child of [#138](https://github.com/realkarych/kora/issues/138). Implementation base: `239a455ef64f6d2cfeb5d33a14dd004fa23dda5e`.

## Authority and scope

The owner accepted the complete [original-view, dispatch and staging proposal](https://github.com/realkarych/kora/issues/569#issuecomment-6540517129), SHA-256 `547844288e47b4363b039e9c0c268ae71ed830e946c4843ff8f9072e405960c9`, including the two exact ADR paragraphs and normative specification replacements. The [direct-answer receipt](https://github.com/realkarych/kora/issues/569#issuecomment-6549183556) records that adoption. Its observation time is not the owner's message timestamp. No new approval date or retrospective implementation authorization is inferred from that observation.

[ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3.3.4.1](../specs/artifacts/StructuredOutput.md#3341-original-view-and-dispatch-admission) govern this component. The actual independent correctness, security and performance design results are [recorded separately](https://github.com/realkarych/kora/issues/569#issuecomment-6541247960); they do not qualify implemented source.

The internal `kora.ir.structured_schema_adapter` module supplies `decide_prepared_schema(prepared, original_position, value, policy, meter)`. This single-schema operation returns a completed private boolean or propagates refusal/internal error. It constructs a real operation context, schema frames, execution views, pinned engine instances and closed reference calls. It is not a complete effective-contract evaluator or public IR export. Complete composition must share its own context across parent, declared and inline constraints rather than invoke this root operation repeatedly.

All 35 dispatch slots exist. `type` calls the unchanged exact leaf; `$dynamicRef` and `$ref` call their original prepared targets on the unchanged value cursor. `else`, `then`, `minContains` and `maxContains` independently complete without evaluating an absent controlling sibling. The remaining 28 slots raise fresh fixed no-argument internal availability errors. They return neither schema-false nor a public refusal. Existing preparation, numerical/cursor/equality/assertion modules and their tests remain unchanged.

The thirteen policy parameters, their saved identity, the ten preparation capacities, supported platforms, dependencies, package layers, gates and deadlines remain unchanged. Full handler coverage, effective evaluation, common composition and producer/API/CEL/replay/storage/retention/aggregate activation remain with #138 and their implementing owners. The reserved execution identity remains unavailable.

## Boundary and lifetime

The trusted caller supplies factory provenance, structural admission for the identical saved schema, one private `checked_value` snapshot, an exact `NativeCursor`, a bound policy and one shared `EvaluationMeter`. Preparation and exact-class checks do not certify structural validity or saved-record authenticity. There is no caller-injected engine, handler, observer or callback.

One fixed class is built from pinned `jsonschema 4.26.0` draft metadata/type definitions, the literal 35-slot table and `version=None`. Precise private protocols isolate the foreign stub's callback/constructor mismatch. Trusted fixed attribute reads use native `object.__getattribute__`; they introduce no fallback, authored attribute lookup or alternate authority.

Every root/evolution receives the same operation-local inert resolver and legacy `resolver=None`. Views contain only trusted executable keyword literals and bound original value-node IDs. `$schema`, `$id`, annotations and data-valued subtrees never become execution authority. Exact class, resolver identity and disabled legacy resolver are checked after each construction. Booleans bypass engine construction, including reference children. References neither decode again nor consult current aliases, registries, files or networks.

The context has meter, prepared input, borrowed payload root and fixed status. A frame has context, original position, native cursor, depth, optional index and remaining-handler count. A bound keyword has frame and original value-node ID. Frames retain no engine/view. Each call owns its engine/view/iterator through ordinary locals; reference calls receive the parent engine locally. There is no active-frame list, ContextVar, operation closure, cache or frame-to-engine cycle.

Scratch cleanup, frame leave, iterator close and the final closed-status write are paid before their owners become active. They run without later reservations and refund nothing. Caller-held tracebacks can retain operation state after an error; the caller must dispose them before claiming release. The component creates no process, thread, task, descriptor or asynchronous cancellation facility. Cancellation waits for finite synchronous completion; no interrupt or latency guarantee is claimed.

## Ordered accounting

The existing work/byte/entry meter is shared and monotone. All amounts below are logical reservations, not Python allocation sizes, CPU counts or time/RSS guarantees. Let `H=791`, `X=128+16H`, `n` be original member count and `m` executable count.

| Boundary | Ordered work and owned-state admission |
|---|---|
| Caller native cursor | Existing two work and one entry; the checked model snapshot has its separate admission |
| Root administration | Exact policy/holder/cursor/position checks: eight work; position field/count/lower/upper checks: four; root borrow: one; final status write prepaid: one; context allocation/four writes: five and one entry; resolver: 128 and 16 entries. Total 147 work/17 entries |
| Schema entry | Candidate depth and guard: two work. Before activation, atomically nine work/one entry for frame allocation, six fields, entry and prepaid leave; total eleven. Depth 65 refuses before frame allocation |
| Original schema read | Context holder, position table, row, node ID, arena and node: six work. Object kind/branches: three. Boolean kind/value/exact-type/guard path: five, each ordinary action prepaid; complete boolean call: 22 work/one entry |
| Scratch | Original members and length: two. Cleanup: 109 prepaid. Atomic initialization: 108 work/36 entries before header/cells. Header, initialization, 36 tests, 35 appends and 35 advances consume that initialization block |
| Classification | Source cursor/count initialization: two. Each member: eight surrounding actions plus `F(key)=1+35*(1+len(key)+20)`. Each executable member adds one slot write and one count advance. One final source test; no second classification |
| View | After classification, atomically `1+m*(4+H)` work and `1+3m` entries before header/rows/cells. Scan initialization: one; 35 loop tests/reads/branches/advances plus final test: 141; each present slot: one trusted-vocabulary read. Complete view: `V=365+8n+798m+sum F(key)`, `37+3m` entries, zero bytes |
| Scratch disposal | Read actual allocated count `c`, initialize cursor, `c+1` tests, `c` clears and advances, release header: `4+3c<=109`, using prepaid credit even after exhaustion |
| Root engine | Remaining-count write and root/child branch: two. Atomic `C(m)=128+(2m+8)H` work and `34+2m` entries before construction |
| Child engine | Same two surrounding actions; evolve-method read: one; separate atomic `X` work/32 entries, then full `C(m)` admission before ordinary evolve. A refused constructor leaves X paid and calls no evolve |
| Postconditions | Exact engine class, resolver identity and legacy-resolver-is-None reads/guards: six work |
| Iterator | Method read: one; atomic open/future-close-read/close: three work/one entry; initial count branch: one. Initial nonempty advance/handler/bootstrap ticket: atomic 20, or empty advance/completion: atomic two; actual advance follows immediately. Result/yield guard: one |
| Handler handoff | Argument-frame/context/meter bootstrap is inside the admitted initial/successor ticket. Successful handler reads remaining count, decrements/writes and branches for three; then atomically admits successor 19 or terminal one before returning None |
| Mismatch | Atomic two work/one entry before fresh no-argument mismatch: one creation and one immediate-boundary conversion. Only that boundary converts to false |
| Unavailable | After admitted bootstrap, atomic one work/one entry before fresh fixed internal error. Exhaustion wins if this admission fails; no handoff or false conversion |

Type semantics add three reads, the existing three-work/one-entry arena cursor, unchanged leaf cost T and one result branch: `7+T`, before successful handoff or mismatch.

Reference semantics add four initial reads, lower-bound lookup `B=1` for no rows or `2+r.bit_length()` for `r>0`, and the at-most-two-occurrence scan. That scan initializes once; each candidate pays seven reads/branches, a nonmatch pays one advance and a match pays its target read. Its cost is nine for the first occurrence and seventeen for the second. Depth/resolver/dispatch and result branch add four more: `8+B+S+child_work`. Reference rows remain borrowed; no new index, result tuple, value cursor or decoded reference is created.

For a fully traversed object ending, let delta be one for a nonboolean child evolve, D entered handlers, s successful handlers, M a completed mismatch, G an availability error, Z normal engine completion, and P the additional handler semantic prefixes. Then `W=34+V+C(m)+delta*(1+X)+19D+sum(P)+3s+2M+G+2Z`; entries are `73+5m+32delta+sum(handler_entries)+M+G`. Whole root examples add the incoming cursor plus root administration: 149 work/18 entries. Refusals retain only their actual paid ordered prefix; no ending formula charges an unvisited action.

## One verification plan and actual evidence

| Component/boundary | Owner and risk | Blocking evidence | Diagnostic limit / independent coverage |
|---|---|---|---|
| Root/frame/view/engine integration | IR; unpaid work or wrong original position | Real structurally admitted saved schemas, actual preparation, frozen payload snapshot, exact ledgers and atomic fences | Actual correctness/security/performance implementation review required |
| Type/reference/no-op/guard dispatch | IR; false/refusal confusion or silent incomplete behavior | All 28 unavailable slots, four independent no-ops, both reference forms/occurrences, canonical order, sibling assertions, shared meter, 64/65 depth | No public validity or complete-handler claim |
| Foreign engine and closed authority | IR adapter; stock errors, registry/resource crawl or altered class | Real root and child engines, omitted metadata, same inert resolver, disabled legacy resolver, real adverse library yield rejected and iterator closed | Saved authenticity/authorization remain caller-owned |
| Cleanup and safe reporting | IR/caller traceback boundary | Actual record/generator inventories after true/false/refusal/unavailable/unexpected exits and caller disposal; fixed errors and empty output | Owned-state closure only; no process heap/RSS guarantee |
| Frozen publication and repository constraints | IR package | Strict changed-source checks, import layers, no-suppression and frozen contract tests | Final supported-environment CI still required |

Focused adapter evidence contains 112 passing cases in 0.86 seconds on macOS arm64, Python 3.11.17, using the existing locked environment and `PYTHONPATH=src`; local JUnit captures that result. A subsequent test-only strengthening checks the exact 35-slot registry and content-free representations of retained owners; all six affected cases pass in 0.39 seconds, with production source and the other 106 cases unchanged. Source/test strict Pyright reports zero errors/warnings; changed-source Ruff lint/format and pinned Markdown lint pass. Exact ledger cases include boolean 171/0/19, empty 7006/0/91, null type 10353/0/97, completed mismatch 10342/0/98, unavailable 10393/0/97, independent else/then 13578/0/101, object reference 34220/0/207, false target 11250/0/98 and integer/1.0 14534/53/100, in work/bytes/entries order.

Initial nonempty admission tests distinguish 10286 work refusing the ticket at 10267/0/96 from 10287 admitting it and entering the type handler before its next read refuses. They compare only trusted traceback code identities and then discard aliases. Empty 7004 refuses its two-unit ticket at 7003/0/91; 7005 completes the empty advance and refuses the final guard. Physical empty advance follows from the immediate source ordering plus the complete result, not from a counter alone. Other fences cover scratch, whole-view admission, mismatch/error allocation, successor 19, numerical bytes/entries, terminal payment and final guard.

Initial test construction exposed rejected non-native schema-root wrappers and aliased fixture subtrees. Tests now use separate native trees exported from the real structurally checked graph document; production admission was not relaxed. The first executable boolean boundary case exposed a bundled type-read/guard reservation, consuming 169 rather than the accepted 170 at the one-short limit. Separating those two ordinary actions repaired that accounting defect without changing its completed total. The same ordinary-action separation applies to root kind checks, source member fields and engine postconditions, with eight additional prefix regressions. The focused test repair passed; no capacity, timeout or check was weakened.

Import-layer, no-suppression and frozen StructuredOutput contract checks passed: 32 cases. The final-source import-layer/no-suppression delta passes all thirty cases in 5.94 seconds; the two unchanged frozen-contract cases retain their earlier evidence. The initial invocation also selected two unchanged installed-wheel schema-packaging tests; both had setup errors before test execution because the available pnpm failed the existing offline build with “packages field missing or empty”. Those historical setup errors are not reported as passing. No dependency, workspace configuration or build hook was changed or repaired here. Installed-wheel qualification remains mandatory in the final native CI; the local results supply only their named passing checks.

Actual implementation reviews and one successful complete final-head CI remain required before readiness; the PR supplies their revision-matched results. Design approval and focused local results do not substitute for them. No additional full local suite, mutation campaign or repeated qualification is required.

The latest available master mutation run [41189316843](https://github.com/realkarych/kora/actions/runs/41189316843), source/tooling `2b468c836e381e1b487effa5d455b5e74bb53d01`, was successful reporting of a partial budget-limited restored campaign. Its summary and report contain 165 executed, zero reused, 315 pending, 105 killed, fourteen survived and 46 test-error outcomes, confined to pricing and CEL paths. It supplies no applicable adapter finding or adapter mutation coverage. The next ordinary master audit supplies follow-up evidence.

## Reporting and threat boundary

The pure module emits no operational event or authored content. The eventual complete composition retains its operation-completion event and activation tests. Fixed mismatch/unavailable/refusal text contains no schema names, paths, payloads or native chains; an internal error is not a public refusal. Unexpected caller reporting must not format retained locals or native exception chains.

Untrusted bounded schema-controlled work maps to TM-04/TM-10; content-free outcomes and traceback disposal to TM-06/TM-07; original closed target/occurrence identity, distinct from authenticity and authorization, to TM-08/TM-11; immutable interpretation to TM-12. This delivery changes no actor, execution authority, deployment assumption, saved format or threat-model status.
