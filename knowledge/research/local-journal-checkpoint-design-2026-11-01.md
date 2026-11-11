# Local journal and checkpoint durability proposal

Historical design record: the daemon-writer, pre-registration-only exit and startup per-sync clock of the original proposal are superseded by the owner-approved [separate-helper contract](../specs/startup-io.md). Later running-state proposals remain unapproved; none is implemented by this design.

## Status and scope

Issue #615 is the design child of #196. The owner approved preparation and subsequently the startup-only contract on 2026-11-01: startup-before-registration, the 16 KiB record/checkpoint bounds, one operation, Running.startup and crash/cleanup semantics. Later running-state capacities, index representation and terminal formats remain proposals. The normative startup contract is recorded in [protocols.md](../specs/protocols.md#startup-durability-contract-issue-615) and [startup-io.md](../specs/startup-io.md); the original startup proposal wording is preserved at [revision 4368f4c](https://github.com/realkarych/kora/blob/4368f4c26eee7aa80017ce229d725865ce9fc43b/knowledge/research/local-journal-checkpoint-design-2026-11-01.md). Inspected base: `d2f14e3aa83421a0c1261c808d49d1e281d14f73`. No production implementation or platform experiment is included. Existing requirements remain authoritative until the owner accepts an amendment and it lands in the normative specification.

