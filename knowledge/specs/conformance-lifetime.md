# Local conformance lifetime ABI

[ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md) governs this ABI for the [installed-provider command](protocols.md#local-conformance-command).
The generic local lifetime library and [installed command](https://github.com/realkarych/kora/issues/215) use this ABI.
Local-runtime and testing-runner activation and evidence stay with work items 14 and 7, respectively.
Production-consumer activation and aggregate-concurrency qualification remain pending.
See the [accepted lifetime decision](https://github.com/realkarych/kora/issues/231#issuecomment-6367958968), [recovery amendment](https://github.com/realkarych/kora/issues/440#issuecomment-6508816138), [observation design](../research/recovered-settlement-observation-2026-10-30.md), [implementation evidence](../research/recovered-settlement-implementation-2026-10-30.md) and [command qualification](../research/configured-command-2026-10-27.md).

## Impact

The command parent retains lifetime authority across fixture and pytest failures.
Successful cleanup requires actual settlement of acquisition operations and owned resources.
Uncertain cleanup fails and retains private local recovery evidence.

## Scope and ownership

The installed bridge and parent command own installed-distribution evidence and use each activated suite's existing models and cases.
Those suites supply explicit typed bundles before installed activation.
Common and secrets checks, including forbidden references, list-names and `StreamMasker`, remain mandatory.
Policy and harness models and direct-injection policy cases retain their existing owners.
On activation, local runtime and the testing runner use this seam and retain authority over their runtime state machines.

The parent establishes a private invocation root, process identity, admission gate and control channel before any external fixture import.
The pytest child is an owned invocation resource.
The process lifetime owns its allocations, tasks, threads, connections, descriptors and pipe endpoints.
Surviving filesystem state and independently live processes require separate parent records.
Preparation receives the lifetime client and retains it in dependencies and drivers for acquisition during construction and exercises.
Return from prepare, cleanup or pytest preserves the original owner until settlement.

This is trusted cooperative local conformance, without containment of hostile same-user code, arbitrary daemonization or remote/cloud effects.
Metadata and adapter fixture imports are resource-free.
This ABI adds no hook, dynamic-import exception or entry-point group.
Automatic installation and provider factory replacement are refused.

## Public operations and closed shapes

Launch-facing types live in `kora.protocols.lifetime`.
`kora.conformance` re-exports the client and records.
Providers use lower-layer contracts under [ADR-0026](../../docs/decisions/0026-package-layers.md) and never import a composition root.
Use the existing runtime, harness and domain models and the approved layer table.
A necessary new row requires approval before edits.

`InvocationId`, `ResourceId` and `OperationId` are core-generated, non-reused 128-bit values in 32 lowercase hex characters.
Only the parent creates invocation and resource IDs and reservation tokens.
The pure `new_operation_id()` helper provides a fresh `OperationId` before dispatch, with an injectable identity source in implementation tests.
Clients retain the ID for replay.

Reserve and acquire each key replay by verb and operation ID within the invocation.
A lost reply retries the exact key and request.
Conflicting reuse refuses without another acquisition.
Acknowledgement names the acquisition operation ID and repeats its recorded result for the same acquisition and revision without reusing the acquisition key.
Close is monotonic per subtree and repeated calls join its recorded report.

| Type | Closed fields and authority |
| --- | --- |
| `LifetimeLimits` | Positive finite acquisition, cooperative, TERM, KILL/observation and drain deadlines. Bounds on records, outstanding operations, control bytes and retained evidence. The defaults are in [bounded settlement](#state-transitions-and-bounded-settlement). The parent monotonic clock governs deadlines. |
| `ResourceKind` | `process_domain`, `local_runtime_domain`, `temporary_tree`, `borrowed_service`. |
| `ReservationToken` | Binds invocation ID, resource ID, owner `ProcessIdentity` and positive uint64 generation. The token is private control authority and stays out of reports and diagnostics. |
| `ReservationRequest` | `operation_id`, `kind`, optional `parent_resource_id`, and `runtime_binding: LocalRuntimeBinding \| None` required exactly for a runtime domain. A parent resource must be an owned still-open resource of this invocation. |
| `Reservation` | `token`, immutable `kind`, `revision`, `state`. |
| `ProcessLaunch` | Bounded executable/argv/environment, parent-owned working-directory `ResourceId`, null `stdin`, captured or null `stdout` and `stderr`, and `domain` of `invocation` or `detached`. The parent owns subordinate capture pipes, separate from public output and lifetime control. Private execution inputs stay out of diagnostics and recovery evidence. No caller cleanup command is accepted. |
| `LocalRuntimeBinding` | `execution_domain_id`, `state_root_resource_id`, `attempt_id`, `launch_nonce`, `request_digest` with the existing runtime types. The parent already owns the state root. This immutable binding precedes starting-entry, workspace and launch effects. |
| `LocalRuntimeLaunch` | Exact `LocalRuntimeBinding` and `ProcessLaunch` for the existing production/testing runner. |
| `TemporaryTreeRequest` | Owned parent tree or invocation root. The parent chooses a fresh basename, creates it exclusively and anchors identity, without a caller deletion path. |
| `BorrowedServiceRequest` | Owner-generated opaque service reference and `pre_existing=True`. The invocation owns client connections and has no stop/delete authority on the service. The declaration relies on cooperative code. |
| `Acquisition` | Token, operation ID, resource revision and exactly one of `ProcessRegistration`, `LocalRuntimeRegistration`, `TemporaryTreeRegistration` or `BorrowedServiceRegistration`. |
| `ProcessRegistration` | `process: ProcessIdentity`, `acquisition_operation_id: OperationId`, `process_group_id: Pid`, `session_id: Pid`. Capture final group and session identity before provider execution and hold it immutable. PID alone confers no authority. |
| `LocalRuntimeRegistration` | Exact `LocalRuntimeBinding`, runner `ProcessRegistration`, fixed controller kind `local_runtime`. Shared retirement and fallback code acts on the anchored registry independently of the fixture. |
| `TemporaryTreeRegistration` | `resource_id: ResourceId`, `parent_resource_id: ResourceId`, `name: str` as core-generated 32 lowercase hex, `device: uint64`, `inode: uint64` and `owner_uid: uint32`. These fields are immutable and contain no caller path. Descriptors remain private. |
| `BorrowedServiceRegistration` | Opaque service reference and owning connection scope without stop/delete authority. |
| `ActivationAcknowledgement` | Token, acquisition operation ID, revision and parent-issued `active` or `closing`. Closure requires its own evidence. |
| `ResourceObservation` | Token-free resource ID, kind, revision, state, pending acquisition and live owned-process counts, remaining pipe/tree counts and closed reason. Exact identities remain with the private controller. |
| `ClosureReport` | Invocation and resource IDs, revision, `closed` or `unknown`, original outcome and separate cleanup outcome, unresolved total, capped content-free samples and opaque `CleanupReference` when unknown. |
| `CleanupReference` | One `InvocationId` naming its journal under the private state directory, with no caller path or second mapping key. |

The asynchronous `ConformanceLifetime` interface defines these operations.

- `reserve(request: ReservationRequest) -> Reservation` records ownership before acquisition and creates no requested resource.
- `acquire(token: ReservationToken, operation_id: OperationId, request: ProcessLaunch | LocalRuntimeLaunch | TemporaryTreeRequest | BorrowedServiceRequest) -> Acquisition` asks the parent to record the effect before dispatch and perform the operation under the verified kind, parent and generation.
  The parent owns the task independently of its waiter.
  Retrospective registration of a fixture-spawned detached process is refused.
- `acknowledge(token: ReservationToken, acquisition_operation_id: OperationId, revision: int) -> ActivationAcknowledgement` asks the parent to atomically check admission, record activation and release the pre-live gate.
  Failure, cancellation and lost replies retain ownership.
  A duplicate does not release another gate.
- `close(resource_id: ResourceId | None) -> ClosureReport` irreversibly fences the resource and descendants, or the invocation for `None`, and joins bounded cleanup.
  Cancelling the waiter leaves its cleanup owner running.
- `observe(resource_id: ResourceId | None) -> tuple[ResourceObservation, ...]` reads parent-authoritative state for at most 256 retained records.
  Fixture assertions grant no evidence.

Transport records are closed immutable values.
Process-held pidfds, wait handles, socket/pipe endpoints, directory descriptors and task handles stay in parent records indexed by `ResourceId`.
Public kind, state and reason values are closed enums.
The wire checks bounded `ProcessIdentity`, positive uint64 revisions and generations, bounded nonnegative counts and exact ID encodings before effects.
Unknown process identity fields refuse activation.
The launch snapshot is private and limited to one MiB in aggregate.
Use bounded chunks and reserve aggregate capacity before accepting input.
Malformed, incomplete or excessive input never dispatches.

Fixed reasons are `complete`, `cancelled`, `acquisition_failed`, `identity_unknown`, `identity_mismatch`, `effect_unsettled`, `resource_live`, `cleanup_failed`, `capacity_exceeded`, `owner_busy` and `journal_unavailable`.

The core-only `resume_close(reference: CleanupReference, limits: LifetimeLimits) -> ClosureReport` powers `kora conformance cleanup <invocation-id>`.
It uses fixed local cleanup recipes and anchored identities.
It refuses a live original owner and never reacquires, reactivates or imports provider or fixture code.
A failed invocation retains its private journal and root until closure is verified.
This is an explicit local operation for recovery.

The parent and resumers hold one exclusive process-lifetime invocation claim.
The journal binds the current owner `ProcessIdentity` and a monotonically increasing recovery generation.
Competing resumers fail within the bound before effects.
Replacing a crashed resumer requires confirmed identity exit and acquisition of the claim.
Signals, escalation and parent exit proceed independently of this claim.

Journal replacement is atomic and synced before acknowledgement of acquisition or activation.
Write failure refuses the transition and preserves prior ownership.
Durable filesystem records retain relative name, device/inode, owner and ancestor identities for safe reopening.
Mismatches remain unknown.
Private descriptors and wait anchors are local to the owning process.

Only fixed journal or registry mutation helpers inherit a duplicate of the claim.
Each holds it through write, rename, sync and observed exit.
This maintains the exclusive OS claim while an old writer can still publish after parent exit.
Before changing owner or generation or making another mutation, a resumer must acquire the claim and verify all previous writer/effect records settled.
Otherwise it returns bounded `owner_busy` or unknown.
Old helpers cannot publish into a later generation.
Allow at most one outstanding publisher per journal in a generation, with bounded queued snapshots in increasing revision order.
Runtime registry transitions retain their own short locked compare-and-write.
An unkillable old writer keeps ownership unresolved.

Recovery may discard the fixed abandoned `next` file only under the verified claim, after authoritative journal and directory validation, earlier owner/helper settlement and refusal of pending effects or exhausted generations/revisions.
Accept only an anchored, no-follow, current-owner regular file with mode 0600 and one link.
Retain unsafe or substituted entries.
Recovery never parses or promotes staging bytes, including after an interrupted partial write.
Unlink and sync the directory before changing owner/generation or publishing.
Failure preserves the authoritative journal and confers no takeover.
Ordinary publication keeps exclusive staging creation.

## Resource establishment and ownership transfer

The parent journals the nonsecret acquisition ID before spawning a core-only gated direct child.
An invocation child stays in its registered process group.
For a detached domain, the spawn establishes the session and group before the core gate starts.
The parent owns both children and their unreaped wait identities.
Before the gate permits provider code or descendants, the parent captures final process, start, group and session identity and records acquisition.
Registration completes the parent's acquire operation.
The child cannot detach again after registration.
A parent crash before concrete identity is persisted supplies no proof of absence.
Treat an unobservable partial spawn as unknown.
The parent observes through a retained child/wait identity, an available pidfd or the accepted local `ProcessIdentity` recheck with its residual race.

Gate EOF or timeout exits before launch.
A close before activation refuses activation and closes the gated child.
In a race, close either wins before release or owns and stops the released process and registered descendants.
A late acquisition result joins cleanup without granting a useful lease.

Ordinary children stay in the invocation domain.
A deliberate independent session or group needs a new parent acquisition, except for the managed subdomains of a registered `local_runtime_domain` under its fixed controller.
The parent retains an unreaped group anchor until affirmative empty-domain observation and records and rechecks individual process identities.
A numeric PGID, exited leader or pytest status alone establishes no emptiness.
Unreadable or ambiguous observations are unknown.
Linux enumerates all process statuses to determine group membership.
A status missing when opened or reporting `ESRCH` on the open descriptor is skipped as a non-member.
Other unreadable or malformed statuses keep the observation unknown.
A matched member that vanishes before its status is read is reported absent.
The cooperative contract forbids unregistered movement from a domain and offers no hostile-process containment.

A listed exited member keeps its domain nonempty.
Native watches use pidfd hang-up on Linux and `NOTE_REAP` on macOS.
Linux requires a kernel that reports the hang-up after the reap.
Otherwise cleanup stays unknown at the deadline.
For a listed late-collection member, per-PID absence or a fresh, completely validated exited snapshot without usable retained readiness allows the same observation pass to classify waitability internally once terminal failures are excluded.
The public observation result remains unchanged.
Only waitable collection permits timed re-observation, at least 100 ms after the previous observation completes and inside the same absolute deadline.
Scope or identity mismatch, unreadable observation, capacity refusal and unexplained native-anchor failure refuse promptly.
Ordinary validated live watches use native waiting.
Explicit disposal wakes settlement and returns unknown.
An elapsed original deadline takes priority with the deadline error.
Only affirmative absence with the existing empty-domain confirmation establishes settlement.

For a Darwin direct child, `NOTE_EXIT` can precede a zombie and an ordinary identity read can report absence while `waitpid(WNOHANG)` still returns zero.
`GatedProcess.settle` arms one shared non-consuming change watch before probing.
Success requires a separate `proc_pidinfo(PROC_PIDTBSDINFO)` read with a nonzero argument confirming that the retained child is a zombie, plus a fresh settled domain observation.
The probe checks parent, PID, start time and process group.
A child that has yet to become a zombie can wait only with a freshly validated live or settled domain.
A zombie still waits while the domain is live or a member watch can still change.
Unknown anchors, unknown, mismatched, incomplete or failed probe/watch facts, and a domain with nothing left to wait for refuse.
Watch completion only requests a new check within the original deadline.
Linux direct children use non-consuming `waitid` confirmation without the Darwin watch.
Only `release` collects a child, after capture EOF.
A zero `waitpid` result preserves ownership for the existing cleanup retry.
An elapsed deadline takes priority over success and concurrent release.
Checks around the probe leave the call non-interrupting and establish no new kernel-latency bound.

**Private gated framed transport (issue #2007).** The trusted runner can choose a bounded raw duplex endpoint for a gated child's stdin and stdout before launch. This choice is exclusive with borrowed streams and separate stdout capture. The original gate owns the endpoint and permits one borrow of an open endpoint for its exact process registration, after acknowledgement and before the inherited cutoff. Wire operations use the earlier of their requested deadline and that cutoff. Stop and failed preparation or acknowledgement close the endpoint. Settlement closes it with the gate's existing release path. The original wait owner confirms domain settlement, descriptor closure and stderr EOF before collection.

The typed reader can synchronously observe the first nonempty receive of a frame header, including a partial header. An initial EOF gives the existing closed outcome. The typed writer checks the send guard and cutoff before each nonblocking send. Its optional completion observer runs synchronously after the last successful send while the guard is still held. An empty allowed body completes on its last header byte. Partial or refused sends do not produce a completion observation. Only socket backpressure permits a retry. An observer or guard failure closes the channel and propagates the failure. Frame lengths, capacities and positions count bytes. Typed operations refuse unsupported buffers before starting a frame. A caller can require EOF after its last expected frame. These observations carry no process or operation authority and add no diagnostic content.

A command-created local service must use the parent-owned process-domain operation.
A pre-existing service can be borrowed explicitly.
Close every acquired local client and report borrowed service continuation separately from owned-resource quiescence.
A command-created daemon or container cannot be borrowed.
Owned containers and non-local resources are refused until their typed controllers and installed lifecycle tests activate with the Docker or independent runtime.
There is no generic cleanup callback or command.
Existing common, secrets and local-process lifetime cases remain required.

The parent creates and removes temporary trees under a private root with 0700 directories and 0600 records. Both operations are descriptor-relative and no-follow with exact identity and owner validation.
Substitution, unknown identity and a foreign tree stay unresolved without recursive removal by name.
Keep a tree while any earlier or pending effect can use it.
Pipes drain continuously into bounded discard or content-free result channels and close after writers settle.
Observe task, descriptor and connection settlement in the live core-owned context during cooperative cleanup and by confirmed process exit for fallback.
A callback result alone supplies no settlement.
Successful command completion requires the applicable observations, empty domains and temporary-state removal.

## State transitions and bounded settlement

The parent must be able to fence, escalate, return unknown and actually exit through journal or filesystem stalls.
Bounded owned helper processes perform potentially blocking durable writes, sync and tree operations.
Uncancellable executor shutdown must not hold the parent.
The helper and its earlier effect remain owned until observed settlement.
A stalled or unkillable helper leaves unknown with retained intent.
Bootstrap record failure refuses before external fixture or resource acquisition.
A retry, logger shutdown or helper join cannot extend the absolute close deadline.
Real stalled-I/O evidence must show deadline progress, escalation, actual parent exit and later recovery of the helper and effect.
An ENOSPC case alone is insufficient.

States are `reserved`, `acquiring`, `gated`, `active`, `closing`, `closed` and `unknown`.
Revisions only increase.
Unknown retains unresolved ownership within irreversible closure.

- Reserve before acquiring.
  Cancellation before dispatch closes a reservation only with proof of no dispatch.
  Retain the terminal token until invocation channels and earlier operations settle.
- Record acquiring before any effect.
  Partial failure records every acquired handle and closes it.
  Lost acquire, register or acknowledge replies reconcile by exact operation identity and parent observation.
  One identity cannot bind a second process or tree.
- A gated resource has concrete parent-owned identity and cannot run independent provider work.
  Acknowledge activates under the gate.
  Close moves to closing, and delayed requests under that token cannot launch.
- Serialize only short ledger and activation transitions.
  Spawn, readiness, observation, I/O and grace waits occur outside locks.
  Close fences immediately despite a stalled acquisition, preserving the existing runtime locks and stop path.
- Close seals the subtree before cooperative cleanup and covers all in-flight acquisition and cleanup.
  Fixed core controllers may open bounded tracked control connections solely to stop and observe under the original close budget.
  Older operations cannot outlive closed, create another effect or reopen admission.
  Replays and concurrent closes join the same idempotent cleanup.
- After successful preparation call `PreparedConformance.cleanup(exact_instance_or_none)` once.
  Failed preparation still receives full registry cleanup without a prepared callback.
  Bound methods and resources created during construction and exercise stay under the same lifetime authority.
- Parent cancellation, child EOF/crash and setup or test error share closure with bounded cooperation, TERM and verified KILL.
  Parent-owned teardown survives later cancellations and retains the original cancelled result.
- Closed requires settled acquisition and cleanup, reaped direct children, affirmative empty owned domains, released task, connection, descriptor and pipe scopes, and removed owned trees.
  A runtime receipt leaves any live supervisor unresolved.
- Unavailable observations, permission errors, unsettled spawn, identity mismatch or deletion failure at the deadline yield unknown and a nonzero command result.
  Retain the bounded actionable private journal, exact identities and exclusive roots.
  Preserve the original failure or cancellation separately.
  Release evidence only after closure.
  Time alone cannot turn pending work into success.
- Parent SIGKILL or host crash leaves cleanup for recovery.
  Resume only after confirming that the recorded parent's identity has exited.
  Missing or corrupt journals or unreadable identity yield unknown and require operator handling.
  They authorize no speculative kill or successful report.

| Default bound | Limit |
| --- | --- |
| Invocation records and outstanding operations | 256 records and 16 outstanding operations. |
| Private transport and recovery | 64 KiB control frames and one MiB aggregate private control/recovery metadata. Separate execution input snapshots are bounded to one MiB and stay out of recovery. |
| Public unresolved evidence | Up to 32 samples plus totals. |
| Phase bounds | 30 s acquisition, 5 s cooperative, 2 s TERM, 5 s KILL/observation and 2 s final pipe drain. |
| State-root admission | At most 32 live or unresolved invocation journals under a root-level lock. Scan only the fixed private root and refuse after 33 entries. |

These are product work bounds and preserve the existing ten-minute CI cap.
Refuse excess before acquisition and retain unresolved records.
Every resource and retry uses the original absolute close budget.

On Linux open `$XDG_STATE_HOME/kora/conformance`, defaulting to `~/.local/state/kora/conformance`.
On macOS open `~/Library/Application Support/kora/conformance`.
Use the local-runtime owner and no-follow verification discipline.
Admission reads no cwd, configuration or plugins and saturation never evicts evidence.
After closure, remove the journal once all operation channels and senders are closed.
IDs remain non-reusable.
Cleanup selects an explicit `InvocationId` in this root.

One core-owned invocation loop runs preparation, configured exercise and cleanup through shared async checks.
Direct injection retains its public fixtures and cases and calls the same checks from its synchronous entry.
The installed cases supply the exact selected object.
Current secrets cases call the provider directly under fresh `asyncio.run`, so driver wrappers alone cannot route them.
Provider proxies, patching and duplicate assertions are forbidden.
Settle pending tasks under their owner or terminate the owning process.
Return from `asyncio.run` supplies no independent-domain closure proof.

Healthy idle IPC uses one cancellable readiness registration per reader and no acquisition timer or closure trigger.
Peer EOF and local close wake the reader.
After readiness one acquisition deadline covers the complete frame or record and admitted request input.
Prefixes, chunks and metadata cannot reset it.
Ordinary and close operations retain their original exchange deadlines during the wait.
A reply at or after its original deadline refuses.

## Local-runtime seam

The existing constructor-injected runner launch seam gains a lower-layer `LocalLaunchAuthority` contract using the reservation/acquisition/activation types above. The conformance composition supplies it; serving runtime composition does not import testing roots. Before the runtime makes a `starting` entry, prepares persistent temporary state or launches a runner, it reserves the test-owned state root/domain and launch operation. The reference local runtime's conformance launch uses the parent-managed detached spawn in place of the ordinary double-fork path. The runner remains the existing production/testing entry point, with the same registry, retirement and recovery rules. Candidate/publisher/harness processes remain children of the existing runner/SDK, preserving launch_spec peer/parent identity and SDK reaping. They are managed subdomains of the pre-registered local_runtime_domain, not reparented conformance processes. The fixed parent controller uses shared runtime retirement/fallback and registry code to fence older dispatch and observe these domains after pytest exit, retaining exact domain/attempt/nonce/digest and pending launcher effects. Absence of a runner handle or a no_runner_started receipt is insufficient. Control/cleanup authority does not depend on a channel or callback owned solely by pytest.

The parent owns the runner's process/domain before the existing readiness acknowledgement. The existing `starting` nonce/digest and `running` compare-and-set remain authoritative within the runtime; the lifetime gate does not manufacture a Runtime receipt. Registration failure/close before runtime readiness tears down the gated or started runner under parent authority. Closing after activation fences further lifetime launch requests, asks the normal runtime cleanup while the child is available, and independently verifies launcher, runner and registered candidate/publisher domains and all pending launch operations before releasing state. It preserves runtime durable evidence through Runtime.release and while any source can still dispatch. Runtime.release still retains tombstones indefinitely. Destroying an entire uniquely invocation-owned disposable test environment is a separate outer teardown: permitted only once every process/task/dispatcher/channel able to submit into that domain is confirmed closed, all earlier operations settled, no external borrowed producer can address it, and no unknown publication or recovery remains. It never applies to a borrowed/production state root and never reports runtime release as having deleted tombstones. Otherwise the root stays as retained evidence and command cleanup remains unknown. Parent process termination is a test-local cleanup observation, not a replacement for API/runtime recovery or publication evidence.

LocalLaunchAuthority is a lower-layer narrow Protocol view of the same reserve/acquire/acknowledge/observe/close implementation: its acquire method takes the identical token, operation_id and LocalRuntimeLaunch request, with the same replay key and Acquisition result. It adds no launch alias, wrapper state machine or second dispatch. Shared process identity/signalling and runtime registry/retirement mechanisms belong in kora.runner under its existing package-layer row, reused by plugins.runtime and the conformance/testing roots; the conformance package owns only command orchestration, IPC and its fixture-facing ledger. #215 may establish the minimum generic local process primitives there when they do not yet exist, while work item 14 owns actual runtime/runner execution and registry implementation; neither supplies a temporary duplicate of the other. Local runtime receives the authority and parent-owned state-root resource through constructor injection. The lower-layer interface also exposes reserve so each attempt reserves its exact LocalRuntimeBinding before effects; a generic domain acknowledgement is insufficient. Its conformance constructor arguments bind these exact objects. They cannot override authoritative provider configuration: a configured state root incompatible with the owned disposable-resource contract is refused, never silently redirected to a fixture root. This ABI does not grant fixture code access to any server layer or arbitrary parent callbacks.

## Required tests and evidence

`test_conformance_fixture_resources_are_released` must include named parameter cases: normal closure; prepare/bind/construct/test/cleanup failure; cancel before dispatch; cancel during acquisition; lost acquire reply; lost acknowledgement; close before activation; close after activation before runtime readiness; a late successful acquisition; pytest exits with a live child; TERM-resistant child; registered detached domain; reused PID/group/domain identity; unreadable identity; duplicate/conflicting operation; queue/record capacity; owned local service; borrowed pre-existing service with released clients; substituted tree; broken/slow pipe; unresolved journal and explicit cleanup resumption; concurrent resumers; resumer crash; full-disk admission/registration failure; descriptor inheritance and raw-output markers; stalled journal/filesystem helper and actual parent exit; state-root admission saturation; a paused old journal/registry writer, parent exit and competing resumer, proving no stale replacement of the new owner/fence. Disposable runtime-domain teardown additionally proves that queued/late dispatch cannot outlive root deletion and that a borrowed/production root is never destroyed. Runtime activation additionally covers completed/retired receipts with a still-live supervisor, parent-only cleanup of a starting entry after pytest exit, preserved runner parentage/SDK reaping, cancellation across detached session establishment, descendant creation racing closure and every earlier launcher request settled.

Tests use real local processes, pipes and directories, explicit phase barriers and injected decision clocks; no mocks/patches/sleep synchronization. Installed fixture tests build/install a separate distribution and preserve exact selected metadata callable/instance, private schema snapshots, authoritative nonempty config, marker forwarding, all active env/secrets/StreamMasker cases and ambient-import exclusion. The currently active direct-injection common/secrets tests remain required. New runtime-specific cases remain pending the named runtime/runner owner, without claiming full protocol or first-slice readiness early.

The lifecycle measurement pre-registers warmup, repeated success/failure/cancel/shutdown cycles in one long-lived parent, multiple independent process runs and base/head environment. It records peak/settled Python and native memory, tasks/threads/timers, process/reaping counts, descriptors/pipes/connections, temporary trees and registry bytes, plus retained unresolved evidence separately. Quiescence uses explicit observed conditions. First execution establishes measured budgets under #215; no number here claims measured performance. Required counters unavailable on a platform are unverified, not passed.

Diagnostics have one parent reporting owner under the #215 conformance activation row. Public records contain fixed codes, core-generated nonsecret IDs, counts and bounded states only; never config/exercise content, exception text/locals, raw child output, argv/env/path/service endpoints or capabilities/tokens. Parent cleanup does not wait on diagnostic output. Map changes to TM-06/TM-07, TM-09, TM-10, TM-11 and TM-12; local limitations under TM-05 remain.
