# StructuredOutput container-count assertions: scope and evidence

Date: 2026-10-31. Implementing child: [#564](https://github.com/realkarych/kora/issues/564), parent [#138](https://github.com/realkarych/kora/issues/138). Baseline: `2b468c836e381e1b487effa5d455b5e74bb53d01`. The [complete implementation/verification plan](https://github.com/realkarych/kora/issues/564#issuecomment-6539518903) preserves the accepted [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3.3](../specs/artifacts/StructuredOutput.md#33-exact-bounded-evaluation). Its [new actual correctness/security/performance design results](https://github.com/realkarych/kora/issues/564#issuecomment-6539563032) assess the container-count composition; prior string-length results establish shared facts only.

## Private interfaces and closed call sites

The existing `kora.ir.structured_schema_assertions` module appends private `min_items_matches`, `max_items_matches`, `min_properties_matches` and `max_properties_matches` functions with one private comparison helper. The array wrappers pass only the literal `array`; property wrappers pass only `object`. This trusted implementation operand is established by those closed source call sites, not by a type annotation or caller-authored keyword map.

Each call borrows the caller-owned exact `NativeCursor` over one checked private list/dict snapshot, the original arena bound cursor and shared meter. The input interface is not widened to an arena instance or object index. The caller owns admission, no concurrent mutation and prior syntax validity for the identical saved schema. Constructed cursors and preparation success certify none of those prerequisites.

The shared mathematical count domain remains nonnegative JSON integers, including admitted finite integral floats and signed floating zero. Preserve original kind/spelling/identity. Real structural syntax validation rejects boolean, negative and fractional counts before the prerequisite is claimed. The pending structural-owner-to-identical-saved-arena bridge under #138/work items 3 and 8 remains unimplemented. A private leaf's ignored malformed argument proves local noninspection only, never whole-schema acceptance. No local classifier, normalization or new domain rule is introduced.

## Exact administrative order

Read instance kind and prepay its comparison/branch against the trusted required kind. Inapplicable instances return the private marker; the wrapper pays the marker branch and completes true without argument inspection or count reading.

For applicable instances, read original arena argument kind and prepay the numeric guard. A visited nonnumber refuses before bound borrowing, native counting or numerical conversion. Borrow the original bound once through `scalar_value`, then call the existing `container_count` once. Its exact-native branch reserves two work units before the cursor check and an atomic two before the field/length action. It reads native `len` directly without item/member traversal, index, copied collection or per-item state. Minimum/maximum properties alone never trigger the schema frame's lazy object index.

Invoke `numeric_compare(count, bound, meter)` once, native count left and original bound right. Preserve its mandatory conversion, zero/sign/alignment and capacity schedule. The wrapper pays marker and relation branches; minima use `>= 0`, maxima `<= 0`. Refusal/programming error propagates, never becoming completed false. There is no huge-bound shortcut, loop bound, cache or reset. Each invocation reads cardinality once; consecutive keywords do not acquire a hidden cached count.

## Exact surrounding ledger

`N` denotes the complete unchanged numerical comparison, including both conversions and all work/byte/entry reservations. Received handles retain their caller charge. Every new action is prepaid; release never refunds the cumulative counters.

| Action/path | Array | Object |
|---|---:|---:|
| Native kind | 10 | 11 |
| Applicability branch | 1 | 1 |
| Arena integer / float kind | 14 / 15 | 14 / 15 |
| Numeric guard branch | 1 | 1 |
| Original bound scalar borrow | 6 | 6 |
| Existing direct native count | 4 | 4 |
| Prefix before comparison | 36 / 37 | 37 / 38 |
| Complete integer-bound leaf | `38 + N` | `39 + N` |
| Complete float-bound leaf | `39 + N` | `40 + N` |
| Inapplicable leaf | `KN + 2` | `KN + 2` |

For one item/member against integer `1`, `N` is 38 work, no auxiliary bytes and two entries, giving 76/77 leaf work. Against `1.0`, `N` is 4,186 work, 51 bytes and four entries, giving 4,225/4,226 leaf work. Initial handle creation adds five work and two entries in complete-call quota fixtures.

Boolean-bound refusal costs 25/26 work with no new numerical state. Integer paths have spent 32/33 work before direct counting; its first reservation reaches 34/35. If the final two-unit reservation fails, that earlier prefix remains paid and neither field/length access nor numerical conversion begins. Independent exact/one-short fixtures pin this atomic boundary, complete work/byte/entry fences and consecutive-call charging. These are logical support reservations, not timing or Python allocation measurements.

## Ownership, safety and test scope

Only borrowed call-local references, a trusted fixed kind and bounded count/result scalars are added. Existing numerical code owns its temporary records/scratch. There is no new cursor, index, vector, frame, cache, iterator, process, task, thread or descriptor. Ordinary unwind releases helper locals; a caller-held exception can retain them until explicit traceback disposal. Cancellation waits for the existing finite synchronous boundary.

The focused tests exercise all four structural keyword mappings, cardinality rather than truthiness/uniqueness/key length, inclusive edges, original scalar/arena identity, opposite-container inapplicability, visited shape guards, count-read atomicity, independent quota fences, shared repeated calls and callback/content protection. One admitted dense array and one object exceed the schema-node capacity while remaining within the separately frozen payload domain. Tiny evaluation-entry allowance permits their direct cardinality decisions without indexing or per-item state. These fixtures are not multiplied across all lifecycle and numerical cases.

Representative true/false/refusal/unexpected-error endings cover both container kinds and distribute min/max wrappers. They use the existing real exact-owner inventory and explicit caller traceback disposal. Existing ten-leaf source bodies and complete assertion/string-length test files remain byte-identical. Relevant numerical/cursor facts are reused with matched inputs; no unchanged numerical campaign, new observer, GC replacement, mock or performance method is introduced.

The implementing PR records actual commands, revisions, raw JUnit, strict/architecture/publication evidence and same-context implemented-result reviews. One successful complete final-head supported-platform CI remains required; design results and old campaigns establish no new executable pass. Whole feedback duration is diagnostic; job/test/cleanup, integrity and owned-resource gates remain blocking.

The latest available successful master audit remains run `41025696626` at source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`, dated 2026-10-30. Its previously inspected partial pricing/CEL report has no applicable container-count finding; a current metadata lookup found no newer successful run. It adds no mutation campaign, required status or stale-report hold.

Pure leaves emit no event, content or formatted native chain. Expected refusals remain fixed; existing callers own unexpected reporting and the future evaluator owns completion diagnostics. Existing TM-04/TM-10 input/callback/capacity, TM-06/TM-07 content/traceback and TM-08/TM-11/TM-12 identity/authority/evidence controls apply, without a new actor or trust assumption.

No wrapper, engine, schema-call frame, complete evaluator/composition, structural bridge, consumer, public export, derived-list aggregate or effective-extraction authority is added. Dependencies, package layers, human `docs/`, parameters, policy identity, limits, supported platforms, workloads, verification methods and deadlines remain unchanged.