The [C2 decomposition](https://github.com/realkarych/kora/issues/196#issuecomment-6488037673) includes journal/checkpoint durability, transfer, command execution and adoption. [C1](https://github.com/realkarych/kora/issues/195) is complete through #614. The first durability implementation must have a real supervisor consumer and complete its own failure/cleanup paths; an unused store protocol is not a milestone. Runner-side full-disk fencing and terminal retry remain C2 obligations. Only worker fallback, public retirement/recovery, final release and provider registration remain #197.

## Existing rules retained

[Runtime protocols](../specs/protocols.md#local-runtime-issue-123) already specify canonical JSONL `{i, at, kind, data}`, append-assigned contiguous sequence, one writer, durable supervisor acknowledgement, Linux `fdatasync` and macOS `F_FULLFSYNC`, a five-second sync deadline, output batching at 50 ms or 256 records, and sparse indexing every 4,096 records. A readable complete line is not necessarily acknowledged durable. The first ENOSPC/EDQUOT append or sync failure permanently closes that process's journal for appends. Stop escalation and runner exit never wait on the writer or a registry lock.

The runtime is cooperative: same-user modification cannot be cryptographically excluded. Integrity checks detect mismatched or corrupted state but do not establish hostile-code isolation, dispatcher authority or termination. ADR-0016/0017/0018/0022/0026 continue to govern those boundaries.

## Proposed index

Use fixed-width network-order pairs of unsigned 64-bit `(sequence, byte_offset)`, one entry for sequence zero and each multiple of 4,096. With the proposed 257 MiB soft-plus-overflow journal envelope, ordinary index capacity is bounded by the minimum valid record size and checked separately; exceptional terminal records are finite by the closed kind/phase set. Index lookup must verify offset alignment, sequence and complete record against the journal before using a hint. Invalid, truncated, absent or unwritable index disables it; bounded streaming scan remains correct. An index never certifies durability or permits skipping malformed interior records.

The first implementation does not expose indexed readers. The subsequent indexed-reader slice owns index construction, corrupted-hint tests, index-disabled resume and actual memory/descriptor closure. No index functionality is claimed by the initial startup checkpoint alone.

## Proposed capacities and writer ownership

The existing journal/log capacities are still called proposals in the specification. Recommend accepting them unchanged: journal 256 MiB plus its specified 1 MiB supervisor overflow; stdout/stderr 64 MiB each; runner diagnostics 16 MiB; crash output 1 MiB. Existing once-per-attempt terminal exceptions remain; this is not a strict 257 MiB total-file guarantee. Output batching and the adapter queue remain their existing 50 ms/256 records and 4 MiB/4,096-record rules. Command-output activation must explicitly preserve its own gap/count semantics.

For the first startup consumer, admission is exactly one operation with at most one 16 KiB line and one 16 KiB checkpoint, including queued and in-flight buffers. The initial journal contains exactly one record `i=0`; the checkpoint requires `journal_next=1`, `journal_end` equal to that line length and a matching record digest. These 32 KiB of final encoded buffers are not a total heap guarantee: schema-bounded serialization intermediates are measured separately. No queue of startup waiters is created. The remainder of this section is deferred design input, not part of the immediate format approval. For later running producers, the provisional recommendation is at most 16 queued-plus-in-flight operations and 2 MiB of their encoded payloads combined, including checkpoint bytes. One operation owns all of its journal/checkpoint buffers and one completion future. Reserve before allocation/encoding beyond the declared operation bound; do not create an unbounded waiting queue. A caller outside the accepted operation set receives a fixed retryable busy refusal. Required internal terminal/fault work uses fixed dedicated state, not a seventeenth ordinary operation or an unlimited priority queue. Sixteen is a proposal aligned to the current maximum control connections, not proof that internal producers can never saturate it.

One daemon writer owns journal/checkpoint descriptors and buffers until the native operation returns. A loop-owned controller owns admission, the sticky memory fence, notification and timers. The existing `TransitionExecutor` must not own this writer: its close waits for real jobs and thread shutdown, contrary to the journal stop/exit contract. Reuse filesystem atomic replacement without duplicating registry semantics; journal data uses fdatasync on Linux, and directory/registry durability keeps the existing appropriate sync operation.

Normal closure stops admission, drains acknowledged work, receives a writer-closed notification, and verifies descriptor/thread release. Forced process exit after unresolved sync does not falsely report thread closure: the OS reclaims process-owned descriptors. No other thread closes/reuses a descriptor while the blocked writer still uses it. Stop escalation is driven independently of writer completion and lock acquisition. Test actual subprocess exit, not merely cancellation of an asyncio waiter. The five-second sync clock starts at native sync entry, not enqueue; queue limits do not introduce a longer sync or shutdown deadline.

## Later running-state full-disk evidence and terminal ownership

The internal storage result distinguishes `no_space`, `quota`, `sync_timeout` and other I/O failure without exposing exception text. Existing public registry refusals stay content-free. A runner-side adapter carries the typed storage cause to its failure controller; parsing the message `registry_unavailable` is forbidden. Every activated failure path has a single transition that closes admission, installs the memory fence before stop, and permanently disables journal appends for that process when required.

Proposed bounded diagnostic portion of terminal state:

| Field | Bound and meaning |
|---|---|
| `format` | Literal `local-journal-failure/1` |
| `cause` | Closed vocabulary `no_space`, `quota`, `sync_timeout`, `io_failure` |
| `readable_next`, `durable_next`, `discarded_tail_bytes` | Existing `Count`; durability never inferred from readability |
| `unwritten` | At most one counter for each activated, closed event kind; no key derived from payload or peer identity |
| Counter value | Existing `Count` plus `saturated` boolean; addition saturates without wrapping or silently claiming an exact total |
| `failed_checkpoint` | Boolean; no checkpoint payload or filesystem path |
| `publication` | Existing publication outcome/reference when the consumer exists; never fabricate `not_dispatched` from missing observation |

Proposed encoded diagnostic portion cap: 8 KiB. It is part of the existing compact 64 KiB head budget, not an additional head exemption. Identity/stop/evidence references use existing typed models and must be budgeted with it. Fixed-kind counter overflow coalesces into one explicit `other_omitted` count, never a map with arbitrary keys. Counts are not closure evidence. No potentially effect-bearing dispatch identity may be dropped to meet a byte cap.

The full terminal tombstone schema cannot be inferred from this diagnostic portion. The running-state implementation plan must provide its exact typed extension to the existing `Retired` head and the authentic receipt scope for its actually observed stop. It may not use `no_runner_started` for a registered runner or claim normal completion after a journal failure. If no honest terminal receipt can yet be produced, recovery remains unresolved; that is a blocker to activating the proposed running-state consumer, not a license to persist a made-up receipt.

The loop holds bounded terminal evidence and owns one retry timer: first retry at one second, exponential backoff capped at 30 seconds. The retry path is independent of the stalled journal writer and merges the current compact head under the existing lock without copying retained history. It does not append to the failed journal or delete user data to free space. Public retire, once implemented, joins the scheduled write rather than starting a competing one. This runner-side controller belongs to #196; #197 owns the separate worker fallback and its own re-read/retry behavior.

## Implementation handoff and decision boundary

The first implementation combines startup consumer, closed startup checkpoint/reference, supervisor journal native durability, typed startup failure, actual writer closure/forced process exit, registry CAS and owned leftover cleanup. A preliminary allocation is 100–160 lines for closed models/reference validation, 180–260 for writer/file operations, 120–180 for lifecycle/fault handling and 140–220 for supervisor/registry/cleanup wiring: 540–820 added/deleted production lines before shared-helper extraction. This is a planning range, not a verified diff; all deleted/refactored lines count too. The implementing child must give a function-level estimate before approval. Preserve the startup reference through takeover, gate changes and subsequent state transitions until an explicit cleanup rule releases it. Forced exit must account for the actual process entry point and its diagnostics shutdown: a daemon writer alone does not prove that exit avoids blocking cleanup. Include both obligations in real subprocess and registry-transition tests.

Subsequent C2 work remains explicit: continuation/ownership of the long-lived journal; running supervisor admission; exact full-disk tombstone extension and authentic receipt scope; batch/output/log caps and index/readers; transfer/spools/openat2; command/reaper/executor phases; and grants/adoption/followers. Their actual producer activation carries their own failure, cancellation, stop and release evidence. #197 retains the worker-side fallback and final provider registration. The early diagnostic portion above is a review proposal, not the complete terminal format or permission to activate Running writes.

## Executable evidence required later

- `test_journal_classes_and_caps`: exact ordering/ACK barriers, native sync, torn tail, permanent process-local closure, output/terminal accounting and disabled-index reads as their consumers activate.
- `test_startup_checkpoint_is_crash_consistent`: every row of the crash matrix, new-directory parent sync, uncertain registry commit preserving referenced startup artifacts, closed schema/bounds and foreign/corrupt state refusal using real files and subprocesses.
- `test_journal_writer_releases_resources`: actual ownership after success, cancelled waiters, failure, normal closure and forced subprocess exit with a blocked native operation.
- For later running consumers, `test_full_disk_fence_precedes_stop`: ENOSPC/EDQUOT, no later success after failure, stop independent of writer/lock, bounded terminal retry and no invented completion/receipt.
- Content-free DEBUG diagnostics and positive canary controls for checkpoint/journal/temporaries/terminal state; never use an empty capture as a secrecy proof.
- Reuse the existing Linux 3.11/3.15 and macOS 3.11 lanes and diagnostics framework. Current pressure fixtures provide ENOSPC only; EDQUOT/quota feasibility must be proved before its implementation relies on them. Current standalone diagnostics jobs do not prepare pressure mounts. Any necessary CI preparation amendment receives its own explicit review and retains deadlines.

Numerical time/RSS measurements are diagnostics unless an existing approved method makes them gates. Exact resource closure, integrity, capacity and deadlines remain blocking. No measurements were executed for this proposal.
