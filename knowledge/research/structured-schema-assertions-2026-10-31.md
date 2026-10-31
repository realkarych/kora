# StructuredOutput primitive assertions: scope and evidence

Date: 2026-10-31. Implementing child: [#536](https://github.com/realkarych/kora/issues/536), coordinated by [#138](https://github.com/realkarych/kora/issues/138). Baseline: `4a38ff8242dad43acd07602f6274172a39644a7d`. The [implementation and verification plan](https://github.com/realkarych/kora/issues/536#issuecomment-6536258646) applies the complete policy accepted under #515. [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) and [StructuredOutput section 3.3](../specs/artifacts/StructuredOutput.md#33-exact-bounded-evaluation) remain authoritative. This record adds no policy or activation authority.

## Internal interfaces and integration ownership

The existing-layer private module [`structured_schema_assertions.py`](../../src/kora/ir/structured_schema_assertions.py) exposes no name through `kora.ir`. Each of its eight leaf functions receives `(instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter)` and returns a completed boolean or propagates a fixed refusal or unexpected programming error.

| Function | Leaf decision |
|---|---|
| `type_matches` | Exact kind or saved-order alternative match; an applicable integer alternative invokes the unchanged numerical integer classifier |
| `minimum_matches`, `maximum_matches` | One exact numerical comparison for an applicable instance, followed by the fixed inclusive relation |
| `exclusive_minimum_matches`, `exclusive_maximum_matches` | The corresponding strict relation after the same comparison |
| `multiple_of_matches` | The unchanged exact conversion, divisor-domain, alignment and remainder procedure |
| `const_matches` | One iterative exact JSON comparison against the original literal |
| `enum_matches` | Iterative comparison against saved alternatives until the first completed true |

A bound comparison returns an internal bounded relation tag or `None` for inapplicability; its wrappers never confuse that sentinel with completed false. There is no authored operation selector, generic keyword registry or fallback handler. Numerical helpers and equality retain their implementation, admission and resource ownership. No arithmetic conversion or equality result is cached.

The caller owns exactly one checked private native tree, obtained through the existing model boundary, and excludes concurrent mutation. Original arena nodes remain borrowed. Whole-domain Unicode, finite-number, bit/depth/cycle and duplicate-name admission belongs to the model/raw/arena owners. Structural schema validity remains a caller prerequisite. Exact defensive checks reject malformed visited kinds/forms without scanning or certifying unvisited data. A direct private cursor is not an admission certificate.

`type` alternatives preserve original saved order, independently from the structural checker's sorted safe projection. Authored names are recognized through charged string comparisons in the fixed literal order `array`, `boolean`, `integer`, `null`, `number`, `object`, `string`. Boolean differs from numeric. An exact native int still invokes the classifier when the selected alternative is integer. Empty type arrays, unknown visited names and visited nonstring alternatives refuse. Empty enum completes false; duplicate enum candidates execute normally. No new parser or array-wide uniqueness scan is introduced.

Numerical bounds inspect instance applicability first. A nonnumber passes without numerical conversion or examining the bound. Applicable instances use one unchanged `numeric_compare`; `multipleOf` uses one unchanged `numeric_multiple_of`, including divisor-zero/sign refusal and its charged zero-dividend path. `const` and `enum` use the existing iterative equality, including its counts/all-keys/values ordering and separately owned comparison state. Refusal never becomes a nonmatch.

Future owned handlers obtain the native cursor, context meter and original value-node ID from their accepted frame/BoundKeyword, construct the charged root arena handle and call a named leaf function. That future wrapper owns fresh mismatch construction after completed false. Canonical keyword/constraint order, installed-handler dispatch, schema frames, engine iterator closure and complete composition remain with their implementing owners. These helpers introduce none of those components and supply no complete schema-validity, artifact-acceptance or extraction result.

## Exact surrounding-action accounting

The complete immutable schedule remains in section 3.3 and the issue plan. The module adds no bulk exemption. Each introduced kind/read/guard/branch, cursor initialization/advance and loop condition is prepaid at one logical unit. Existing accessors, numerical functions and equality retain their full charges and auxiliary reservations.

Let `KN` and `KA` denote the existing kind-accessor costs for the particular native/arena kind; `NC`, `NM`, `NI` and `J` are the complete existing comparison, multipleOf, integer-classifier and iterative-equality costs. Received handles have already been charged. Native/arena scalar access costs 3/6. Arena-array count costs 9; each visited arena child costs 8 accessor-prefix work plus 3 handle-factory work and one entry. A native string comparison costs `E(a,b)=1+min(len(a),len(b))`.

| Leaf path | Logical work |
|---|---|
| Numerical bound inapplicable / applicable | `KN+2` / `KN+KA+13+NC` |
| multipleOf inapplicable / applicable | `KN+1` / `KN+KA+11+NM` |
| Visited nonnumeric bound/divisor | `KN+KA+2`, then refusal |
| const | `J`, with no extra leaf branch or record |
| Scalar-form type | `KN+KA+7+D+M` |
| Nonempty array-form type setup | `KN+KA+13` |
| Nonmatching / matching type alternative | `21+KA(child)+D+M` / `20+KA(child)+D+M` |
| Empty type array | `KN+KA+12`, then refusal before position initialization |
| Enum setup | `KA+11` |
| Nonmatching / matching enum candidate | `14+J` / `13+J` |
| Empty enum | 23, completed false and no candidate handle |

`D` sums `E+1` for each visited literal-ladder comparison and recognition branch. Unknown names pay all seven before refusal. `M` is 2 for noninteger kinds or inapplicable integer alternatives; an integer alternative on a number costs `5+NI`. Exhausting a type or enum loop adds one final failed condition. Each visited alternative/candidate creates one charged arena handle; equality separately charges its existing frames and indexes. New caller-created root handles remain outside these received-handle totals.

Leaf calls own no row, vector, byte buffer, schema frame, engine or global retainer. Bounded local tags/counts/cursors are administrative state. Numerical/equality helpers own their existing temporary records, buffers, pair frames and lazy indexes. Every recreated state is charged; release never refunds cumulative work, bytes or entries. A caller-held exception traceback may retain borrowed handles until disposal, which release evidence must distinguish from helper-owned retention.

## Failure, diagnostics and limits

Completed true/false is only a leaf answer. Exhaustion and unsupported visited forms propagate existing content-free `EvaluationRefused`, never false or SO006. Unexpected errors propagate to the existing caller-owned safe reporting boundary. The module emits no operational event, authored name/path/value, native chain or ValidationError. It introduces no executable entry point, task, process, descriptor, asynchronous interruption or time/heap/RSS guarantee. The eventual complete evaluator retains its one completion-diagnostic owner.

TM-04/TM-10 govern admitted input, visited kinds and resource admission; TM-06/TM-07 cover content-free failure/reporting and traceback disposal; TM-08/TM-11/TM-12 retain immutable interpretation, authority and activation separation. No actor, trust assumption or threat-model statement is changed.

`minLength`/`maxLength` and all collection/child-call assertions remain outside #536. Their count-domain/check/conversion-order question is not settled here: section 3.3 requires unsupported_schema for a violated nonnegative-integer prerequisite and one count/bound conversion, while additional classifier/sign calls would add an unspecified order/conversion. No check, new shortcut or policy-identity change is silently selected.

## Verification requirements and current status

The implementing PR records exact compared revisions, complete production count, actual independent profile results, commands/environments and native artifacts. Separate [design results](https://github.com/realkarych/kora/issues/536#issuecomment-6536311703) passed correctness/security/performance for the complete plan and exact ledger. Design acceptance supplies no implemented behavior or runtime qualification.

Required changed-boundary evidence covers all seven type names, bool/number separation, exact integer classification, saved type/enum order, empty/duplicate enum, exact numerical relation/multipleOf vectors and recursive literal meaning. Hand-derived exact/one-short work/byte/entry cases protect paid prefixes, repeated shared-meter charging and no refunds. Inapplicable dense admitted values must remain lazy. Real model/arena admission and deliberately invalid visited-internal cases keep their different claims. Success, mismatch, real refusal and bounded unexpected-error release require disposal of caller-held traceback before final no-retainer assertions. No mocks, test-only production hooks or heap/RSS threshold is used.

Source-only lint/format and strict Pyright passed on the initial source snapshot, SHA-256 `2402275b623f364111e2923959ee65fc694aa77d8b0b9dfdbc798b8e513cb027`. Focused test, actual complete-diff independent review, remaining strict/architecture/frozen-publication and one complete final-head CI evidence are pending at this record's initial writing and must be recorded in the PR before readiness. Native CI retains Linux/macOS and ordinary both-database coverage; pure helpers need no new database fixture. Runtime durations are diagnostic, not support guarantees.

The latest available daily master mutation audit is run `41025696626` at source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`. The previously inspected summary/report has partial pricing/CEL scope, no applicable assertion finding and no equality mutation confirmation; a fresh workflow query found no newer report. It adds no PR mutation campaign, required status or stale-report hold. Complete handler/adapter/evaluator/composition and real consumer activation remain pending under #138 and their existing owners.

## Local implementation evidence

The final semantic-test snapshot, SHA-256 `2ff5511dd1a9b801ccd2c141bbf4390116f705aba667e74935ba1ce4cb856b09`, passed 370 focused cases in 3.79 s with zero failures, errors or skips. Production remained at SHA-256 `2402275b623f364111e2923959ee65fc694aa77d8b0b9dfdbc798b8e513cb027`. The later test snapshot `d5ca0e084593f1fb8431e303e353a4ae4956c30aa534eb77b96912b42a4de5a5` adds only two explicit parameter-ID lists; an independently inspected diff preserves every case value, assertion and order. A collection-only check retained 370 cases, with maximum node-ID length 206 instead of formatting entire large integers. No unchanged semantic campaign was repeated solely for these labels. Full final-head native CI must execute the final named identities.

Source and semantic-test strict Pyright checks passed with zero errors/warnings. Touched Ruff lint/format checks passed, including the ID-only delta. Existing import-layer/no-suppression and source packaging checks produced 30 passes. Two real-wheel packaging checks failed during fixture setup because the local pnpm 9.15.4 rejected the unchanged workspace manifest, while the project/CI pins pnpm 12.14.0. These are retained local setup errors, not passing wheel evidence. Required final-head CI with locked dependencies must supply successful wheel/installed checks before readiness; no configuration, dependency, timeout or assertion was changed to fit the local environment.

The local Python environment was macOS with Python 3.11.17 and Ruff 0.17.1. Runtime checks used an explicit isolated source path and the existing interpreter; Pyright used the existing environment through its venv-path option. Changed specification Markdown passed markdownlint-cli 0.45.0 under local Node 26.13.0; research remains ignored by the unchanged Markdown configuration. Native CI supplies its declared locked runtime/platform evidence. Exact native commands, raw JUnit, final revision and actual source-review results are recorded in the implementing PR. All durations remain diagnostics.
