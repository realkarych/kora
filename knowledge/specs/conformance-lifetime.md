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

Process acquisition records intent before spawning a core-only gated direct child. An invocation child stays in its group; for a detached domain the parent spawn operation establishes the new session/group before the core gate starts. The parent owns the child and its unreaped wait identity in both cases. The gate cannot execute provider code or create descendants until the parent captures final process/start/group/session identity and records acquisition. It cannot detach again after registration. The journal records the nonsecret acquisition ID before spawn; parent crash before concrete identity persistence never proves absence, and an unobservable partial spawn remains unknown. The parent uses a retained child/wait identity, a pidfd where available, or the accepted local ProcessIdentity recheck method with its stated residual race. Registration is the parent's completion of acquire, not an external callback. A gate EOF or timeout exits without launching. Parent close before activation refuses activation and closes the gated process; close racing activation either wins before release or owns/stops the released process and all registered descendants. An acquisition result arriving after closure joins cleanup, never returns a usable lease.

All ordinary children remain in the invocation domain. Deliberate independent sessions/process groups use a new parent-managed acquisition, except managed subdomains of an already registered local_runtime_domain, whose complete lifetime belongs to the fixed runtime controller below. The parent retains an unreaped group anchor until the owned group's membership is observed empty, and records/rechecks individual process identities; a numeric PGID, an exited group leader or pytest's status alone cannot prove emptiness. Unreadable/ambiguous observations are unknown, not absence. On Linux, where membership is found by reading every process status, a process whose status reports that it no longer exists (missing at open, or `ESRCH` on an already-open status descriptor) is skipped as a non-member; every other unreadable or malformed status keeps the whole observation unknown, and a matched member that vanishes before it is read is still reported absent.

A listed exited member keeps its domain non-empty. Existing native watches use pidfd hang-up on Linux or NOTE_REAP on macOS. For a listed late-collection member with either per-PID absence or a complete freshly validated exited snapshot without usable retained native readiness, a single observation pass may internally distinguish waitability from terminal refusal without changing its public result. Only a waitable collection case may use timed re-observation, at intervals of at least 100 ms after the preceding observation completes and within the original absolute deadline. Scope or identity mismatch, unreadable observation, capacity refusal and unexplained native-anchor failure refuse promptly. Ordinary validated live watches retain native waiting. Explicit disposal wakes settlement and returns unknown; an elapsed absolute deadline retains deadline-error priority. Only affirmative absence and the existing empty-domain confirmation establish settlement.

On Linux this needs a kernel whose pidfd reports the hang-up after the reap; otherwise such a close stays unknown at its deadline. The supported cooperative contract forbids an unregistered move out of a domain; this is not hostile-process containment.

A direct child's exit notification is not proof that its parent can collect it. On macOS, `NOTE_EXIT` can be observed before the child is a zombie, and the ordinary identity read can report that same child absent while `waitpid` with `WNOHANG` still returns zero. `GatedProcess.settle` for a Darwin direct child arms one shared non-consuming child-change watch before it probes, and it succeeds only when a separate `proc_pidinfo(PROC_PIDTBSDINFO)` read with a nonzero argument reports that the retained child is a zombie and a fresh domain observation is settled. The probe checks the current parent, pid, start time and process group. A child that is not yet a zombie may wait only when the fresh domain observation is live or settled. A zombie child still waits while that domain remains live or an existing member watch can still change. Unknown, mismatched, incomplete or failed probe and watch results, an unknown anchor, and a domain observation with nothing left to wait for all refuse. A watch completion is only a hint to repeat those checks. Waiting stays inside the caller's original deadline. Linux direct children keep the existing non-consuming `waitid` confirmation and do not attach this extra watch. `release` remains the only operation that collects the child, and it still requires capture EOF; a zero `waitpid` result keeps ownership for the existing cleanup retry. An elapsed deadline keeps priority over both success and a concurrent release. Checks around the probe do not interrupt the call or create a new kernel-latency guarantee.

