# Complete direct-output inventory construction

Date: 2026-11-04. Implementation owner: [#690](https://github.com/realkarych/kora/issues/690), under [#593](https://github.com/realkarych/kora/issues/593). The accepted [inventory format](graph-output-inventory-2026-11-04.md) and [data-model section 11.1](../specs/data-model.md#saved-output-binding-inventory) govern the saved value. ADR-0018 and ADR-0027 retain interpretation and activation authority.

## Delivered boundary

`engine.graph_output_inventory` constructs complete canonical array text for a graph whose entire declaration inventory uses supported direct StructuredOutput ancestry or exact AgentSession, CommandResult and VcsRef leaves. Its checking reader captures a supplied native array and compares its complete canonical text with expected derivation. It returns the captured saved text only on equality. The existing leaf digest remains unchanged; there is no new standalone inventory digest.

Every root and stored-body node output is visited, including unused bodies. Empty maps add nothing; an empty complete inventory still requires real IR, saved-type and output-schema checks. Paths are sorted by their admitted compact ASCII representation. Declared body exports, lists, wider native forms and unavailable composition refuse the operation rather than disappearing from a partial success. This helper does not establish whole-graph validity, first-slice topology support or execution readiness.

Input capture accepts only existing native JSON representations and canonical ASCII IR. Callers exclude mutation during capture, as required by the existing input boundary; capture is not an atomic concurrent snapshot. Parsed private copies and the returned immutable text do not retain caller-mutatable aliases. The real leaf constructors recheck the captured original sources, types, output roles and preparation. No current aliases, provider claims, supplied hash or trusted-object shortcut replaces those checks.

## Cumulative admission and repeated work

Each call requires a positive explicit `max_encoded_bytes`; no public default or persisted numerical policy is selected. Let I and T be canonical IR and type-array lengths, Q the sum of retained canonical path keys, O the complete generated inventory text, P each invoked leaf path length, and V its AgentSession policy/checker acquisition (7,029 bytes), zero for other leaves. Successful builder admission is exactly:

`I + T + Q + O + sum(I + T + P + V)`.

The reader additionally admits S, the complete supplied array, before expected derivation:

`I + T + Q + S + O + sum(I + T + P + V)`.

The helper reserves each sorting key before list growth; entry framing, path and digest width before leaf construction; and repeated I/T/P inputs before classification or invocation. AgentSession V is reserved before its constructor. The leaf receives only remaining capacity plus prepaid inputs. Its result is charged before appending the entry. There are no refunds or retries, and the first graph-value or preparation refusal stops further leaf calls. Both saved and expected arrays remain admitted during comparison.

These are cumulative canonical-operation charges, not unique storage size or Python heap/RSS/time guarantees. Final joining, canonical parsing and hashing can duplicate admitted representations by fixed factors. Existing array-parser wrappers and source-read sentinels remain fixed overhead. No cross-call cache, task, connection, process or new host-resource owner is introduced. Public acquisition limits, concurrent-call admission and frequency remain complete-consumer obligations.

For N native and H structured leaves, actual unchanged helpers perform `2 + 2N + 3H` document reads, `1 + N + 2H` saved-type checks, `1 + N + H` output-schema passes and H real preparations. Each refused operation performs only a prefix of these admitted calls. Repeated ancestry lookup occurs inside the prepaid per-leaf work. The allowance therefore accounts for repeated complete sources rather than resetting a full budget for every output; these counts do not claim linearity of every internal schema algorithm.

V bounds only the separately published AgentSession policy/checker source acquisition. The existing main built-in schema catalogue still uses its separately owned `read_bytes()`/decode path before integrity verification. As in the [native construction record](native-output-construction-2026-11-04.md), this slice does not bound corrupted/oversized main catalogue acquisition or certify complete raw-source admission. That obligation remains required before full consumer activation; no source-loader repair or additional probe is part of this change.

## Acceptance evidence

- `test_complete_inventory_preserves_mixed_root_and_unused_body_leaves` compares a mixed inventory with actual individual leaf constructors and verifies exact paths, ordering, default approval-form provenance and digests. Saved structured descendants and separate equal native declarations have dedicated cases.
- `test_empty_inventory_still_requires_real_document_and_types` observes real source-check counts even for empty inventory. `test_empty_saved_inventory_cannot_hide_source_failures` retains actual structural/type failure precedence.
- `test_inventory_reader_refuses_changed_association` covers missing, extra, duplicate, reordered and substituted entries, member shape/order, digest changes and rehashed source mismatches. Native capture tests reject foreign values, cycles, key collisions and noncanonical source spelling.
- `test_unsupported_late_output_refuses_complete_inventory` and `test_inventory_preserves_real_preparation_refusal` exercise real wider-output and preparation refusals after earlier supported outputs, with no later constructor call or partial result.
- `test_inventory_exact_cumulative_budget_and_source_passes` exercises exact/one-under builder and reader admission with unrelated retained IR/type content. Direct Python call observations verify the stated document/type/output/preparation counts, without replacing behavior or using timing/RSS thresholds. `test_inventory_admits_before_any_unpaid_leaf` covers saved-array capture and first/second-leaf pre-admission.
- `test_complete_output_inventory_round_trip` uses the existing paired canonical codec and parameterized SQLite/PostgreSQL fixture. It stores the complete section, releases the read unit before checking, rejects a foreign namespace, preserves scalar kinds/order/NUL/negative zero/large integers and checks the final ConnectionLedger after successful and refused readback. Its fixture envelope is not a complete executable graph contract.
- `test_inventory_diagnostics_exclude_retained_content` checks marker-safe DEBUG records, fixed refusals, clean stdout and sink shutdown. The helper is pure and reuses actual IR/preparation diagnostics; it does not activate an absent serving owner's observability obligations.

Focused local and final-head CI results, exact revisions and independent correctness/security/performance conclusions belong in the implementing PR. Linux/macOS, both databases, strict static checks, actual resource release and complete CI evidence remain mandatory. Applicable retained master mutation reports contained no output-inventory finding; absent or unrelated reports create no extra campaign or hold. The ten-minute whole-pipeline target remains diagnostic, with hard deadlines unchanged.

### Source-pass failure attribution

[CI 41748367461](https://github.com/realkarych/kora/actions/runs/41748367461/attempts/1) failed on Linux AMD64/Python 3.15.0 at head `4f2ebe5a20c262d4ae7e69273e8b701fc5b3b1fd`. The agent case reported eight events named `read_document` against the required seven. The source has seven direct invocations across the four known caller functions for that case. The retained result lacks code-object and caller attribution.

The same revision passed the 37-case inventory module locally on macOS and Linux AMD64/Python 3.15.0. One bounded Linux AMD64 run of the native-output module followed by the inventory module passed 91 cases. Both Linux containers exited and were removed. These runs leave the CI discrepancy unresolved.

The [approved diagnostic run 41791338141](https://github.com/realkarych/kora/actions/runs/41791338141/job/126445899602) repeated the failure on head `7ec65682ce4405d5d2793eba2157fcf2097a598d`. The profiler reported eight entries for the reader code, one marked recursive, with seven incoming edges distributed 1/1/1/4 across the expected callers. It also reported two entries into the outer derivation despite its single invocation in the measured workload. The exact interpreter event sequence remains unverified. Both historical failures retain their verdicts.

Under the [owner-approved replacement](https://github.com/realkarych/kora/issues/690#issuecomment-6625957895), the exact cumulative case counts each Python call event matching one of its four code objects by identity. The current-thread profile slot must be empty before acquisition. The observer covers the one measured inventory build, holds only fixed counters and clears its slot on success or refusal. Repeated and recursive entries count independently. The regression cases distinguish same-name code and check slot ownership and release. The four exact requirements and other profiler checks remain in force. A qualifying complete final-head run verifies the direct observation and retains the original admission, database and lifecycle checks.

## Remaining activation

Full execution-contract assembly still needs other required sections and identities. Wider output composition, complete source acquisition, operational publication, caller concurrency/retention admission, native payload checking/projection, first-slice validation and actual graph starts remain required under #593 and their existing owners. Existing historical records receive no backfill or current-default repair. The helper adds no API/CLI/MCP operation, dependency, migration, layer allowance or CI change.
