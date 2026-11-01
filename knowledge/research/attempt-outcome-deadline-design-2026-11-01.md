# Attempt outcome and deadline persistence design

Date: 2026-11-01. Owner: [issue #545](https://github.com/realkarych/kora/issues/545). Inspected source: `f089cbba5f75353c9c0bc8eaac47f4434968c066`. The [direct owner instruction](https://github.com/realkarych/kora/issues/545#issuecomment-6550566787) approves the published formats and bounded design/outcome/deadline PR sequence. This record explains the selected representation and evidence limits; [data-model section 16](../specs/data-model.md#16-original-outcome-and-full-deadline-persistence-545) is the normative format contract.

## Existing implementation and delivered decision

Cancellation-cause storage is delivered by #572/#574. Original outcome and complete metadata deadline storage are not delivered by this design PR. The existing `TerminalReport` admits a finite 16-KiB `AttemptError` whose details can contain U+0000, while `db.JsonDocument` rejects that character on both dialects. A nullable timestamp cannot preserve the complete existing `ExecutionDeadline` or distinguish historical missing provenance from an explicitly unlimited binding.

The selected outcome receipt preserves original core identity and a complete original report/grant/evidence comparison fingerprint, independently of completion-applied status/error. Bounded canonical UTF-8 JSON TEXT preserves every admitted error without changing the shared JSON contract. The error body is one expiring attempt-owned value; its current `transcripts` permission and shorter saved transcript/artifact retention prevent a permanent content copy. Its exact byte identity and report comparison fingerprint survive expiry. A fingerprint cannot recreate original evidence or authenticate an incoming report.

The selected deadline envelope preserves unrecorded, explicitly unlimited and complete values. Canonical decimal microseconds preserve the entire existing nonnegative timedelta domain without float conversion. The existing timestamp remains a compatible scalar projection; old scalar/start/latch values remain unchanged. Existing clocks, scope tie precedence, current ancestor eligibility, sticky expiry and fixed grace are not redefined.

One correctness finding narrowed an ambiguous read promise to the already specified four-field original outcome core. Neither the receipt nor its hash can reconstruct the exact original ClosureEvidence after aggregate reconciliation changes it. The durable text explicitly makes the full-report fingerprint comparison-only; it adds no evidence archive or full-envelope read operation.

## Independent design evidence

The [actual resumed correctness/security results](https://github.com/realkarych/kora/issues/545#issuecomment-6550605632) qualify the clarified proposal `77471d312bc458d81c878a10a5e55ef851a85f6a41d7fffbcfe0dd9253b7ac92`. Correctness returned no remaining findings; security inspected the exact read-scope delta and retained matching original conclusions. The [actual performance design result](https://github.com/realkarych/kora/issues/545#issuecomment-6550420954) qualifies the unchanged bounded representation, preparation and migration-resource inputs of proposal `354f586056cb9ea548894ea27dd29096096443f54b8cb94d3bc4c6cf7480cf35`. The sole later change clarifies selected-field meaning without changing storage, serialization, ownership, admission or methods, so that scoped result is reused with its original identity and limits.

All profiles used independent read-only source contexts. No candidate execution, installation, network, edits or publication by reviewers supplied evidence. Earlier correctness/security HTTP403 dispatch failures remain failed attempts with no review result; direct owner continuation later supplied the missing actual results. They were not replaced by performance, self-review or a generic readiness statement.

## One verification plan and activation ownership

| Boundary and changed risk | Integration owner | Blocking evidence | Diagnostics and remaining limits |
|---|---|---|---|
| Original report/grant/evidence to immutable receipt and finite error body | #575 engine foundation | Canonical numeric/Unicode/NUL/depth/size/domain checks, detached ownership, complete changed-input comparison and honest expired-content identity | Codec success is neither authenticated acceptance nor closure |
| Nullable storage and compatible migration checkpoints | #575/#576 metadata foundations | Real predecessor history/old writers/no defaults/noop/schema equivalence; incompatible refusal; cancellation/shutdown/recovery and exact connection/writer/lease release on both databases | SQLite history validation occupies its writer; acquisition deadlines do not bound total validation duration |
| Full envelope to existing ExecutionDeadline | #576 engine foundation | Three distinct states, exact UTC/timedelta.max, fixed grace and known scalar consistency; unchanged threshold/registry contracts | No first-claim transaction, discovery, timer or stopping guarantee |
| Report/claim/retention/read/runtime activation | Existing shared engine, API/worker, scheduler and #216 owners | Their real authenticated locked transitions, current access, holds, lifetime, client parity and runtime/recovery checks when implemented | Explicitly pending; no foundation proves a reachable unchanged consumer |

The complete handwritten production diff of each implementation stays at most 1,000 added plus deleted lines. #575 is estimated at 450–750 and #576 at 250–500, excluding tests, knowledge and generated files; exceeding the hard cap requires a split. Each actual foundation keeps focused regressions, strict static/architecture checks, independent source review of changed risks and one complete successful final-head CI. There is no extra qualification stage or repeated unchanged campaign.

Migration diagnostics keep the current metadata owner and safe revision/count/reason fields. Codec failures use fixed content-free outcomes; no original error content, raw/private digest, SQL parameter, input or evidence body is logged. No new operational/domain event is introduced. The metadata foundations update their applicable observability evidence; the authoritative transitions retain their own activation obligations.

Threats TM-01/TM-08 preserve current access and authentication; TM-09/TM-11 preserve original identity and no invented authority/history; TM-06/TM-07 cover content lifetime/diagnostics; TM-10 covers admitted serialization and migration resource release; TM-12 covers compatible formats and truthful evidence. The targeted threat-model paragraph records the new content classification and pending implementation evidence; its exact docs wording awaits owner review. Actors and runtime trust assumptions remain unchanged.

## Audit and unresolved implementation evidence

Latest available master mutation audit `41189316843`, source/tooling `2b468c836e381e1b487effa5d455b5e74bb53d01`, was partial/budget with restored continuation and pricing/CEL-only scopes. Its existing summary/report were inspected again. It supplies no outcome/deadline finding, benchmark or PR mutation gate.

This design has no executable codec, schema migration, production transaction, content deletion or selected read evidence. Original full grant/process-snapshot persistence remains a prerequisite of its future writer. No runtime class or deadline mechanism is replaced; runtime/runner ownership remains unchanged. Implementations report their actual revisions, commands, final CI and remaining limits in their own PRs.