A local service launched by the command is owned and must use the process-domain operation. A pre-existing service may be borrowed explicitly; all acquired local clients are still closed, and reports distinguish borrowed service continuation from owned-resource quiescence. A command-created daemon/container cannot be labeled borrowed. Owned containers and non-local resource controllers are refused until their protocol-specific typed controller and installed lifecycle tests activate with the Docker/independent runtime implementation; there is no generic arbitrary cleanup callback or command. Pending future controllers do not deactivate any current common/secrets/local-process lifetime case.

Temporary trees are created under a parent-owned private root (0700 directories, 0600 records). Creation/removal is descriptor-relative and no-follow, with identity/owner validation; a substituted path, unknown identity or foreign tree is unresolved and never recursively deleted by name. Cleanup keeps trees while any acquired or still-pending effect may use them. Pipes are continuously drained into bounded discard/content-free result channels, then closed after writers settle. Process-local task/fd/connection settlement is observed within a live core-owned execution context for cooperative cleanup and by confirmed process exit for fallback; a callback boolean never establishes it. Successful command completion requires both, as applicable, plus empty domains and temporary-state deletion.

## State transitions and bounded settlement

The command parent must stay able to fence, escalate, return unknown and actually exit if journal/filesystem I/O stalls. Potentially blocking durable writes, sync and tree operations run in bounded owned helper processes, not uncancellable executor threads whose shutdown can hold the parent. A helper and its earlier effect remain owned until observed settlement; an unkillable/stalled helper yields unknown with retained prior intent, not a clean exit report. Bootstrap record failure aborts before external fixture/resource acquisition. No retry, logger shutdown or helper join can extend the absolute close deadline. The implementation includes a real stalled-journal/filesystem case demonstrating parent deadline progress, escalation and actual parent exit, plus later helper/effect recovery; ENOSPC alone is insufficient.

States are `reserved`, `acquiring`, `gated`, `active`, `closing`, `closed`, `unknown`. `unknown` is a retained unresolved state within irreversible closure, not permission to reopen. State revisions only increase.

- `reserve` precedes `acquiring`. Reservation cancellation before dispatch closes only when the parent proves no operation was dispatched. It retains the terminal token until the invocation's control channels and all earlier operations have settled.
- `acquiring` precedes any effect. Partial failure records all acquired handles and closes them; no error unwinds ownership. Lost acquire/register/ack replies are reconciled by exact operation identity and parent observation. The same identity cannot bind a second process/tree.
- `gated` has a concrete parent-owned identity and cannot run independent provider work. `acknowledge` under the gate makes it `active`; closure instead makes it `closing`. Delayed requests under the closed token cannot launch anything.
- Only short ledger/activation transitions serialize. Spawn, readiness, observations, I/O and grace waits occur outside locks; close fences immediately despite a stalled acquisition. Existing runtime registry-lock and stop-path rules remain in force.
- `close` seals the complete resource subtree before cooperative adapter cleanup. Fixed core cleanup controllers may open bounded tracked control connections solely to stop/observe existing resources; this is cleanup work under the same close budget, never reopened execution/acquisition authority. It includes acquisition and cleanup operations already in flight. No older operation can outlive `closed`, create a new effect or reopen admission. Cleanup is idempotent and concurrent close calls join one operation.
- Core invokes `PreparedConformance.cleanup(exact_instance_or_none)` exactly once after successful prepare; a preparation failure has no prepared cleanup object and still receives full registry cleanup. Bound methods/resources created during construction and exercise remain under the same authority.
- Parent cancellation, child EOF/crash and setup/test errors initiate the same close path. Cooperative cleanup receives its deadline; then TERM and verified KILL escalation apply. The parent keeps its own teardown task and ignores subsequent cancellation requests as teardown cancellation, while retaining the original cancellation result.
- `closed` requires settled acquisition/cleanup operations, stopped and reaped direct children, affirmative empty owned domains, released task/connection/fd/pipe scopes and removed owned temporary trees. A successful runtime receipt alone does not cover a still-live supervisor.
- At a deadline, unavailable observations, permission failure, unsettled spawn, identity mismatch or deletion failure yield `unknown` and a nonzero command result. They retain a bounded actionable private journal, exact identities and exclusive roots. The initial failure/cancellation is not overwritten by cleanup failure. Cleanup evidence is released only after closure; observations and pending work never expire into success.
- Parent SIGKILL/host crash do not promise immediate cleanup. Journal recovery may resume only after confirming the recorded parent's identity is gone. Missing/corrupt journal or unreadable identity is unknown and requires operator handling; it does not authorize a speculative kill or clean result.

