# Bounded worker-upload progress and recovery

Date: 2026-10-23. Scope: the behavioral contract in [#224](https://github.com/realkarych/kora/issues/224), following the staged-storage contract merged in [PR #245](https://github.com/realkarych/kora/pull/245). The [implementation plan](https://github.com/realkarych/kora/issues/224#issuecomment-6368223396) received [owner approval](https://github.com/realkarych/kora/issues/224#issuecomment-6368258293). This record supplies review evidence and activation ownership; the specifications define behavior. No runtime upload implementation ships with this design.

## Contract and rationale

The [API contract](../specs/api-and-agent-surface.md#bounded-chunked-uploads-issue-224) chooses the existing 16 MiB payload-part maximum as the exact non-final width. The 256 MiB cumulative cap consequently permits at most 16 public parts; an empty upload has one empty final part. The final part contains the exact positive remainder for every nonempty source, including a full final part at exact multiples. This excludes zero/tiny non-final progress without adding a separate arbitrary count limit or changing DD-6.

One staged owner retains bounded immutable part receipts, fixed expiry and one finalizer. Exact part replay and a fresh progress observation have different meanings. The explicit meta-only recovery mode can discover current progress, an unsealed ready request or the current saved terminal descriptor after response loss and staging cleanup. Canonicalization and server truncation use #229's durable source-to-retained binding. No recovery restores effect authority, superseded/expired content or old part receipts. Terminal transcript truncation never acknowledges unreceived bytes. Segment chunks and single canonical VcsRef uploads retain their separate contracts.

## Deterministic boundary traces

These are arithmetic and state-transition design traces, not runtime test results. A local integer calculation checked each row and the marker length on 2026-10-23.

| Source bytes | Public parts | Final-part bytes |
|---|---|---|
| 0 | 1 | 0 |
| 1 | 1 | 1 |
| 16777215 | 1 | 16777215 |
| 16777216 | 1 | 16777216 |
| 16777217 | 2 | 1 |
| 268435455 | 16 | 16777215 |
| 268435456 | 16 | 16777216 |

The ASCII marker `\n[transcript truncated]\n` is 24 bytes. An over-cap transcript retains at most 268435432 source bytes and that marker. Fifteen full parts plus a 16777192-byte prefix reach that source bound; the prefix does not become a false full-part receipt.

| Trace | Required result |
|---|---|
| First non-final index 0, size 0 or 1 | API003; no progress or expiry renewal |
| Source 16777217: index 0 non-final size 16777216, then index 1 final size 1 | Prefix advances once per validated part; whole-source verification precedes finalization |
| Source 16777216: index 0 final size 16777216 | Completes with no empty follow-up part |
| Index 1 arrives with no committed index 0 | API002 upload_part_order; no append |
| Exact retained index 0 receipt replay | Original prefix acknowledgment, no new receipt, append or renewed expiry |
| Same index with changed declaration or validated bytes | API002 upload_part_conflict; original winner and owner remain usable for the next index after request-local cleanup |
| Two instances race for the next index | One winner; identical loser reconciles, conflicting loser refuses, unresolved effect stays pending |
| Lost part response, then recover | Current committed prefix without accepting bytes; next data is exactly the needed immutable part |
| Lost terminal response after stage cleanup | Current authorized saved descriptor through recover; no parsing, promotion or fabricated part acknowledgment |
| Expired unsealed ready request | API002 upload_unavailable; no sealing right or reopened staging |
| Truncated completion followed by more source data | API003; explicit recovery can return the retained descriptor |

Every transition still checks current authorization. A malformed recovery envelope with payload is API003. Unknown effects/readers retain their capacity and reservations until actual closure; fixed expiry alone proves no settlement.

## Independent review scope

Planning used thirteen fresh component/profile contexts for the shared storage/upload proposal, with conclusions recorded in the approved issue plan. Readiness selects the complete diff against `ae7a2d4fcbf14cc5dff02a1e8ccb5c4952c36133`; returned final-revision results and executable evidence belong in the pull request.

| Component | Inclusion and boundary ownership | Required readiness profiles |
|---|---|---|
| API upload | Direct wire modes, validation, exact acknowledgments, acceptance and authorized terminal recovery; owns public response and finalizer boundary | correctness, security, performance |
| Metadata/retention | Durable winning progress, fixed receipt count/expiry, completion bindings and cleanup; owns DB side of API/storage and owner lookup | correctness, security, performance |
| Worker upload | Fixed part construction, immutable spool, response-loss recovery, ready/seal and terminal producer closure; owns HTTP/spool boundary | correctness, security, performance |
| Artifact storage | Generic part-layout/conformance reference and incomplete-prefix bound; owns provider receipt/effect/reader closure, distinct from wire acknowledgment | correctness, security, performance |

Propagation stops at unchanged runtime/supervisor authority, target-result atomic acceptance, saved artifact validation/approval identity and ordinary descriptor reads. No new endpoint, client business logic or console journey is introduced. Source/test/tooling/dependency changes are absent, so readiness condensation does not activate; storage abstraction condensation was independently reviewed in planning. Existing browser smoke and all existing executable checks remain required.

A metadata correctness finding identified an inherited refusal rule that could retire a valid shared upload after a losing replay conflicted. The contract now distinguishes request-local conflict/order/layout rejection from whole-owner authority loss, expiry, cancellation or validation failure. The former preserves committed progress and closes only its own resources; unknown effects remain charged. The named atomic-conflict case must prove the next valid index remains usable after cleanup. This resolves the ambiguity without weakening whole-owner retirement or provider closure.

## Runtime activation and evidence limits

The [named API cases](../specs/api-and-agent-surface.md#bounded-chunked-uploads-issue-224) activate with real API/metadata consumers in work items 11/12, worker 17 and [retention #216](https://github.com/realkarych/kora/issues/216), on both PostgreSQL and SQLite. Local provider work item 8 receives a separate owner-approved implementation plan after both contracts merge. Provider-only conformance does not establish consumer readiness. No placeholder consumer satisfies an integration gate.

Measure minimum/boundary/maximum sizes and concurrency, per-request work, receipt/query growth, actual spool/temporary-byte peaks and settled Python/native memory, tasks, threads, descriptors and HTTP/database connections. Repeat success, replay, conflict, disconnect, cancel, expiry, restart and shutdown under existing lifecycle minima; preserve unrelated heartbeat, stop and database progress. Intentional durable owners and unresolved safety state are distinguished from disposable leaks. Stored quotas and part counts do not prove RSS bounds, throughput or global request-rate limits.

API, metadata and worker diagnostics use the existing [activation and safe-field rules](../specs/observability.md#7-component-coverage-and-activation), including broken/blocked sinks; no body, digest, private reference or validation content becomes a diagnostic. New runtime tests and measurements remain pending implementation. Design traces, review verdicts and documentation CI are not substitute runtime evidence.
