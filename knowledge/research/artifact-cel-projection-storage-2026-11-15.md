# Lossless artifact CEL projection storage

Date: 2026-11-15. Issue: [#1633](https://github.com/realkarych/kora/issues/1633). This record explains the accepted representation and remaining evidence; the normative contract is [data-model projection storage](../specs/data-model.md#lossless-artifact-cel-projection-storage), with [IR cutting/conversion](../specs/ir.md#75-projections-and-typed-conversion) and [accepted derived owners](../specs/execution-content.md#23-accepted-owners).

## Delivered decision

The owner approved one native byte record containing both the original admitted cut and its converted CEL view, the explicit derived-owner version boundary, canonical path-set digest and revised encoded-size accounting. The design changes no production source, dependency, API, table or migration. Accepted outputs, native bounded reads, Worker reporting and scheduler settlement are not activated.

The original cut is necessary for later conversion under a different saved view. An integer-view child can turn `-0.0` into integer `0`; its dyn-view destination still needs negative double zero. A number-view child can round `9007199254740993` to `9007199254740992.0`; its integer-view destination still needs the original exact integer. Retaining only the converted child cannot meet either contract. Retaining the whole primary payload would enlarge ownership and introduce forbidden fallback reads.

The existing planned `artifact_derived_owner(kind=cel)` already owns independent semantics, content identity and availability. It therefore holds the one BLOB/bytea value and the external representation, length, digest and binding facts. A marker in the old projection column does not confer that authority: arbitrary legacy JSON strings may equal the marker or resemble an envelope. Only the matching atomic acceptance/derived owner selects the new format. Legacy history and expiry retain their existing meaning, without backfill.

## Encoded size and exactness

The six fixed keys, punctuation, version string and three 64-character hashes take 323 ASCII bytes excluding the two JSON values. Each canonical original/converted value independently retains the existing maximum of 4,096 UTF-8 bytes and 256 occurrences. The complete maximum is `323 + 4096 + 4096 = 8515` bytes. No outer JSON-string escaping or duplicate value column adds another stored copy.

The producer uses the existing compact canonical value spelling: ordered inner object keys, array order, integer/float kind, finite float sign and unnormalized UTF-8 with JSON control escaping. The full-record digest covers the exact bytes. Path-set identity hashes the exact compact ASCII object `{"whole":[...],"presence":[...]}` with normalized saved arrays. It is not a new digest of the enclosing graph section.

The 8,515-byte bound concerns encoded content only. It does not bound Python object overhead, driver copies, SQLite preceding fields, PostgreSQL compression/TOAST, physical read time or caller aggregate capacity. A malformed record may have more decoded occurrences than a valid original-plus-converted pair. The implementation must pre-admit parser state and conversion intermediates rather than construct an unbounded value and reject it afterward. The existing cutter and converter are reusable primitives, not evidence of complete consumer admission.

For 1,000 children, the record maximum gives 8,515,000 cumulative encoded bytes. The approved collector design acquires/checks/releases one child at a time, retains only the bounded destination and fixed references/current-child state, stops at the first original/converted destination bound and charges all visited work. This does not authorize aggregate materialization, enlarge a caller's allowance or implement a graph route.

## Binding and ownership

The same preparation selects a complete checked graph/output/path/view, constructs the installed effective composition, validates the original model and cuts that successful model. A standalone StructuredOutput or unrelated `EffectiveValid` carries insufficient association. The prepared result retains its exact caller/output identity separately from semantic bytes; complete output acceptance still requires current authorization and authenticated source provenance.

StructuredOutput's complete original canonical model supplies the payload digest. Other artifact kinds need their own source mapping before using the format. In particular, a transcript digest cannot identify AgentSession metadata. First delivery is restricted to a complete detached StructuredOutput producer/checker. It must preserve larger valid originals with small projections and cannot import the stricter run-parameter domain.

The caller owns admitted full inputs and all simultaneous cut, conversion, encoding, parser and driver copies until actual disposal. Cancellation and trusted exceptions cannot return capacity while their frames or results retain content. Database/native acquisition remains a separate owner with admission before transfer. The accepting transaction publishes artifact, acceptance and derived owner atomically and retains uncertain-commit recovery. Accepted bytes do not establish physical execution closure or release attempt capacity.

These boundaries retain the existing controls in [the threat model](../../docs/threat-model.md): authored-data/resource admission (TM-04/TM-10), current authorization and source integrity (TM-01/TM-09/TM-11), content retention and diagnostic exclusion (TM-06/TM-07), and implementation/evidence limits (TM-12). No new threat actor, access grant or weakened control is selected.

## Implementation and verification ownership

The design delivery contains zero production changes. A separate concrete plan inventories a complete detached producer/checking reader, initially estimated at 700–950 production added/deleted lines, within the 1,000-line complete-diff ceiling. Reconcile this estimate before editing; it is not a size exception. Reuse the existing checked graph, frozen-path, cutter, converter and bounds primitives without widening package imports or replacing the generic document codec.

| Owner | Required implementation evidence |
|---|---|
| Detached StructuredOutput producer/checker | Same-call complete effective validation and exact output association; no record for standalone-valid/effective-invalid or unsupported behavior; swapping equally shaped outputs still refuses. |
| Canonical representation | `test_cel_projection_number_kinds_round_trip` on real SQLite/PostgreSQL: integer/float distinction, positive/negative zero, exact `2**53 + 1`, admitted extreme values/subnormals, NUL/non-BMP keys/values and scalar/list/object/null roots. Literal canonical path-set/hash vectors cover Unicode, empty/root, whole/presence sets. |
| Real CEL binding | `test_cel_projection_matches_full_binding`: complete original versus selected value, including larger-original/small-cut admission. `test_cel_projection_composes_through_collect_and_exports`: both lossy examples, saved destination views and whole/presence paths at the library boundary; unsupported graph routes remain refused. |
| Checking/history boundary | Wrong original payload/contract/path/output, duplicate/missing/extra/reordered fields, noncanonical/deep/oversized/nonfinite/unknown-version input, native subclasses and legacy lookalikes refuse without repair. Primary content present with an expired CEL owner remains unavailable. |
| Resource and diagnostics boundary | Independent original/converted/encoded limits, pre-admitted lexical/parser/conversion scratch, actual success/refusal/exception/cancellation disposal at the real caller, content-free diagnostics including DEBUG and blocked/broken sinks. No extra timing or RSS gate. |
| Native metadata acquisition | Whole selected row/indexed acquisition and simultaneous-copy admission on SQLite/PostgreSQL, including preceding wide fields and TOAST. Codec or database round-trip tests do not qualify this. |
| Actual output acceptance | Atomic artifact/acceptance/derived-owner insertion, unique whole-result replay, authenticated producer evidence and current authority, retention/availability and real Worker handoff under #1472's dependencies. No fixture-manufactured acceptance authority. |
| Later collector/settlement | Exact immutable child snapshot and current-owner recheck, sequential bounded reads, destination admission and actual release. #1472 owns terminal state and physical capacity; #1446 consumes only real accepted inputs. |

Independent correctness, security and performance planning assessed the concrete representation and its source, authority and resource boundaries. Actual documentation review and final-head CI remain separate evidence for this delivery; planning reports certify no producer implementation. The future implementation retains source review and real supported-platform/database checks. No additional verification framework or campaign is introduced.

The latest applicable audit inspected during planning was run 43705847856 at source/tooling revision `2dff3824df3b93d2a0937300219a3978c770cec5`, attempt 1. Its partial pricing/CEL report contained no projection or converter findings and supplies no qualification for this design. Future findings follow the normal owning-boundary workflow.