Default bounds: at most 256 resource records and 16 outstanding operations per invocation; 64 KiB control frames and 1 MiB aggregate private control/recovery metadata (execution input snapshots are separately bounded to 1 MiB and never retained in recovery); 32 unresolved public samples plus totals; 30 s acquisition, 5 s cooperative cleanup, 2 s TERM, 5 s KILL/observation, 2 s final pipe drain. These are product work bounds, not measured performance budgets. Excess is refused before acquisition; unresolved records are never evicted. Deadlines form one absolute close budget and are not restarted per resource or retry. The private conformance state root is `$XDG_STATE_HOME/kora/conformance` (default `~/.local/state/kora/conformance`) on Linux and `~/Library/Application Support/kora/conformance` on macOS, opened/verified with the existing local-runtime owner/no-follow discipline. A root-level admission lock bounds live or unresolved invocation journals to 32; saturation refuses a new run before effects and never evicts evidence. Admission scans only this fixed private directory and refuses after 33 entries; it does not discover cwd/config/plugins. Closed invocations discard their journal after all operation channels and senders are closed, so IDs are never reusable. Cleanup selects an explicit InvocationId in this root. Existing CI's ten-minute cap is unchanged.

Preparation, configured exercises and cleanup need compatible async ownership. #215 must run their async work on one core-owned loop for the invocation, by factoring case assertions into shared async checks. Direct-injection tests retain their public fixtures/case interface and call those checks through their synchronous entry; installed command cases call the same checks on the invocation loop with the exact selected object. Current secrets tests call provider methods directly via fresh asyncio.run, so driver wrappers alone cannot route them. No provider proxy, patching or duplicate assertion implementation is permitted. Pending tasks have explicit owners and are settled or terminated with their owning process; returning from an `asyncio.run` call is not an independent-domain cleanup proof.

Healthy idle IPC does not consume an acquisition interval or initiate resource closure. Each reader owns one cancellable readiness registration without an idle timer; peer EOF and local close still wake it. After readability, one acquisition deadline covers the complete frame/record and admitted request input; partial prefixes, chunks and metadata do not restart it. Outstanding ordinary and close exchanges retain their original deadlines while the reader waits, and a reply received at or after its original deadline is refused.

## Local-runtime seam

The existing constructor-injected runner launch seam gains a lower-layer `LocalLaunchAuthority` contract using the reservation/acquisition/activation types above. The conformance composition supplies it; serving runtime composition does not import testing roots. Before the runtime makes a `starting` entry, prepares persistent temporary state or launches a runner, it reserves the test-owned state root/domain and launch operation. The reference local runtime's conformance launch uses the parent-managed detached spawn in place of the ordinary double-fork path. The runner remains the existing production/testing entry point, with the same registry, retirement and recovery rules. Candidate/publisher/harness processes remain children of the existing runner/SDK, preserving launch_spec peer/parent identity and SDK reaping. They are managed subdomains of the pre-registered local_runtime_domain, not reparented conformance processes. The fixed parent controller uses shared runtime retirement/fallback and registry code to fence older dispatch and observe these domains after pytest exit, retaining exact domain/attempt/nonce/digest and pending launcher effects. Absence of a runner handle or a no_runner_started receipt is insufficient. Control/cleanup authority does not depend on a channel or callback owned solely by pytest.

The parent owns the runner's process/domain before the existing readiness acknowledgement. The existing `starting` nonce/digest and `running` compare-and-set remain authoritative within the runtime; the lifetime gate does not manufacture a Runtime receipt. Registration failure/close before runtime readiness tears down the gated or started runner under parent authority. Closing after activation fences further lifetime launch requests, asks the normal runtime cleanup while the child is available, and independently verifies launcher, runner and registered candidate/publisher domains and all pending launch operations before releasing state. It preserves runtime durable evidence through Runtime.release and while any source can still dispatch. Runtime.release still retains tombstones indefinitely. Destroying an entire uniquely invocation-owned disposable test environment is a separate outer teardown: permitted only once every process/task/dispatcher/channel able to submit into that domain is confirmed closed, all earlier operations settled, no external borrowed producer can address it, and no unknown publication or recovery remains. It never applies to a borrowed/production state root and never reports runtime release as having deleted tombstones. Otherwise the root stays as retained evidence and command cleanup remains unknown. Parent process termination is a test-local cleanup observation, not a replacement for API/runtime recovery or publication evidence.

