# StructuredOutput string-length assertions: scope and evidence

Date: 2026-10-31. Implementing child: [#558](https://github.com/realkarych/kora/issues/558), parent [#138](https://github.com/realkarych/kora/issues/138). Baseline: `21df1269f2e107c7f89e7392f11477a436a3d970`. The [concrete implementation and verification plan](https://github.com/realkarych/kora/issues/558#issuecomment-6538922713) implements the accepted [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput sections 2, 3.1 and 3.3](../specs/artifacts/StructuredOutput.md). No new count domain or numerical policy is selected.

## Private interfaces and existing admission owners

The existing-layer `kora.ir.structured_schema_assertions` module appends private `min_length_matches(instance, argument, meter)` and `max_length_matches(instance, argument, meter)` functions with one shared comparison helper. Both borrow the existing native payload cursor, original arena cursor and cumulative evaluation meter. They return a completed private boolean or propagate existing refusal/programming error. No public IR export, engine, wrapper, schema-call frame, evaluator, complete-validity value or extraction authority is added.

The accepted count domain is mathematical nonnegative JSON integers: exact native integers and admitted finite integral floats, including `1.0`, `1e23` and `-0.0`. Preserve the saved scalar's kind, canonical spelling and signed-zero identity. Boolean, negative and fractional counts are structurally invalid. Exact-native integers required by the policy's parameter map do not narrow authored count values.

The real structural keyword validator owns syntax validation of the identical saved schema before a caller may rely on that prerequisite. It checks a diagnostic-safe view through the existing draft-2020-12 metaschema without rewriting the original schema. Preparation owns exact native snapshot/support/closed-reference admission, not complete syntax certification. Neither its opaque result nor an `ArenaCursor` is such a certificate. The future structural-owner-to-identical-saved-arena bridge remains pending under #138, work items 3 and 8; this leaf slice does not implement or activate it. Whole-operation `unsupported_schema` for violated count syntax remains that integration's obligation.

Consequently, the leaf does not add `numeric_is_integer`, a sign classifier, count rounding, `int(bound)` or another conversion. Invalid private numeric fixtures are outside its structural prerequisite. An inapplicable leaf's ignored invalid argument demonstrates noninspection only, never whole-schema acceptance.

## Exact local algorithm

Read native instance kind, then pay the applicability branch. Nonstrings return the private inapplicable marker and pay the leaf's marker branch before completing true. They never inspect the argument or create per-item state.

Applicable strings read the original arena argument kind and pay its branch. A visited nonnumber refuses before scalar borrowing, length or conversion. Borrow the native string once, then the original numerical scalar once. Prepay one native `len` call on the exact admitted string; this counts Unicode code points, with no UTF-8 byte count, normalization, materialization or authored callback. Invoke existing `numeric_compare(count, bound, meter)` exactly once, count left and bound right. Pay the leaf marker and inclusive relation branches, using `>= 0` for minimum and `<= 0` for maximum.

Numerical comparison retains its own exact canonical-decimal conversions and mandatory zero/sign/alignment schedule. There is no mixed native numerical comparison, enormous loop bound, sufficiency shortcut, cache or new numerical operation. Refusal never becomes a completed mismatch.

## Ledger and resource ownership

Let `KN` be the existing native kind cost and `N(count,bound)` the complete existing numerical comparison, including both conversions and every work/byte/entry reservation. Received cursor construction remains charged to its caller. All new guards, reads and branches are prepaid; nothing refunds the shared counters.

| Action/path | Work |
|---|---:|
| Native string kind | 9 |
| Applicability branch | 1 |
| Arena integer / float kind | 14 / 15 |
| Argument-kind branch | 1 |
| Native string borrow, then arena scalar borrow | 3 + 6 |
| One native Unicode-code-point length read | 1 |
| Prefix before comparison | 35 / 36 |
| Complete applicable leaf | `37 + N` / `38 + N` |
| Inapplicable leaf | `KN + 2`, argument untouched |

For the admitted string `x` and bound `1`, existing comparison costs 38 work, no bytes and two entries; the leaf costs 75 work. Bound `1.0` retains the existing 4,186-work comparison, 51 cumulative auxiliary bytes and four entries, giving 4,224 leaf work. These are hand-derived logical reservations, not timing or heap measurements. Cursor creation adds five work and two entries in the corresponding complete-call quota fixtures. Tests freeze independent exact/one-short work/byte/entry paths and the already-paid prefix; failed reservations happen before the associated primitive.

The helper introduces no new cursor, index, vector, schema frame, byte buffer or fixed auxiliary row. It retains only borrowed local references and bounded native count/result scalars. Existing numerical code owns temporary conversion records/scratch. Ordinary returns unwind locals; caller-held exceptions can retain them until explicit traceback disposal. There is no global state, retained cache, process, task, thread, descriptor or asynchronous interruption. Caller cancellation waits for the existing finite synchronous boundary.

## Verification, diagnostics and limits

The focused module exercises the real structural owner for both count keywords, Unicode/NUL/supplementary/combining strings and inclusive edges, original arena/scalar identity, large integer/integral-float bounds, applicability/visited-kind order, independently derived quota fences, cumulative repeated calls, silent hostile scalar/metaclass callbacks and true/false/refusal/unexpected-error release with explicit traceback disposal. The latter uses the existing real GC/exact-owner inventory; no GC replacement, mock, new numerical method or general framework is introduced. Existing eight-leaf source and its complete test file are preserved.

The implementing PR binds actual commands, tested source identities, raw JUnit and independent results. Relevant existing numerical ordering/callback and actual native/arena admission regressions complement the complete old/new assertion files. Strict source checks and focused architecture checks remain blocking. One successful complete final-head Linux/macOS CI with existing applicable database, frozen-publication and wheel coverage is required; old campaigns and design review establish none of those new results. Runtime durations remain diagnostics, not latency, RSS or leak-freedom guarantees.

Pure leaves emit no operational event or authored content. Fixed expected refusals remain content-free; existing callers own unexpected-failure reporting without native chains or content-bearing locals. The eventual evaluator owns completion diagnostics. TM-04/TM-10 input/callback/capacity, TM-06/TM-07 content/error/traceback and TM-08/TM-11/TM-12 immutable authority/evidence controls remain unchanged. No actor, trust assumption or threat-model amendment is added.

The latest available successful master audit remains run `41025696626` at source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`, dated 2026-10-30. Its previously inspected partial pricing/CEL report has no applicable string-length finding. A fresh metadata lookup found no newer successful master run. It adds no PR mutation campaign, required status or stale-report hold.

Other count keywords, child calls, schema frames, wrappers, engine/composition, real consumers and complete effective extraction retain their existing implementing owners and activation gates. No dependency, package-layer, public ABI, human `docs/`, policy identity, parameter, limit, persisted representation, platform, workload, verification method, timeout or acceptance gate changes.
