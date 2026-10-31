# StructuredOutput collection predicates: scope and evidence

Date: 2026-10-31. Implementing child: [#548](https://github.com/realkarych/kora/issues/548), parent [#138](https://github.com/realkarych/kora/issues/138). Baseline: `523d22ebf310a38eb85ccb52c73a02b0aaf7a72f`. The [adopted implementation/verification plan](https://github.com/realkarych/kora/issues/548#issuecomment-6537798487) preserves the exact proposal accepted under the existing #515 policy. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3.3](../specs/artifacts/StructuredOutput.md#33-exact-bounded-evaluation) remain authoritative. This record grants no new support or execution authority.

## Private interfaces and ownership

The existing-layer [`structured_schema_collection_predicates.py`](../../src/kora/ir/structured_schema_collection_predicates.py) has no public package export. Its functions return a completed private boolean or propagate the existing fixed refusal or unexpected programming error.

| Interface | Caller prerequisite and local responsibility |
|---|---|
| `unique_items_matches(instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter) -> bool` | Borrow the one admitted private native tree and original boolean keyword value; create fresh left/right child handles for each visited pair and use existing iterative equality |
| `required_names_present(index: NativeObjectIndex, argument: ArenaCursor, meter: EvaluationMeter) -> bool` | Borrow an exact already built index for the applicable owned object and an original required-name array; test saved names until the first absence |
| `dependent_required_present(index: NativeObjectIndex, argument: ArenaCursor, meter: EvaluationMeter) -> bool` | Borrow that same index and an original dependency map; visit canonical triggers, then saved required names only for present triggers |

The model/raw/arena owners retain whole-domain exact-kind, Unicode, finite-number, bit/depth/cycle and duplicate-name admission. The caller owns one checked private tree, original immutable schema arena, prior structural validity and no concurrent mutation. A cursor or exact index class is not an admission, provenance or object-binding certificate. The index must come from the existing factory for that owned object and remain private/unmodified. Defensive visited checks do not validate skipped data or rescan its rows.

No predicate builds, sorts, clears or frees a borrowed index. Future wrappers retain object applicability, the frame's one lazy index, empty-before-index behavior, exact prefix/handoff charges and mismatch construction. Empty requirements can complete in that future wrapper before a missing index is constructed; a predicate's empty behavior applies when its exact index is already supplied. These wrappers and complete keyword execution are not delivered here.

## Order and short circuits

Uniqueness reads native applicability first; nonarrays do not inspect the argument. For arrays the visited flag must be an exact boolean. False returns before native length or child access. True visits `(0,1),(0,2),...,(1,2),...`, with two fresh charged handles and one existing `json_equal` invocation per pair. The first completed equal pair returns false. Empty/singleton arrays have no pair. There is no pair vector, fingerprint, cached left handle, identity discount, native composite equality or conversion cache.

Required names preserve their original saved array order. Each visited name must be an exact string; its single sorted lookup determines presence. The first missing name stops before later names. Dependent-required traverses the original arena's canonical code-point trigger order; each trigger receives one lookup. False/null/zero-valued members are present. An absent trigger creates no value cursor and does not inspect that value's shape. A present trigger creates one arena cursor and calls the required-name predicate using the same index/meter. Refusal never becomes absence or completed false.

## Exact local accounting

The immutable section 3.3 schedule and the issue plan define the complete ledger. Every introduced guard/read, loop condition and cursor initialization/advance is prepaid. Existing accessors/equality/lookup retain their complete charges; their internals are neither replaced by a new bulk nor charged twice.

An exact-index class guard costs 2 before method access. Native/arena child creation costs 6/11 work and one entry. Borrowed arena key access costs 11; an arena object-value cursor costs 12 and one entry. Existing `find` costs `F(q)=4+B(q)`, with empty `B=1`, otherwise `B=1+(n.bit_length()+1)*(1+len(q)+M)` using the index's stored maximum key length. It allocates no entry. Existing `J` below denotes complete iterative equality and its separate state reservations. `KN` denotes native kind cost, including array 10.

| Predicate path | Work, excluding received handles/index construction |
|---|---|
| Required setup / empty completion | 24 / 25 |
| Present required name / first absent name | `37+F` / `36+F` |
| Dependent-required setup / empty completion | 26 / 27 |
| Absent trigger | `14+F` |
| Present satisfied / failed trigger | `27+F+R` / `26+F+R`, with complete nested required cost `R` |
| Unique inapplicable / false-array flag | `KN+1` / `KN+22` |
| True-array setup | `KN+27` |
| Distinct pair / first equal pair | `15+J` / `14+J` |
| Completed unique array | `KN+28+4r+sum_pairs(15+J)`, where `r=max(0,n-1)` is explanatory only |

Successful membership completion adds the final paid loop condition. Each visited required name adds one arena handle; each present dependency adds one value cursor plus nested handles, with no new index. Each unique pair adds two native handles plus equality's existing state. True empty/singleton uniqueness costs 38 with no child handles. False-array uniqueness costs 32 without reading length.

With an index `{a:false}`, required `[a]` costs 73 work/one entry, `[b]` costs 71/one, and `[a,b]` costs 119/two. Dependency `{a:[]}` costs 90/one and `{a:[b]}` costs 134/two. An immediate null duplicate costs 90 work/five entries including existing null equality. Exact/one-short tests independently freeze these paths, atomic handle prefixes, equality byte/entry admission and repeated shared-meter charging. No release refunds reservations.

## Failure, release and diagnostics

Predicates add no logical frame, result row, vector, byte buffer, global context/cache, process, thread, task, descriptor or asynchronous interruption. They retain only bounded local positions and borrowed references. Existing equality owns its temporary stack, frames, conversions and comparison indexes. Ordinary return drops predicate locals; a caller-held exception/traceback can retain references until explicitly disposed. Borrowed caller state must remain alive and unchanged while testing that no extra helper state remains.

Expected unsupported/exhausted outcomes propagate content-free `EvaluationRefused`. Unexpected errors propagate to the existing safe reporting owner. No key/value/schema text, native chain, `ValidationError`, diagnostic path, output or per-pair event is produced. Event ownership stays with the eventual evaluator. TM-04/TM-10 retain input/callback/resource ownership, TM-06/TM-07 content/error/traceback handling, and TM-08/TM-11/TM-12 immutable authority and activation separation. No actor, trust assumption or threat-model amendment is introduced.

The module supplies no accepted value, effective digest/summary authority, engine/schema frame, complete handler/evaluator/composition or real consumer. The reserved execution identity remains unavailable. Count assertions, including minLength/maxLength, and their separate domain/order question remain excluded. No dependency, layer, format, platform, parameter, limit or gate changes.

## Implementation evidence and limits

Separate [actual design results](https://github.com/realkarych/kora/issues/548#issuecomment-6537841659) passed correctness/security/performance for the exact adopted plan. These establish design acceptability only. Actual source/evidence review and complete final-head CI remain required before readiness.

The first complete focused semantic input passed 84 cases in 1.19 s with zero failures/errors/skips. A later six-case targeted delta passed in 0.54 s, with 84 deselected, proving a caller-owned index remains usable and row-identical after false/refusal/unexpected error. Existing case values/assertions/order are preserved; only test typing and equivalent literal bindings changed alongside that new delta. Source stayed unchanged. This is 90 cases with matched original inputs and six fresh cases, not a claimed repeated complete 90-case execution. Raw original/final identities and JUnit belong to the implementing PR.

Touched Ruff lint/format and strict Pyright passed after explicit test-container typing. Focused import-layer/no-suppression checks passed 30 cases in 5.88 s; changed specification Markdown and diff checks also passed. Final actual C/S/P source results and complete Linux/macOS CI with ordinary database/frozen-publication/installed evidence remain required and recorded by the PR. No new DB fixture is necessary for these pure functions. Supported-platform CI must execute the final complete test file. Runtime durations are diagnostics, not CPU, heap/RSS or latency guarantees.

Production source is 121 added lines, SHA-256 `aed3bf8e3ab8f55642d269ea9a9557aa7ca382cb6694b72a54ace6c7b887b247`. The retained 84-case test snapshot is `f0e053bbdff7fe0df9e1156c4407ceb5f17e112bb9208ccb0a88cc604c68d8be`; final tests are `bae9638de50b1b18d9d315c0012abd413c037d93c1a93bc1f6e67b418f0eae0c`. Their bounded diff contains equivalent typing/literal bindings and the six new borrowed-index cases. The implementing PR binds raw JUnit and final revisions; no unchanged semantic campaign was repeated solely for that bookkeeping.

The latest available master audit remains run `41025696626` at source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`; its previously inspected partial pricing/CEL report has no applicable collection-predicate finding or equality mutation confirmation. A fresh metadata query found no newer run. It adds no PR mutation campaign, required status or stale-report hold. Remaining complete execution and consumer activation stay with their existing #138 owners.
