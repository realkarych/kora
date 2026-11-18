# Saved output policy and preparation ownership

Implementation record for #1595, based on `1d41a3837c7a9e50c044f81f272b7839336864fe`. The [standard-schema contract](../specs/schema-validation.md) and [ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) govern. The owner approved the separate trusted-failure cleanup envelope in [#1595](https://github.com/realkarych/kora/issues/1595#issuecomment-6896362580), recorded with exact exception-group accounting in the [approval reconciliation](https://github.com/realkarych/kora/issues/1595#issuecomment-6896398331). This change supplies graph publication/checking, not output acceptance or scheduler settlement. Parameter-profile integration remains separate.

## Saved identity and installation ownership

`OutputPublicationBinding` captures the exact v2 policy from admitted constants and borrows an installation-owned stable behavior mapping. API/engine do not discover plugins, enumerate the installation or retain the map in a saved graph. For each structured leaf the writer authenticates the original IR/type closure and calls complete effective composition. Required identities are looked up without invoking validation/extraction hooks. Native output contracts remain unchanged.

The checking chain selects the admitted saved policy for each structured leaf. V1 uses its historical source preparer; v2 uses complete standard source support. Neither reader needs today's installed map or gains value authority. Complete path/member/order/digest comparisons reject partial inventories, rebinding and source substitution. CEL/projection and whole execution checking retain the existing eight-member envelope. Exact v1 bytes and matching receipts remain unchanged, without new validation effects or parameter defaults.

`tests/fixtures/schema_policy_v1_history.json` was captured once from the unmodified base writer before #1595 source edits, using CPython 3.11.17 on macOS arm64. It contains four agent/command/human-input/event leaf contracts and one complete execution contract. Its 90,873 bytes have SHA-256 `a7bc6d198e0e24a2375c1a9f9570e52552e9eba5a1019a1db6c542b5dddd456b`. The original whole execution digest is `4a82670802109990959313ee8149e69f5eca4c2e5814fa6132dac91ed6a51801`. No production legacy writer or migration generates these fixtures.

## Sequential source reservation

Use the accepted source maxima B/S/P/R/D/T/I/Sd/SA for bytes, nodes, positions, references, documents, string bytes, integer bits, depth and array items. The existing source derivation gives:

`L = 4*S + 12*P + 8*R + 4*D + 256*(Sd+1) + 2*SA = 139776` entries.

Add `4*R + 24*D` for role dependency sets, pending targets, ancestry/name vectors, immutable behavior identities and callable capture; add `4*S + 64` for encoding fragments/traversal. Total entries are 223,296. Variable bytes are `13*B + 32*T + 128*D + 2*I = 15764832`: the source record's original `10*B` plus three canonical fragment/text/byte owners. Combined source scratch is 15,988,128 logical units, not CPython object bytes.

The complete source phase has two bounded shape scans and two sequential Sources constructions: role closure first, then final support. Final support tables remain live through encoding. Immutable prepared source bytes then transfer into the short-lived composition or reader result. Publication captures only required callables and discards its composition before returning saved text. No value view, validator, registry or error tree is allocated on this path.

Reserve the larger v1/v2 source peak and `output_context_bytes // 64` possible leaf preparations. Per-leaf work is `4 * (max_validation_score + B + S + entries) = 273588480`, covering all scans, role/support construction and encoding. The outer graph reader still charges its original decoded/canonical source copies, paths, repeated leaf inputs and complete expected output.

API preparation performs three sequential inventory passes. Remove this shared source scratch from each of the two copied reader reservations and add one peak; retain all three work charges and every other native/text/result owner. Default read admission is 783,551,424 units; default preparation needs 2,119,616,025, within its unchanged 2,147,483,648 capacity. Existing exact small-workload boundary tests now use reader 57,189,696 units / 44,294,855,301,597 work and preparation 156,461,529 / 358,548,790,373,459. These are implementation-derived admission checks, not elapsed-time/RSS gates.

`test_saved_output_preparation_reservation_covers_both_policies` checks the exact default boundary and one-unit refusal. `test_schema_source_and_composition_holders_do_not_span_inventory_passes` observes actual Sources owners weakly and records only holder identities across the three passes. It inspects tracked object ownership without forcing collection, and restores the previous profiler. This proves disposal at those boundaries, not process-heap reclamation. Existing cumulative byte/source-pass checks remain active.

## Cross-thread completion ownership

The caller's notification Future carries only None. The worker invokes the supplied callable inside its Task; caller and launch frames release the callable after submission/construction. A completed Task is inspected even when cancelled. Ordinary errors are reduced to fixed dispositions before their traceback/cause/context graph and standard exception-group children are cleared.

Cleanup has separate O(E + G + F) work and O(E) scratch as the specification defines. Identity is marked before enqueue; group tuples are iterated without copying. Completed frames are cleared, and only the current completion callback may remain active. If another frame cannot be cleared, the safe fault is returned but settlement is not acknowledged: the reservation remains unavailable and shutdown cannot claim success. This is not a general traversal of exception arguments, custom objects or external references.

The callback drops original Task/error references and schedules delivery on the next turn of the same worker loop. Supported CPython 3.11/3.15 event loops snapshot the current ready batch and discard its last Handle before the next batch; the original callback cannot own sources when delivery is forwarded. The completion itself contains only the intended successful result or safe disposition. A live caller takes the result; cancellation marks delivery abandoned and clears its result before acknowledging settlement. Success, error and cancellation use the same slot owner. Supported EvaluatorUnavailable reasons recreate the existing API011 behavior; unexpected failures reach the existing single API010 error owner with fixed text. No original exception is placed in a cross-thread Future.

Startup/shutdown interruption and KeyboardInterrupt/SystemExit worker-loop termination are outside this ordinary-failure repair. No timeout, concurrency, forced-GC, dependency or suppression exception is added.

## Evidence and limits

Focused checks cover exact publication and missing installed identities, no hook calls/map enumeration, reference-only roots, v1/v2 refusals, both-database historical leaf/whole reads after metadata release, accepted/skipped replay before graph reads, source capacity, cancellation before success/failure, chained/cyclic/grouped exceptions and synchronous invocation failure. `test_deploy_unexpected_preparation_failure_keeps_safe_api010` exercises the real deployment route and a failing trusted installation mapping, checking one content-free ERROR, no publication and subsequent slot availability. Existing unavailable-evaluator coverage preserves API011.

The PR records final revisions, actual commands, independent correctness/security/performance results and CI. Earlier component results retain their original revisions and limits. The latest applicable master mutation audit inspected during planning had no schema finding; it adds no PR mutation campaign. Schema validity and saved consistency supply neither physical custody nor output acceptance, Worker report authority or scheduler settlement.
