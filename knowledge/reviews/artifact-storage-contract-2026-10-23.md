# Artifact-storage contract review — 2026-10-23

## Scope and authority

[Issue #229](https://github.com/realkarych/kora/issues/229) defines staged storage, immutable preparation/publication and mutation closure. Its [concrete plan](https://github.com/realkarych/kora/issues/229#issuecomment-6368221981) and the separate [#224 bounded-upload plan](https://github.com/realkarych/kora/issues/224#issuecomment-6368223396) were [approved by the project owner](https://github.com/realkarych/kora/issues/229#issuecomment-6368257832). ADR-0005 owns the protocol selection; ADR-0018/0019/0022 and the #140 physical-store/full-digest fence remain authoritative. The [#208 shared-owner cleanup amendment](https://github.com/realkarych/kora/issues/208#issuecomment-6367344003) is already approved and merged through #211; it is consumed unchanged.

This change specifies provider/core responsibilities and future evidence. It implements no provider, API route, migration, worker or retention task. Generic chunked implementation still requires #224's separate contract. The local-provider part of work item 8 gets a separate implementation issue and approved plan after both contracts merge. The actual metadata foundation lacks upload staging, worker staged-byte accounting and deletion intents.

## Independent planning coverage

Thirteen separate fresh contexts reviewed the plan against `18a229427afecba74e2e8d402027508c739a9a81`. The final proposal had SHA-256 `43ee4ef865199cdc516664ff7f017c4014eeff8649e19a96e0ada5e9dcce6cfd`; affected conclusions were rechecked after corrections. The merged #211 boundary at `85577d27e0fe38fb320571928b8a12e96fdc2e6a` has no API/metadata/protocol/scheduler/artifact-contract differences from inspected `c83609c`. This comparison does not replace review of the final change.

| Component | Direct or transitive boundary | Independent profiles | Planning status |
|---|---|---|---|
| Artifact storage | Byte identity/effects, publication attribution, private resource and evidence closure, inventory | correctness, security, performance, condensation | passed |
| API uploads | Authorization, immutable preparation, finalizer admission, acceptance and recovery | correctness, security, performance | passed |
| Metadata/retention | Intents, reservation/admission fences, cleanup eligibility and terminal bindings | correctness, security, performance | passed |
| Worker uploads | Immutable spool/replay, request-ready versus sealed authority, terminal truncation and HTTP resource closure | correctness, security, performance | passed |

API owns validation/current authorization and locked acceptance; metadata owns durable admission, reservations and acknowledgments; storage owns byte effects and actual closure; worker owns its source/HTTP resources. Unchanged immutable artifact validation and approval identity, history-read policy and runtime/supervisor authority stop further propagation. #226/#227 own grant-history/read projections. No console interaction changes, so browser design review is not applicable; existing browser CI remains mandatory. Source implementation will independently reassess condensation for every affected component and all applicable profiles.

## Decisions and resolved defects

| Risk or gap | Selected contract |
|---|---|
| Canonicalization would require rewriting sealed input | Separate immutable raw source and one prepared candidate; complete raw integrity before transformation and complete candidate reread before publication |
| Destination checked against raw bytes after transformation | Promotion binds to the selected prepared reference, its source/stage records and actual retained digest; incomplete source seals cannot publish directly |
| Source identity lost after accepted stage cleanup | One compact source-to-retained completion binding for every identity-changing acceptance, retained with owner metadata and expiry state |
| Concurrent final calls duplicate parsing/preparation | One durable core finalizer per stage through actual reader/work/effect closure, without database resources over I/O |
| Provider receipts or accepted private bytes accumulate uncharged | Separate finite private-byte/stage/deletion capacity and bounded semantic receipts; durable outcome acknowledgment and closed issuance/effects before compaction |
| Unlink mistaken for physical reclamation | Explicit read leases and actual descriptor/effect closure before retirement completes |
| Cleanup debt transferred to another abandoned upload | Only the already accepted independently eligible durable non-staging owner predicate can complete created-address cleanup without physical deletion |
| Partial transcript falsely verifies full source | Bounded immutable prefix with source_complete=false, core-only marker preparation and separate actual retained identity |
| Execution-request upload treated as sealed authority | Ready remains grant-bound, staged and reserved until successful seal_request |
| Inventory reports only staging | Bounded stages and content scans; current complete owner lookup and the deletion fence remain mandatory |
| Fewer method names would merge distinct authority | Retain separate stage/delete release and bounded prefix/candidate operations; reuse shared model/context/error/conformance facilities without a generic transformation framework |

## Required contract traces

| Trace | Required result |
|---|---|
| Individually valid parts whose concatenated digest differs from the original declaration | Refuse before canonical preparation or publication; retain cleanup ownership |
| Complete raw R canonicalizes to C, where R differs from C | Verify R, reserve/prepare/reread C, admit C's physical address and publish C without rewriting R |
| Prepared reference belongs to another stage or destination digest | Refuse the first invocation before effects, not only a replay |
| Promotion succeeds, response or outcome persistence is lost | Reconcile the same operation and preserve created/existing/unknown distinctions |
| Acceptance commits, response is lost and staging is removed | Authorized recovery returns the current durable descriptor through the source binding, with no validation or promotion |
| Owner expires before terminal recovery | Return its expired descriptor; do not renew, reconstruct or restore it |
| Over-cap transcript stops inside a non-final source part | Retain prefix plus the exact 24-byte marker within 256 MiB, make no full-tail integrity claim and refuse further source data |
| Request upload finishes but policy/authority changes before sealing | Remain ready/staged until the seal decision; refusal/expiry follows owned cleanup, never execution authority |
| Refused creator shares an address with an eligible permanent owner | Confirm stage closure and atomically release the refused reservation without deleting shared content |
| Only another staging row or transient hold exists | It cannot discharge created-object cleanup responsibility |
| An old reader or mutation still exists during retirement | Keep private bytes/capacity/evidence charged until actual closure |
| Delete response is lost, then a new owner arrives | Keep admission fenced until all old effects settle; a later upload performs a fresh promotion |
| Delete acknowledgment is lost or a process crashes around compaction | Preserve the exact outcome/handshake; an old operation cannot become a fresh delete after recreation |
| Scan omits an owner or observes absence | Observation alone cannot authorize deletion, clear a fence or close an effect |

The selected marker is 24 ASCII bytes; 256 MiB minus that marker is 268435432 bytes. The independent arithmetic check of empty, boundary and maximum part layouts establishes only those counts, not measured runtime performance.

## Implementation and verification status

Planning reviews passed. Final-diff specialist review and existing repository checks are pending for this document revision until their evidence is recorded in the PR. No runtime performance/security or containment claim follows from plan acceptance.

The [storage conformance table](../specs/protocols.md#artifact-storage-contracts-issue-229) preserves existing dedupe/read/delete behavior and names the staging/promotion/replay/cleanup/lifetime/deletion/inventory cases. Local work item 8 owns actual concurrent-instance, crash, restart, full-disk, cancellation and shutdown evidence. The concrete plan must prove durable physical identity, creation attribution, real issuer/effect closure, compacted replay guards, finite settings and complete copy/pass accounting. S3 activates its own real backend cases.

API/metadata/worker integration and #216 own both-database grants, source/prepared address admission, finalizer/resource lifetime, ready/seal, shared-owner cleanup, lost responses and delayed-delete races. They must specify compatible lock ordering and measure actual query/scanned-row work, pools/cursors/locks, memory, tasks, connections, descriptors and bytes across growing histories and repeated endings. Preserve at least 1,000 cycles per applicable integration ending, uncontended measurements, supported Linux/macOS paths and the ten-minute CI budget. Provider conformance cannot satisfy absent consumers.

All active CI remains mandatory on the final PR head, including static/tests, existing measurements, offline build, browser, Markdown/links/Mermaid and example/template checks. No gate, setting, budget or verification policy changes. Exact docs/ wording remains pending line-by-line owner review before merge.