LocalLaunchAuthority is a lower-layer narrow Protocol view of the same reserve/acquire/acknowledge/observe/close implementation: its acquire method takes the identical token, operation_id and LocalRuntimeLaunch request, with the same replay key and Acquisition result. It adds no launch alias, wrapper state machine or second dispatch. Shared process identity/signalling and runtime registry/retirement mechanisms belong in kora.runner under its existing package-layer row, reused by plugins.runtime and the conformance/testing roots; the conformance package owns only command orchestration, IPC and its fixture-facing ledger. #215 may establish the minimum generic local process primitives there when they do not yet exist, while work item 14 owns actual runtime/runner execution and registry implementation; neither supplies a temporary duplicate of the other. Local runtime receives the authority and parent-owned state-root resource through constructor injection. The lower-layer interface also exposes reserve so each attempt reserves its exact LocalRuntimeBinding before effects; a generic domain acknowledgement is insufficient. Its conformance constructor arguments bind these exact objects. They cannot override authoritative provider configuration: a configured state root incompatible with the owned disposable-resource contract is refused, never silently redirected to a fixture root. This ABI does not grant fixture code access to any server layer or arbitrary parent callbacks.

## Required tests and evidence

`test_conformance_fixture_resources_are_released` must include named parameter cases: normal closure; prepare/bind/construct/test/cleanup failure; cancel before dispatch; cancel during acquisition; lost acquire reply; lost acknowledgement; close before activation; close after activation before runtime readiness; a late successful acquisition; pytest exits with a live child; TERM-resistant child; registered detached domain; reused PID/group/domain identity; unreadable identity; duplicate/conflicting operation; queue/record capacity; owned local service; borrowed pre-existing service with released clients; substituted tree; broken/slow pipe; unresolved journal and explicit cleanup resumption; concurrent resumers; resumer crash; full-disk admission/registration failure; descriptor inheritance and raw-output markers; stalled journal/filesystem helper and actual parent exit; state-root admission saturation; a paused old journal/registry writer, parent exit and competing resumer, proving no stale replacement of the new owner/fence. Disposable runtime-domain teardown additionally proves that queued/late dispatch cannot outlive root deletion and that a borrowed/production root is never destroyed. Runtime activation additionally covers completed/retired receipts with a still-live supervisor, parent-only cleanup of a starting entry after pytest exit, preserved runner parentage/SDK reaping, cancellation across detached session establishment, descendant creation racing closure and every earlier launcher request settled.

Tests use real local processes, pipes and directories, explicit phase barriers and injected decision clocks; no mocks/patches/sleep synchronization. Installed fixture tests build/install a separate distribution and preserve exact selected metadata callable/instance, private schema snapshots, authoritative nonempty config, marker forwarding, all active env/secrets/StreamMasker cases and ambient-import exclusion. The currently active direct-injection common/secrets tests remain required. New runtime-specific cases remain pending the named runtime/runner owner, without claiming full protocol or first-slice readiness early.

The lifecycle measurement pre-registers warmup, repeated success/failure/cancel/shutdown cycles in one long-lived parent, multiple independent process runs and base/head environment. It records peak/settled Python and native memory, tasks/threads/timers, process/reaping counts, descriptors/pipes/connections, temporary trees and registry bytes, plus retained unresolved evidence separately. Quiescence uses explicit observed conditions. First execution establishes measured budgets under #215; no number here claims measured performance. Required counters unavailable on a platform are unverified, not passed.

Diagnostics have one parent reporting owner under the #215 conformance activation row. Public records contain fixed codes, core-generated nonsecret IDs, counts and bounded states only; never config/exercise content, exception text/locals, raw child output, argv/env/path/service endpoints or capabilities/tokens. Parent cleanup does not wait on diagnostic output. Map changes to TM-06/TM-07, TM-09, TM-10, TM-11 and TM-12; local limitations under TM-05 remain.
