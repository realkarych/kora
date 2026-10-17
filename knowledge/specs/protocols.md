# kora MVP design — the eleven protocols and the harness mappings

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: NFR-5.1–5.2, NFR-10.2, FR-2.3, FR-3.3, FR-5.2, FR-7.1, FR-8.1, FR-10.1, FR-11.3, FR-12.4. Interfaces are Python `typing.Protocol` classes; DTOs are frozen dataclasses or Pydantic models with JSON-only fields (NFR-4.3). Signatures are illustrative; names are the contract. Owner decisions on design choices are referenced as **DD-n** ("design decision n") and recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md).

There are eleven protocols: the nine of the original requirements plus **sensor** (DD-2, section 11) and **policy** (DD-29, section 12). Harness versions are fixed per installation (DD-14); each adapter declares the harness version range it was tested with, and a worker refuses to start (and `kora doctor` reports) when the installed harness is outside it (design review finding [harness version health check](../reviews/review-2026-10-15.md#harness-version-health-check)). Nodes never pin a version.

## 1. Common rules

- Every protocol has a `name` class attribute, a `capabilities()` method returning a frozen dataclass of flags, a `config_schema` (JSON Schema for its deployment configuration, validated at start-up and shown by `kora plugins list`), and a `conformance` marker that the test suite uses to discover it.
- Implementations are discovered through **one entry-point group**, `kora.provider` (NFR-5.2, design review finding [single provider entry point](../reviews/review-2026-10-15.md#single-provider-entry-point)): each entry point returns validated `ProviderMetadata(protocol, name, implementation, config_schema, version)`; `kora plugins list` prints what is installed per protocol; names are then used in the IR (`runtime.runtime: docker`) or in the deployment config:

```toml
[project.entry-points."kora.provider"]
inhouse_ci  = "kora_inhouse:runtime_provider"      # returns ProviderMetadata(protocol="runtime", name="inhouse_ci", …)
inhouse_vcs = "kora_inhouse:vcs_provider"
```

- All methods are `async` and cancellable; every failure is a `KoraProtocolError(code, message, retryable, details)`, and `retryable=True` maps to error class `infrastructure`.
- Implementations receive a `ProtocolContext` (namespace, run id, attempt id, logger, secrets resolver) and never import the scheduler.

## 2. Runtime (FR-8.1)

[ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md) defines execution uncertainty, durable retirement and dispatcher closure. [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md) separates the lifecycle supervisor from candidate execution and introduces late finalization. These interfaces are design contracts; no implementation or isolation experiment has run.

```python
@dataclass(frozen=True)
class RuntimeCapabilities:
    supports_resume: bool
    portable_handles: bool
    enforces_network_policy: bool
    credential_proxy: bool
    protected_execution: bool
    plan_executors: frozenset[Literal["runner", "worker_host"]]
    max_concurrent: int | None
    default_stall_after: timedelta

@dataclass(frozen=True)
class Workload:
    attempt_id: str
    execution_domain_id: str
    dispatcher_id: str
    execution_request: dict
    execution_request_digest: str
    kind: Literal["agent", "command"]
    resolved_config: dict
    workspace: WorkspaceTarget
    checkout: CheckoutPlan | WorkspaceRef
    publish: PublishPlan | None
    plan_executor: Literal["runner", "worker_host"]
    env: dict[str, str]
    secret_refs: dict[str, str]
    harness_credentials: dict[str, str]
    artifact_inputs: dict[str, ArtifactRef]
    limits: dict
    session_spec: SessionSpec | None

@dataclass(frozen=True)
class PublicationAuthorization:
    attempt_id: str
    execution_domain_id: str
    dispatcher_id: str
    operation_id: str
    prepared_digest: str
    destination_digest: str
    principal_id: str
    credential: SecretValue

@dataclass(frozen=True)
class DispatcherQuiescence:
    attempt_id: str
    execution_domain_id: str
    dispatcher_id: str
    worker_incarnation_id: str
    lease_generation: int
    closed_at: datetime
    evidence_ref: str

@dataclass(frozen=True)
class TerminationReceipt:
    attempt_id: str
    execution_domain_id: str
    retirement_id: str
    stopped_at: datetime
    evidence_ref: str

@dataclass(frozen=True)
class RecoveryEvidence:
    attempt_id: str
    execution_domain_id: str
    handle: Handle | None
    retired: bool
    termination: TerminationReceipt | None
    closed_dispatchers: tuple[DispatcherQuiescence, ...]
    publication_outcome: PublicationOutcome
    observed_at: datetime

class Runtime(Protocol):
    name: str
    config_schema: dict
    def capabilities(self) -> RuntimeCapabilities: ...
    execution_domain_id: str
    async def workspace_target(self, attempt_id: str) -> WorkspaceTarget: ...
    async def submit(self, workload: Workload) -> Handle: ...
    async def lookup(self, attempt_id: str) -> Handle | None: ...
    async def adopt(self, handle: Handle) -> AdoptedSession: ...
    async def status(self, handle: Handle) -> RuntimeStatus: ...
    def events(self, handle: Handle, from_offset: int) -> AsyncIterator[RawEvent]: ...
    async def heartbeat(self, handle: Handle) -> Liveness: ...
    async def finalize(self, handle: Handle, authorization: PublicationAuthorization) -> VcsRef: ...
    async def cancel(self, handle: Handle, grace: timedelta) -> None: ...
    async def recovery(self, attempt_id: str) -> RecoveryEvidence: ...
    async def record_dispatcher_quiescence(self, attempt_id: str, acknowledgement: DispatcherQuiescence) -> None: ...
    async def retire(self, attempt_id: str, grace: timedelta) -> TerminationReceipt: ...
    async def release(self, handle: Handle) -> None: ...
```

**Lifecycle and authority.** `kora-runner` is the lifecycle supervisor shipped with the runtime installation. It executes trusted preparation, hosts the adapter and policy client, enforces event/budget/stall limits, records observations, stops candidate execution, seals inputs and prepares publication. The candidate domain contains the harness binary, command workloads, tools, MCP servers, managed helper processes and all descendants. The adapter uses a runtime-supplied process launcher for argv, cwd, constructed environment, stdio and signals; the SDK/client and policy decision remain on the supervisor side. Merely setting a subprocess UID is not a complete protected launcher. The first slice must verify the full Claude SDK launch, hook, interrupt and subagent flow; the Codex app-server path must meet the same contract before it is supported.

The supervisor appends `{i, at, kind, data}` events to its **control journal**, outside the candidate root. On protected runtimes the candidate cannot read/write the control directory, replace a parent path, access its descriptors or impersonate its control channel. Plans, helpers, resolved checkout credentials and publisher metadata live there or in a separate private publication root, never under candidate `.kora/plan/`. Only the supervisor emits authoritative `phase` results; this kind is reserved, so the normalizer never forwards a harness payload as a phase result or platform VCS reference. Harness-derived events remain observations: protected journalling does not establish their honesty, completeness or tool-hook coverage. Candidate `.kora/` may hold harness configuration and checkpoints, but is never a lifecycle source of truth. Images disable harness auto-updates and record their digests on the attempt.

**One logical handle.** `submit` is idempotent on `attempt_id` and binds the saved canonical `execution_request_digest` (ADR-0018), not a serialization of every `Workload` field. The runtime verifies the supplied nonsecret request body and logical workload fields, binds allocated workspace/preparation-plan digests separately in its immutable checkpoint and rejects a changed request or changed previously accepted preparation as `workload_conflict` (`config_error`). Dispatcher/lease authority and injected secret/credential values never participate in this digest; changing those does not change the logical request or permit another execution. `lookup` returns `None` only for authoritative absence of a handle in the original registry, and raises retryable `KoraProtocolError` while unreachable or the registry lineage is unavailable. `None` does not prove that a paused worker cannot later submit; it never authorizes replacement by itself. `RuntimeStatus.lost` is also an observation, not a terminal attempt transition without the retirement/dispatcher evidence below. `adopt` returns the phase, prepared result if any, and journal position. `RuntimeStatus` retains `queued | starting | running | exited(code) | lost` and includes `phase`: `preparation | execution | sealing | inspection | awaiting_authorization | publication | complete | failed`. A harness exit alone does not make a publication-producing workload `exited`: it remains live through finalization. These phases do not add attempt states or consume another attempt/capacity slot. Workloads without a `VcsRef` output collect their ordinary outputs and complete without `finalize`.

Before a protected runtime begins inspection it confirms that the entire candidate domain stopped, including detached descendants, then seals a private handoff. Unconfirmed termination or sealing prevents authorization and fails with `publication_input_unavailable` (`infrastructure`); it does not authorize another concurrent execution. Local follows the lifecycle with ordinary process management in its trusted environment, without this adversarial guarantee.

**Late authorization.** After the supervisor reports `inspection` succeeded and `awaiting_authorization` with a `PreparedPublication`, the worker checks its current lease and desired state through the API heartbeat and obtains a fresh publication credential through the VCS integration. The parent credential remains on the worker/provider side; no publish credential is resolved at `submit`. Issuance, expiry, principal identity and effective backend rights are owned by issue #43; a static credential stays static and is never advertised as scoped or short-lived. Before dispatch the worker checks that every earlier dispatcher and its platform operations are closed (ADR-0017). The current worker owns a local dispatch gate; a lease check does not fence an already queued call. The worker calls `finalize` through an authenticated runtime control channel, not candidate stdio, files or environment. The authorization binds attempt, operation, prepared digest and destination digest; mismatches fail `publication_authorization_mismatch` (`config_error`). Values are redacted and never persisted. Expired/rejected credentials can be reacquired for the same operation; no retry reruns the agent or inspection against mutable files.

`finalize` coordinates the selected common plan executor. With `runner`, publication runs in the runtime's trusted publication environment; with `worker_host`, the runtime adapter transfers sealed input to the private worker-host executor immediately after sealing, before prepare/inspect. That executor produces the stored `PreparedPublication`; `finalize` subsequently delivers authorization to it and consumes that existing checkpoint. VCS tools may exist only on that host. It never points a credentialed publisher at the exposed candidate directory. Placement is a runtime transport responsibility, not VCS-specific logic. A runtime declaring portable handles must also make sealed input, journal and publication checkpoint accessible to an adopting worker; host-local storage cannot support that claim. An active finalization is serialized per operation, and repetitions reconcile the same prepared revision (section 4). The worker opens the change request through the VCS API, uploads outputs/usage and requests `finishing`; it keeps heartbeating through authorization and publication. An API fence is not an atomic fence against an external push already in flight.

**Saved request before effects (ADR-0018).** Before `workspace_target` or any preparation/submission effect, the worker seals the nonsecret logical request through the existing attempt-reporting API and receives its durable digest. It uses current policy revision and capability checks while the request is unsealed. The body owns exact input bindings, rendered config/feedback, composed execution policy, logical workspace/retry/publication intent, selected implementations/resources, limits and secret references; the original domain is bound as an immutable execution identity. Request sealing uses current execute authority and result authority, and does not grant or renew either. Sealing retries of identical content are idempotent. An unsealed cancelled attempt has no fabricated request.

A workspace root may be allocated only afterwards. `WorkspaceTarget`, executor placement, checkout/publish plans and harness configuration files are derived using the saved logical values and trusted provider bindings. The runtime records their exact digests before applying them and compares duplicates against that checkpoint. Allocation paths and actual checkout results are observations, not reasons to re-render the prompt or change the semantic-request digest. Request and private checkpoint retention are separate from the lifetime of an API lease. New recovery authority adopts existing trusted state under ADR-0017; it does not reconstruct a request or restart preparation from current aliases, VCS HEAD or feedback. The existing session-spec builder may use the allocated root before checkout, but must preserve the saved logical request and its rendered text.

Actual retry fallback can be established only after runtime/harness checks. The request records intended strategy and prior session/artifact identity; the supervisor records selected strategy/fallback and installed harness version when known, bound to the request digest. A fallback permitted by the request is a recorded decision under the pinned semantics; it does not silently replace intended inputs. A contradictory runtime/session state is an explicit failure.

**Execution-domain registry and dispatch gates.** `execution_domain_id` is available without preparing a workspace or submitting work and identifies the durable authority/registry lineage. Persist it and each dispatcher grant in the API before any effect; authenticate runtime control calls with the opaque dispatcher identity bound to that grant. `workspace_target`, preparation, submission and finalization may not bypass this control gate. The API epoch remains an API fencing value, not a provider-enforced external fence. Losing or replacing the registry requires a new domain id; even if lookup in the new registry is empty it cannot certify old-domain termination. No shared worker filesystem or separate coordination service is required: the runtime's existing control registry owns its fence and evidence. `PublicationOutcome` distinguishes no operation dispatched, pending/unknown, completed and confirmed failed, with the operation identity and reference when available; it cannot infer no operation from missing state.

Each trusted worker serializes its final lease/desired-state check and every effect dispatch with its local gate. On closure it atomically rejects all future dispatch under that grant, drains in-flight work or obtains authoritative provider fencing, then records `DispatcherQuiescence`. Closure is irreversible for that dispatcher id. A mere API rejection, process heartbeat gap or runtime receipt cannot certify these steps. The API retains **all** grants, including a worker paused before its first runtime call. `record_dispatcher_quiescence` authenticates the originating incarnation, binds every id and generation, and appends immutable/monotonic evidence in the original registry; it permits closing an old grant, never starting work. `recovery` returns this evidence even if no handle exists. A current claim performing recovery relays it through existing API reporting routes; stale epochs remain rejected for every API write. Missing/contradictory evidence is unresolved, never a fabricated acknowledgement.

**Retirement and termination evidence.** `retire(attempt_id, grace)` installs a durable per-attempt fence in the same registry/serialization boundary as `submit` and `finalize`, including when called before any handle exists. Racing submit either registers before the fence and is stopped with all managed candidate/publisher processes, or is rejected without starting. Finalization either settles its recorded operation or remains unknown; retirement may not return a receipt while a managed publisher or its external operation can still take effect. The fence also rejects later preparation and new publication dispatch. A queued duplicate of a completed operation can return the recorded result read-only, but never re-execute an effect. `retire` is idempotent: repeated success returns the same receipt; interruption/unreachability/uncertain termination is a retryable `KoraProtocolError`, with no affirmative receipt. `cancel` remains a cancellation request, not proof. A completed normal lifecycle records equivalent affirmative runtime quiescence and fences future dispatch; successful work remains successful. `release` is allowed only after the API has settled all dispatcher and publication recovery. Small registry entries, receipts, acknowledgement records and per-attempt tombstones survive bulky workspace/control-journal cleanup indefinitely; a future bounded-retention scheme must first prove no old dispatch can arrive.

A receipt binds the original attempt/domain and attests only to work controlled by that runtime. It does not certify independent worker-host publication or a worker's code-host call. The API's replacement barrier clears only after runtime quiescence **and** every recorded dispatcher and kora-managed publication is closed. Provider evidence or an operator's explicit attestation may establish closure of an inaccessible old authority, with provenance kept distinct. Already-issued agent/MCP effects are not rolled back and are not promised exactly-once. On local these are cooperative guarantees in the trusted development/test environment, without sandboxing or hostile same-user containment.

**Recovery state.** Before acknowledging each completed phase the supervisor durably records attempt/workload digest, immutable plans and their digests, trusted `WorkspaceRef`, journal sequence, phase, sealed input location/digest, `PreparedPublication`, inspection verdict and publication outcome. The checkpoint binds the API-sealed execution-request digest, exact derived plan/workspace identities and observed facts. Credential references/identities only are retained, never values or secret-derived hashes. Request/phase observations use the existing trusted journal and API event path; missing observations remain unknown, and duplicate journal sequence delivery is idempotent while conflicting content is rejected. A crash after sealing or preparation resumes from those bytes; no new commit is made. Loss/corruption of required private state is an infrastructure error, but leaves recovery unresolved until termination and dispatch/publication closure are proven. `release` removes bulky candidate, control-journal and publisher storage only after final output collection, terminal publication outcome and settled recovery; durable retirement/dispatcher registry evidence remains; partial usage/logs may still be collected on cancellation, but cancellation never initiates publication.

**Capabilities and reference runtimes.** `protected_execution` covers control-state integrity, whole-domain containment, immutable transfer and publisher credential isolation together. It is independent of `enforces_network_policy` and `credential_proxy`. Namespace policy `require_protected_execution` rejects a runtime declaring false (`IR021`); worker startup rejects a configured protected runtime whose installation cannot enforce its declaration. A missing executor is an error, not a warning that permits execution.

| runtime | lifecycle placement | protected execution | recovery and executors |
|---|---|---|---|
| `local`, Linux/macOS | host supervisor, candidate subprocess group, private directories for ordinary lifecycle structure | false; trusted development/tests, no sandbox, VM, administrator permission or extra trust switch | host-bound handles; `runner` and `worker_host` |
| `docker`, Linux engine (including a Linux VM on a macOS host) | host supervisor; separate candidate and publisher containers under one attempt handle | required supported configuration: candidate without control mounts, Docker socket, privileged mode or host PID namespace; confirm whole container stopped; private sealed transfer; publisher sees only sealed input and trusted metadata | host-bound handles; `runner`; Docker-dependent tests run on Linux CI |
| independent runtime | protected supervisor/control channel and isolated candidate/publisher environments supplied by the provider | declare true only with containment, transfer and OS/service evidence; otherwise rejected when required | no shared worker filesystem required; handles portable only when all recovery state is portable |
| configured fake runtime | real protocol implementation with injected deterministic events and phase barriers | proves protocol decisions, never OS isolation; security claims require real process tests | `runner`, nothing exposed on worker host |

Local process groups provide cleanup, not a hostile-code boundary. The same capability rules apply in standalone and shared installations. Neither reference runtime supports cross-attempt harness resume in MVP: `supports_resume = False`, because `release` destroys its harness checkpoint. `portable_handles = False` requires access to the original runtime domain, normally on the same host; the stable host id is only a routing hint and registry loss cannot impersonate the old domain; partition/loss policy is in scheduler.md section 3. `credential_proxy = False` remains the MVP default. Harness checkpoints and VCS revisions are separate from the lifecycle checkpoint: the VCS revision outlives release, the reference harness checkpoint does not. A future runtime declares `supports_resume` only if that harness state survives release and is reachable by the next attempt. Enterprise workers still connect outbound to the API, and default `stall_after` remains 15 minutes.

## 3. Harness adapter (FR-2.1, FR-3.3, FR-6.6, FR-9.1, DD-27)

Reference adapters speak each harness's **programmatic interface** instead of parsing CLI output. MVP ships two (ADR-0004): `claude` uses the Claude Agent SDK for Python (which spawns the pinned `claude` binary and speaks to it over stdio) and `codex` speaks the `codex app-server` JSON-RPC protocol directly through a small kora client, because the Codex SDK exists only for TypeScript. "SDK event stream" in this document means the messages the Claude Agent SDK yields or the notifications the Codex app-server sends. **After MVP** an `acp` adapter speaks the Agent Client Protocol (JSON-RPC over stdio) to any ACP-capable agent (OpenCode, Pi, Gemini CLI and others), selected in the IR as `harness: acp:<agent>`; it implements this same protocol and passes the same conformance suite, so it arrives without a change to the graph format. Until it ships, `acp:<agent>` is a reserved harness name that validation rejects (`IR018`, [ir.md](ir.md) section 10). kora still enforces `max_events`, the soft budget and `stall_after` itself from the SDK event stream (NFR-8.2).

```python
@dataclass(frozen=True)
class HarnessCapabilities:
    resume: bool                    # the harness can continue a session from its handle when the runtime kept the checkpoint (RuntimeCapabilities.supports_resume)
    reports_usage_per_turn: bool    # needed for partial usage after a kill
    policy_coverage: frozenset[Literal["shell", "file_edit", "mcp_tool", "subagent", "hosted_tool"]]
                                    # call kinds the harness's PreToolUse hook routes to the policy protocol (section 12);
                                    # validation compares every tool_policy pattern with it (IR008, IR019)

class HarnessAdapter(Protocol):
    name: str
    config_schema: dict             # deployment config incl. `credentials: {ANTHROPIC_API_KEY: "secret://…"}` (DD-36)
    tested_versions: str            # PEP 440 specifier of installed harness versions this adapter was tested with (design review finding: harness version health check)
    def capabilities(self) -> HarnessCapabilities: ...
    def check_config(self, config: dict) -> list[Warning]: ...          # compile-time: fields it cannot honour (IR008); never compares versions (DD-14)
    def build_session_spec(self, config: dict, workspace: WorkspaceTarget, feedback: Feedback | None) -> SessionSpec: ...
        # everything the runner needs: SDK options, files to materialise (agents, MCP config, skills, schema), prompt, resume handle;
        # built by the worker before the checkout plan runs, so it sees the workspace root (section 4), not the commit
    async def run(self, spec: SessionSpec, policy: PolicyClient, sink: EventSink) -> SessionResult: ...
        # executed by the runner inside the runtime: opens the SDK session, forwards every SDK message as a NormalisedEvent
        # (session_started | turn | tool_call | tool_result | permission_denied | usage | result | error) to the sink;
        # the supervisor alone writes lifecycle `phase` results (sections 2 and 4)
        # routes pre-tool-call hooks to `policy`, honours sink.enforcement() (terminate on max_events / budget / stall)
    def finalise(self, events: list[NormalisedEvent], outcome: RunnerOutcome) -> SessionResult: ...
        # AgentSession fields, StructuredOutput payload, usage records with completeness, stop reason, error class
    def installed_version(self) -> str: ...     # from the SDK's init message; recorded on AgentSession, never enforced (DD-14)
```

A fake adapter (`fake`) replays recorded SDK event streams for CI (NFR-10.1). Adapters are importable in supervisor installations; their harness subprocesses launch through the runtime boundary (section 2).

**First-slice defaults** (DD-36). The normalised event is `{i, at, kind, data}`; `max_events` counts `turn`, `tool_call` and `tool_result` events; termination is interrupt, 30 s grace, then kill. The `claude` adapter opens a `ClaudeSDKClient` with `cwd` set to the workspace, `CLAUDE_CONFIG_DIR=<workspace>/.kora/claude` and `setting_sources=[]`, so the harness state the SDK writes there has the workspace's lifetime and is released with it: on the MVP runtimes a stored `resume` handle points at a checkpoint that no longer exists once the previous attempt was released (section 2); harness credentials come from the adapter's deployment configuration (`credentials`, secret references resolved at submit) and reach the session as environment variables (DD-30). Structured output is taken from `ResultMessage.structured_output`, else from the final text parsed as JSON, else the attempt fails `artifact_invalid`. Usage maps per model from the result message into `usage_record` rows with completeness; the `AgentSession` payload fields are listed in [ir.md](ir.md) section 3.

## 4. VCS (FR-5.2)

The VCS plans; kora's common executor runs its typed plans. [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md) supersedes collection in the candidate working copy. Requests carry no runtime identity. A VCS can supply Git or an independent native representation without changing the core.

```python
@dataclass(frozen=True)
class VcsCapabilities:
    change_requests: bool
    review_comments: bool
    ci_status: bool
    head_sha_watch: bool
    plan_executors: frozenset[Literal["runner", "worker_host"]]

@dataclass(frozen=True)
class WorkspaceTarget:
    root: str
    host_path: str | None

@dataclass(frozen=True)
class CheckoutRequest:
    repo: str
    rev: str | VcsRef
    root: str
    attempt_id: str
    branch: str

@dataclass(frozen=True)
class PublishRequest:
    repo: str
    root: str
    attempt_id: str
    branch: str
    commit_message: str

@dataclass(frozen=True)
class PlanFile:
    area: Literal["control", "publication"]
    path: str
    content: bytes
    mode: int

@dataclass(frozen=True)
class PlanStep:
    argv: tuple[str, ...]
    cwd: Literal["candidate", "control", "publication"]
    env: dict[str, str]
    stdin: bytes | None
    timeout: timedelta
    capture: str | None

@dataclass(frozen=True)
class CheckoutPlan:
    required_tools: tuple[str, ...]
    credentials: dict[str, str]
    files: tuple[PlanFile, ...]
    steps: tuple[PlanStep, ...]

@dataclass(frozen=True)
class PublishPlan:
    required_tools: tuple[str, ...]
    files: tuple[PlanFile, ...]
    export: tuple[PlanStep, ...]
    prepare: tuple[PlanStep, ...]
    inspect: tuple[PlanStep, ...]
    publish: tuple[PlanStep, ...]

@dataclass(frozen=True)
class WorkspaceRef:
    attempt_id: str
    root: str
    repo: str
    commit: str
    base_commit: str
    branch: str | None

@dataclass(frozen=True)
class PublicationInput:
    attempt_id: str
    format: str
    location: str
    digest: str

@dataclass(frozen=True)
class InspectionContent:
    revision: str
    path: str | None
    kind: Literal["file", "symlink", "revision_metadata"]
    content_location: str
    content_digest: str
    size_bytes: int

@dataclass(frozen=True)
class InspectionManifest:
    prepared_digest: str
    revision: str
    content_index_location: str
    content_index_digest: str
    complete: bool

@dataclass(frozen=True)
class PreparedPublication:
    attempt_id: str
    operation_id: str
    input_digest: str
    prepared_digest: str
    inspection_digest: str
    repo: str
    branch: str
    commit: str
    base_commit: str
    destination_digest: str
    changed_paths: tuple[str, ...]
    diff_stat: str

class Vcs(Protocol):
    name: str
    def capabilities(self) -> VcsCapabilities: ...
    async def checkout_plan(self, request: CheckoutRequest) -> CheckoutPlan: ...
    async def publish_plan(self, request: PublishRequest) -> PublishPlan: ...
    async def open_change_request(self, ref: VcsRef, title: str, body: str) -> VcsRef: ...
    async def change_request_status(self, ref: VcsRef) -> ChangeRequestStatus: ...
    async def head_sha(self, ref: VcsRef) -> str: ...
    async def post_comment(self, ref: VcsRef, body: str) -> str: ...
    async def ci_status(self, ref: VcsRef) -> CiStatus: ...
```

**Trusted roots and captures.** The runtime privately allocates `control_root` and `publication_root` outside `WorkspaceTarget.root`; `host_path` exposes only the candidate workspace for compatible preparation, never permission to publish there. `PlanFile.path` is relative to its named private area, rejects traversal/absolute paths, and is created with descriptor-relative no-follow operations. Parent replacement, symlinks and writable hard-link aliases cannot redirect private writes. Plans are immutable trusted provider output, not files loaded from the repository. Allowed placeholders in argv/env/stdin are `{candidate_root}`, `{control_root}`, `{publication_root}`, `{input_location}`, `{base_commit}`, `{commit}` and `{branch}`; all values come from supervisor state. There is no shell interpolation.

Captures are bounded typed outputs: `commit` and `branch` are validated single identifiers; `changed_paths` is a JSON array of relative paths, not newline-split text; `diff` is display text, not an executable validation source; `diff_stat` is display text. The provider-owned native decoder additionally emits the common `InspectionManifest` and `InspectionContent` index defined below; an opaque native manifest alone is not sufficient. Missing/malformed captures fail the phase. Core treats `PublicationInput.format` and revision strings as opaque; only the provider understands them. Locations name immutable bytes reachable through the runtime's transfer channel, not a worker-local filesystem assumption.

**Preparation.** The selected executor creates an empty candidate root, materializes plan helpers only in control storage, checks required tools and executes the checkout plan with a minimal environment and checkout credentials. It captures `commit`, optional `branch`, and `changed_paths` for a `VcsRef` checkout. The supervisor constructs `WorkspaceRef` using the trusted request and persists it before starting the harness. For a `VcsRef`, `base_commit` comes from that input and protected-path checks run before a command gate. Checkout secrets/helpers and credential stores must not be inherited by candidate code. Reclaiming an attempt adopts a completed preparation rather than deleting a live workspace; a pre-submit absence alone permits no preparation anew while an earlier dispatcher may resume. Recovery observes/adopts the same attempt or retires it under section 2; only a never-dispatched first claim can prepare without resolving earlier authority.

**Export and sealing.** `PublishPlan.export` runs, if needed, in the candidate domain after the session, with no checkout, publication or code-host API credential. Its output is untrusted data until validated. The supervisor then terminates that entire domain and copies the export and selected workspace content into a private `PublicationInput`. Reads must resist path replacement, traversal, symlink dereference and hard-link aliases; symlinks that are legitimate VCS data remain data, not paths the publisher follows. Reject special files or unsupported encodings rather than guessing. Copied bytes have no writable alias in candidate storage. The supervisor fixes their digest before acknowledging sealing. Local uses the same copy/validation path without claiming that a hostile same-user process cannot interfere.

**Prepare and inspect publication.** `prepare` and `inspect` execute only in fresh publication storage, without VCS credentials, candidate configuration, executable attributes, helpers, plugins or live candidate processes. `prepare` yields one exact native revision; it is persisted once with the input and plan digests. `inspect` derives changed paths, review diff and the complete newly published content/history from that revision. kora checks protected paths, scans all newly published content for resolved secret values and common key patterns, and verifies a nonempty change against the trusted base. A hit is `secret_leak`; malformed/empty/unsupported content is `artifact_invalid`. Relevant secret values are available only to the trusted scanner and redactor, not the provider's subprocess environment; the publish credential acquired later is also checked against prepared content before it is delivered to a publish step. Success emits `PreparedPublication` and `awaiting_authorization` from the supervisor. `prepared_digest` binds the native prepared data and its metadata; `destination_digest` binds provider identity, trusted remote, repository, branch, base and transport settings, not just candidate display strings. `operation_id` is deterministic per attempt/publication output. The existing per-attempt publication plan produces the workspace revision; declared outputs referring to that workspace use the same prepared operation rather than allowing different destinations under one identity.

**Inspection handoff to the scanner.** The provider-owned decoder runs as an inspect step in private publication storage, with no scan secret values in its environment. Its required `inspection` capture is an `InspectionManifest`: the exact native revision and prepared digest, a content-index location/digest, and `complete=true`. The content index is a bounded JSON-lines stream of `InspectionContent` records. Each record identifies an opaque native revision, a relative path (or no path for revision metadata), kind, immutable content location, digest and byte length. It enumerates every file/symlink version and revision metadata newly reachable from the trusted base through the prepared revision, including intermediate versions later removed. A non-Git system with snapshot-only publication enumerates that complete new snapshot and its metadata. Native decoding/reachability stays provider-owned; core reads only this common interchange.

The trusted scanner pulls raw bytes through the authenticated private transfer channel, with backpressure, checks index/content digests and sizes against the manifest and binds its verdict to both prepared and inspection digests. It does not run the decoder with secret values or scan a display diff. Provider configuration supplies finite index/count/content-byte and inspect-time bounds, checked at startup; exceeding them is failure, not truncation. Malformed records, missing blobs, changed digests, unsupported kinds, a missing terminal completeness assertion or an incomplete enumeration fail `artifact_invalid` before authorization. Transfer unavailability is infrastructure failure, never successful inspection. Provider conformance verifies native reachability/completeness against real published history, not just a boolean assertion. The checkpoint retains this immutable index/content and verdict for recovery and for checking a newly acquired publication secret before delivering it to the publishing process.

**Finalize.** Publication steps execute only against the prepared revision and trusted destination, with a late credential supplied through section 2's authorization. No inspect/publish step may have `cwd=candidate`; only export and initial checkout may execute there. The authorization is kept out of argv, URLs, journal and persisted plan files; an ephemeral private helper or descriptor passes it only to the publication process. Output is redacted. The resulting `VcsRef` is assembled from trusted request/base, the prepared revision and verified remote result, not mutable workspace captures. `open_change_request` adds `url`, `commit_url` and `compare_url`; this remains a code-host API call made by the worker and idempotent on head/base by search before create. Its dispatch participates in the worker gate and must settle or be authoritatively fenced before that dispatcher acknowledges closure; a successful runtime retirement alone cannot settle it. Issuance backend design remains #43.

**Placement.** Both protocols declare `plan_executors`; prefer `runner`, otherwise `worker_host`. No common executor fails validation with `IR009` and fails execution with `config_error`. The names describe trusted executor placement, not permission to run a publisher inside the candidate domain.

| executor | preparation | publication | capability requirement |
|---|---|---|---|
| `runner` | supervisor in the runtime's execution infrastructure | separate trusted publication environment coordinated by `Runtime.finalize` | installed plan tools and code-host connectivity in the corresponding trusted phases; protected authority when required |
| `worker_host` | common host executor before candidate launch where `host_path` is exposed | runtime adapter transfers sealed bytes to a private worker-host publisher; same `finalize` contract | host client/tools and durable private checkpoint; no credentialed access to the candidate root |

Git declares both placements; local declares both, Docker and the configured fake runtime declare `runner` in MVP. An independent provider may expose no host path and still support complete preparation, sealed transfer, inspection and publication. The VCS sees no runtime name. The runtime adapter coordinates the common executor; it does not implement Git or another VCS.

**Git reference behaviour.** Provider `remotes` map logical repositories to trusted transport settings. Checkout uses a new repository and an explicit requested revision/branch; helpers and an empty hooks directory are private control files, never `.kora/plan/`. The agent may commit locally; kora performs publication. A candidate-side export produces a Git object bundle for the selected HEAD plus a raw selected worktree manifest: all tracked paths and untracked non-ignored paths, excluding candidate `.kora/` and `.git/`. Submodule gitlinks are data; no submodule command is executed. LFS publication is rejected as unsupported in MVP rather than executing a candidate filter or silently omitting required objects.

After sealing, the publisher imports validated, size-bounded objects into a fresh bare repository using strict object checks, with no inherited Git config/environment, alternates, object directories, hooks, fsmonitor, external diff, textconv, credential helper or candidate attributes. Validate that the selected HEAD descends from the recorded checkout commit and that the trusted base is present; never fetch candidate-selected prerequisites. Imported existing commit objects are retained unchanged. A separate trusted index uses raw bytes (`hash-object --no-filters` and index entries), modes and symlink blobs from the manifest to create the final tree, without `git add` or checkout in the candidate repository. If the final tree differs from HEAD, prepare exactly one additional commit with that HEAD as parent and message `kora: <node_path> #<n>`; persist its complete bytes and object id before inspection. Clean HEAD needs no extra commit. The trusted commit identity for that additional commit comes from provider configuration; existing candidate author fields are preserved and do not substitute for the authenticated publishing principal.

Checks read raw blobs and newly reachable commits between the trusted base and prepared HEAD, including intermediate content; diff is presentation only. `git archive` is not an exact raw-tree transfer because repository export attributes can omit or transform files. Push the explicit prepared object id to `refs/heads/kora/<graph>/<run_id>/<node_path>/<n>` at the configured remote, with create-only compare-and-set semantics. A candidate branch name or remote cannot redirect it. Remote equality with the prepared object is success; another value is `publication_conflict` (`artifact_invalid`) and is never force-overwritten. Candidate commits or explicitly granted code-host credentials cannot redirect this platform operation, but candidate code given its own publication credential can make independent side effects (FR-6.1b).

**Idempotency and failure.** Store the `PreparedPublication` and native bytes before accepting late authorization. Concurrent `finalize` calls for an operation serialize; identical repetitions return one `VcsRef`, different bindings fail. After a lost response, query the trusted destination with safe provider configuration: the same revision is success, authoritative absence permits retry of the same prepared revision only after earlier dispatchers and in-flight calls are closed and the attempt is not retired, unexpected content is conflict, and unreachable is still unknown (`infrastructure`, retryable). Do not recreate commits, repeat collection against mutable candidate files, or claim success from a missing response. Record each reconciliation as `vcs.publish_repeated`. Lost sealed/prepared state fails `infrastructure` and leaves any existing remote branch discoverable under its deterministic name; another attempt has another branch and never adopts it. There is no database outbox: the persisted attempt and the runtime's private phase checkpoint retain the operation identity. Lease/result fencing still governs API uploads; it cannot revoke an external push already in flight. A point-in-time absence while an older call remains possible is not a settled outcome. Unknown kora publication retains the execution-recovery barrier and reservations; after retirement only recorded results can be returned, with no new push or change-request creation (ADR-0017).

## 5. Approval channel (FR-7.1, FR-7.2a)

```python
class ApprovalChannel(Protocol):
    name: str
    def capabilities(self) -> ApprovalCapabilities: ...   # push_notifications, carries_identity, observes_head_sha
    async def request(self, req: ApprovalRequestDto) -> str: ...
        # req: approval_request_id (idempotency key), form schema, context artifact (ReviewBundle facts and narrative apart), bound commit SHA and digests
    async def poll(self, channel_request_id: str) -> ApprovalResponseDto | None: ...
        # response carries: payload, approver login and permission level, is_author, is_bot, timestamp, observed head SHA and digests (DD-35)
    async def feedback(self, channel_request_id: str) -> list[FeedbackComment]: ...
        # review comments with author, association and body; the vcs channel returns collaborators with write access by default (`feedback_from`)
    async def notify(self, channel_request_id: str, event: Literal["invalidated", "expired", "cancelled"]) -> None: ...
    async def cancel(self, channel_request_id: str) -> None: ...
```

Built-in: `cli` (the API itself is the channel; `poll` reads the row) and `vcs` (publishes the `ReviewBundle` as a change-request comment with facts and narrative apart, maps review states to decisions, returns the head SHA and the approver's login and permission level with every response). The channel never decides validity: the scheduler compares SHA and digests and applies the DD-35 rules (approver is not the author and not kora's bot; on the namespace's `approver_allowlist` when set; "merged" counts only when the channel configuration sets `accept_merged_as_approval`).

A push to the candidate branch during review is not the channel's business either: the scheduler observes the head of the bound `VcsRef`'s branch through the VCS protocol's `head_sha`, moves the request to `invalidated` and fails the node with class `candidate_moved` (scheduler.md section 5); the channel learns of it through `notify("invalidated")`, and a response it reports afterwards is recorded and ignored.

## 6. Secrets (FR-10.1)

```python
class SecretsProvider(Protocol):
    name: str
    config_schema: dict             # includes `namespace_prefixes`: namespace -> allowed reference prefixes
    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue: ...   # secret://<provider>/<name>; refuses references outside ctx.namespace's prefixes
    async def list_names(self, prefix: str) -> list[str]: ...                           # for validation of aliases, never values
```

Secret references are **bound to the namespace** (design review finding [scoped tokens and secrets](../reviews/review-2026-10-15.md#scoped-tokens-and-secrets)): the provider configuration maps each namespace to the prefixes it may reference; `kora validate` enforces it (`IR007`) and `resolve` enforces it again at submission time. Resolution happens inside the runtime adapter at submission time, after the nonsecret request has been durably sealed (ADR-0018); the saved request contains references only and current namespace authorization is checked again. Credential injection is outside semantic hashing and never serialized back into history; `SecretValue` registers itself with the redaction filter that every captured stream, error detail and VCS result passes through (FR-10.2). Built-in: `env`, `file`.

## 7. Artifact storage (FR-4.4)

```python
class ArtifactStorage(Protocol):
    name: str
    async def put(self, payload: bytes | AsyncIterator[bytes], digest: str, content_type: str) -> StorageRef: ...
    async def get(self, ref: StorageRef) -> AsyncIterator[bytes]: ...
    async def exists(self, digest: str) -> StorageRef | None: ...      # content-addressed dedupe
    async def delete(self, ref: StorageRef) -> None: ...                # idempotent; called only by the retention task
```

Built-in, both in MVP (FR-4.4, ADR-0005): `local` for standalone and development (directory tree `artifacts/sha256/ab/cd/<hex>`, `StorageRef = local://sha256/<hex>`, DD-36) and `s3` for docker-compose and organisation deployments, against any S3-compatible service through the `obstore` library (ADR-0013; objects under `<prefix>/sha256/ab/cd/<hex>` in one configured bucket, `StorageRef = s3://sha256/<hex>` resolved against the configured bucket and prefix so that references survive a bucket move; GCS and Azure are reachable through the same library without a new implementation). `s3` exists in MVP because session segments at the target scale (about 6 to 20 GB a day) do not fit local disk ([load estimate](../analysis/control-plane-load-estimate.md)). Workers never reach the store: they upload through the API, and the API always proxies downloads; presigned URLs were dropped from v1 (DD-37).

## 8. Artifact type (FR-4.1–4.3)

```python
class ArtifactType(Protocol):
    name: str                       # "VcsRef" or "namespace/Name"
    parent: str | None
    schema: dict                    # JSON Schema of the payload
    def validate(self, payload: dict) -> list[ValidationError]: ...
    def digest_fields(self, payload: dict) -> list[str]: ...     # which fields participate in gate digests (FR-7.2a)
    def summary(self, payload: dict) -> dict: ...                # small projection for branch expressions and the API list view
```

Built-ins ship as immutable schema/behavior revisions, expanded into the deployed contract (ADR-0018). A bound `ArtifactType` instance takes schema/parent and exact behavior identities from the saved graph-version contract, not the current registry alias. Custom types are registered through the API (`PUT /artifact-types`) as schema, parent and declarative behavior descriptors, with an optional trusted installed entry-point binding for custom `summary`. Registry writes update the authoring alias, never any deployed closure. Registration checks JSON Schema draft 2020-12 and the supported closed-reference rules in [ir.md](ir.md) section 2.1; deployment uses embedded definitions, not replacement aliases.

The generic `validation` identity is `kora.json-schema-2020-12.v1`; `kora.json-payload.v1` selects the whole canonical payload for digesting and returns the payload as the generic identity summary. A custom identity pins provider-declared semantics and the trusted installation's immutable implementation binding. It is not executable persisted data or an automatic package download. A reused identity with mismatched implementation metadata is incompatible; required semantics must be supported before the affected validation/execution operation. Compatible upgrades retain bindings required by active runs.

At production/upload, store the applied contract digest, validation outcome, actual digest-field selection and returned summary/projection. User-content diagnostics and projection values follow payload retention even if small/inline; content-free outcome/selection/digests remain metadata. Built-in content-bound approval projections are computed once under the saved contract and reused, never recomputed with a replacement plugin. Historical reads/diffs load these saved results and explicit availability; they do not call today's `summary`, `digest_fields` or validator. Branches, fan-out collection and approvals share these contracts/results despite having no physical runtime attempt. Required expired payloads or unavailable behavior cause explicit failure, not current-value fallback.

## 9. Observability exporter (FR-11.3, NFR-9.2)

```python
class ObservabilityExporter(Protocol):
    name: str
    config_schema: dict
    async def on_event(self, event: EventRow, ctx: ExporterContext) -> None: ...   # every row of the append-only event table, in order
    async def flush(self) -> None: ...
```

One event concept (design review finding [one event concept for exporters](../reviews/review-2026-10-15.md#one-event-concept)): exporters are loaded in the control plane only and consume the `event` table, so an export can be **replayed** from any point by re-reading rows; the scheduler feeds them through a bounded queue with drop-with-metric and an exporter never blocks a transition. Session facts reach the table because the API promotes them on upload: `vcs.*` from the worker's publish results and `permission_denied` from the runner's segments become rows. The built-in `otel` exporter follows the GenAI semantic conventions (design review finding [OpenTelemetry GenAI mapping](../reviews/review-2026-10-15.md#opentelemetry-genai-mapping)): a run is an `invoke_workflow` span with `gen_ai.workflow.name = <graph id>`; on `attempt.finishing` it reads the attempt's session segments through `ctx.segments(attempt_id)` and builds the `invoke_agent` span, `CLIENT` kind when the harness runs remotely or managed, `INTERNAL` otherwise, with `gen_ai.usage.*` from the usage records; `gen_ai.conversation.id` is the harness session id, so kora spans join with Claude Code's own OpenTelemetry output on `session.id`. Langfuse and Datadog exporters follow the same interface.

## 10. Authentication (FR-12.4, NFR-4.4)

```python
class Authenticator(Protocol):
    name: str
    config_schema: dict
    async def authenticate(self, request: RequestInfo) -> Principal: ...
        # kind (user | service), owner, namespaces [{namespace_id, transcripts}], approve, platform, and for service: runtime
    async def revoke(self, token_id: str) -> None: ...
```

Two principal kinds (design review finding [scoped tokens and secrets](../reviews/review-2026-10-15.md#scoped-tokens-and-secrets), DD-32): a **user** principal is a personal token bound to a person and a namespace list, with exactly two flags, `approve` (may respond to approvals) and `platform` (may publish presets, namespace policies and tool policies), and a per-namespace `transcripts` capability for reading transcripts and session segments (design review finding [transcript access, audit and supply chain](../reviews/review-2026-10-15.md#transcripts-audit-supply-chain)); no roles or groups. A **service** principal belongs to a worker, is bound to a namespace list and may only claim attempts of its runtime in those namespaces and heartbeat, upload and transition attempts whose lease it holds (checked by lease epoch). Every by-id route joins on `namespace_id` against the principal's list (design review finding [namespace-scoped principals](../reviews/review-2026-10-15.md#namespace-scoped-principals)). Built-in: `static_token` (MVP). OIDC implements the same `Principal` so routes do not change. There are no tokens for agent nodes: nodes never call kora (DD-5).

## 11. Sensor (FR-2.3, FR-6.2, DD-2)

A sensor observes an external condition on behalf of a `wait_event` node or a sensor trigger. It is polled by the scheduler at the configured interval; it holds no worker and keeps no state between polls beyond what it returns.

```python
@dataclass(frozen=True)
class SensorCapabilities:
    supports_push: bool             # can also deliver observations as inbound events (webhook), not only by poll
    min_interval: timedelta         # the scheduler refuses shorter intervals; protects code hosts from tight polling (DD-37)
    needs_vcs: bool                 # built-ins change_request_status and ci_status

class Sensor(Protocol):
    name: str
    config_schema: dict             # JSON Schema for `poll.config`; validated at kora validate (IR003-style error with YAML path)
    def capabilities(self) -> SensorCapabilities: ...
    async def observe(self, config: dict, inputs: dict, ctx: ProtocolContext) -> Observation: ...
        # returns a JSON object exposed to the `until` CEL expression as `observation`; sensors are stateless between polls
```

Built-ins: `change_request_status` and `ci_status` (through the VCS protocol; observation is the `ChangeRequestStatus` or `CiStatus` DTO), `http` (GET a URL with optional headers from secret references; observation is `{status, body}`; the URL must match the namespace's `http_allowlist` and link-local and cloud-metadata addresses are always refused, design review finding [webhook and sensor hardening](../reviews/review-2026-10-15.md#webhook-and-sensor-hardening)). Adopters register sensors through the `kora.provider` entry point and reference them by name in `poll.sensor`.

## 12. Policy (DD-29)

The policy protocol is the **decision point for tool calls**. The session runner invokes it from the harness's `PreToolUse` hook before every tool call the harness routes through that hook; a deny stops the call and travels as a `permission_denied` session event that lands on the `AgentSession` artifact and is promoted to an event row on upload (design review finding [one tool-policy field](../reviews/review-2026-10-15.md#one-tool-policy-field)). Approval requests are not the decision point: the Codex app-server raises `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` only when the agent needs to leave its sandbox, never for a command or an edit inside `workspace-write`, and `item/permissions/requestApproval` belongs to its `request_permissions` tool ([harness policy hooks](../research/harness-policy-hooks.md), checked 2026-10-16; [architecture critique of 2026-10-16, finding A01](../reviews/architecture-critique-2026-10-16.md#a01-policy-boundary)).

**Mechanism per adapter.** `claude`: the Agent SDK `PreToolUse` callback registered without a matcher; the harness runs it before every tool call in every permission mode, and deny decisions hold under `bypassPermissions`. `codex`: a `PreToolUse` command hook, `kora-runner policy-hook`, that reads the hook input from stdin, obtains the decision from the runner and exits 0 to allow or 2 to deny with the reason on stderr; the runner declares it as a managed hook in the attempt's `requirements.toml` with `allow_managed_hooks_only = true`, so the hook is trusted by policy and the repository's own hooks never load; `approval_policy` stays `never` and plays no part in the policy. `acp` (after MVP): `session/request_permission`, which an agent may or may not raise; that adapter declares its coverage per agent and ships only once verified.

**Coverage matrix.** Each adapter declares in `HarnessCapabilities.policy_coverage` (section 3) which call kinds its hook intercepts, and `check_config` and `kora validate` compare every `tool_policy` pattern with it (`IR008`, `IR019`, [ir.md](ir.md) section 10). For the MVP adapters, under every kora `permission_mode` and `sandbox` value, as the Claude Code and Codex documentation and the Codex sources stated on 2026-10-16:

| call kind | pattern examples | `claude` | `codex` |
|---|---|---|---|
| shell command | `Bash`, `Bash(etl *)` | intercepted (`Bash`) | intercepted (`Bash`) |
| file edit | `Edit`, `Write` | intercepted (`Edit`, `Write`, `NotebookEdit`) | intercepted (`apply_patch`; the hook matcher accepts `Edit` and `Write` as aliases) |
| MCP tool call | `mcp__<server>__<tool>` | intercepted | intercepted |
| tool call inside a subagent | any pattern | intercepted; the hook input carries `agent_id` | intercepted; subagent hooks carry the parent session id |
| web and other hosted tools | `WebSearch`, `WebFetch` | intercepted (built-in tools) | **not interceptable**: Codex web search is a hosted tool outside its hooks; the adapter sets `web_search = disabled` in the attempt configuration and the runtime network policy is the layer that holds |
| harness-internal actions (project instructions, skills, memory) | none | not a tool call; governed by `trust_repo_config` through `setting_sources` | not a tool call; governed by `trust_repo_config` through `project_doc_max_bytes` and `allow_managed_hooks_only` |

**Three layers.** The hook is one of three layers and the only one that sees tool names and arguments: (1) runtime isolation bounds the process regardless of what the agent calls (filesystem, network policy, credentials, process rights; `RuntimeCapabilities.enforces_network_policy`, DD-21); (2) the permissions of external services bound what a call can do (the rights of the tokens a session holds, the capabilities an MCP server exposes, publication through the worker rather than through the session); (3) the harness hook denies a call before it runs. A policy is the set of rights these layers enforce; a tool-name or argv check does not prove the absence of a forbidden effect inside an allowed program, so the shell-chaining rule below complements the first two layers and never replaces them.

**Fail closed.** A `tool_policy` pattern that names an uncovered call kind is error `IR019` when it comes from the namespace policy, because the platform team's rule would otherwise run unenforced, and warning `IR008` when it comes from the node alone.

```python
@dataclass(frozen=True)
class ToolCallRequest:
    harness: str
    tool: str                       # harness tool name, e.g. "Bash", "Edit", "mcp__yt__query"
    arguments: dict
    workspace: WorkspaceRef
    node_path: str

@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reason: str

class Policy(Protocol):
    name: str
    config_schema: dict             # schema of the `tool_policy` object nodes carry
    def validate(self, policy: dict) -> list[ValidationError]: ...        # kora validate, IR013
    async def decide(self, request: ToolCallRequest, policy: ToolPolicy, protected_paths: list[str], ctx: ProtocolContext) -> PolicyDecision: ...
```

One policy applies to every call (design review finding [one tool-policy field](../reviews/review-2026-10-15.md#one-tool-policy-field)): the node's `tool_policy` with the namespace's `tool_policy` (set by the platform team, DD-32 `platform` flag) **prepended**, so a namespace deny wins. The MVP implementation `allow_deny` matches ordered allow and deny patterns in the harness's tool-pattern syntax; at argv level it rejects shell chaining metacharacters and `-c` or `--exec` style indirection inside an allow-listed command (design review finding [tool deny coverage](../reviews/review-2026-10-15.md#tool-deny-coverage)), and it denies `Edit` and `Write` on `protected_paths` (DD-34). Cedar and OPA arrive as plugins through the same interface without an IR change. The policy runs inside the runner, so the decision is local to the session and never requires the agent to reach kora.

## 13. Conformance suites (NFR-10.2)

Each protocol ships `kora.conformance.<protocol>` as pytest parametrised over the entry point under test (`kora conformance runtime inhouse_ci`). Outline:

- **Runtime**: idempotent submit and workload-digest conflict; authoritative absence versus unreachable lookup; original-domain adoption and protected journal offset; declared portable recovery state; constructed candidate environment; local deadlines and cancellation; one logical handle/capacity reservation through publication; late `finalize` only after inspection, binding checks and serialization; no candidate-visible publication credential; preparation crash recovery through the supervisor checkpoint; release removes bulky candidate/control/publication roots but retains the retirement/dispatcher registry. ADR-0017 adds the execution-recovery gates below. `test_workspace_preparation_recovers_after_worker_crash` uses the configured fake runtime with no shared host filesystem, crashes at phase barriers and recovers the same trusted `WorkspaceRef`. OS isolation is verified only on actual protected runtimes (table below).
- **Harness adapter**: recorded SDK event streams yield the expected normalised events, usage completeness, `max_events` termination at the exact count, structured output and its fallbacks, hermetic behaviour (a repo hook that must not run), policy hook invoked before every tool call, installed version recorded and checked against `tested_versions`.
- **VCS**: checkout captures the requested base and protected paths; all private files stay outside the candidate tree; native sealed transfer, fresh metadata, fixed inspected revision and trusted destination; no runtime name in requests; same prepared revision on repeat/concurrent finalization; change-request search before create. `test_publish_and_open_change_request_are_idempotent` repeats finalization and creation concurrently using the same operation/head/base and asserts one published revision and one change request. `test_publish_reconciles_after_response_loss` loses the push/create response through an injected provider implementing the real protocol, then compares the same remote revision and reuses the existing request; conflicting remote content fails without overwrite. `test_vcs_runtime_cross_product` runs git and a directory-backed independent non-Git VCS through preparation, execution, sealing, inspection, authorization, publication and release on local and Docker in Linux CI, and on local and the configured fake runtime in macOS CI. The fake runtime exposes no worker-host root. Adopters run the same contract on their runtime/VCS pair; unsupported executor/protection combinations fail validation, with the limitation in the report.
- **Approval channel**: request → `None` → response with identity → stale response reported with its observed SHA → cancel and notify idempotent.
- **Secrets**: resolution, missing-reference error shape, redaction in a captured stream.
- **Artifact storage**: content-addressed dedupe, streaming beyond memory, `get` of a reference written by an earlier instance of the implementation, `delete` after which `exists` returns nothing and `get` fails, and a repeated `delete` that succeeds. The suite runs against `local` on every CI job and against `s3` with an S3-compatible server started through `testcontainers` on the Linux job, which has Docker.
- **Artifact type**: validation errors carry JSON paths; `digest_fields` stable across key order.
- **Observability**: a slow exporter does not delay a run; drops are counted.
- **Authentication**: scope enforcement per operation; revoked tokens are refused.
- **Sensor**: `config_schema` rejects bad config with a path; `observe` is side-effect free when called twice; an `until` expression over the documented observation shape evaluates; `min_interval` is honoured by the scheduler.
- **Policy**: `validate` rejects a malformed policy with a path; a namespace deny beats a node allow; decisions are deterministic for identical requests; **a deny reaches every call kind the adapter declares in `policy_coverage`, under every permission mode**, through the adapter's `PreToolUse` hook (Claude: the SDK callback; Codex: the managed command hook; after MVP ACP `session/request_permission` for the kinds the agent declares), and the denied call never reaches the tool (design review finding [tool deny coverage](../reviews/review-2026-10-15.md#tool-deny-coverage)); a pattern on a kind outside the coverage is rejected by validation (`IR019`) or warned (`IR008`), never left unenforced; chaining metacharacters and `-c`/`--exec` indirection are denied inside allow-listed commands; a protected path cannot be edited. `test_required_policy_is_enforced_or_rejected` runs the matrix of section 12 against the real harness interface with a deterministic driver (recorded streams where the interface allows no driver): for every covered kind and permission mode an allowed and a denied shell command, file edit and MCP tool call, judged by whether the side effect happened, not by the presence of a `permission_denied` event; for every uncovered kind, validation of a node carrying a namespace pattern on it returns `IR019`. A live smoke test against a model is a manual experiment outside CI, recorded with the harness version; the fake adapter proves the runner's handling of a decision, never a real hook.
- **End to end**: the canonical graph on the fake harness against the runtime and VCS under test (NFR-0.2).

### Publication authority and recovery contracts

These are **future executable test contracts**, specified for issue #51; this repository has no application implementation. Documentation CI does not run them. Use actual local Git and processes, local bare repositories and in-process code-host providers, plus configured protocol fakes. No model calls, external code hosts, patched transports or mocks. Synchronize through explicit phase/process conditions with deadlines, and inject clock/randomness sources. Every case touching metadata storage is parametrized over PostgreSQL and SQLite; Docker/PostgreSQL container cases run on Linux CI only.

| test | setup and observable assertion |
|---|---|
| `test_candidate_cannot_forge_runner_journal` | On a protected runtime, candidate code tries replacement, truncation, parent-path swaps and symlinks to control journal/checkpoint files, then the worker restarts. Adoption reads the original monotonic journal and trusted base/phase; planted completion/reference records never become authoritative. |
| `test_candidate_git_configuration_cannot_execute_in_publisher` | A real candidate repository plants a clean/process filter, hook, fsmonitor, helper and external-diff/textconv configuration. Run full export and publication. Candidate-side export may execute candidate code without VCS credentials, but no planted program runs in publisher authority, accesses its private marker or obtains the publication secret. The publisher uses fresh config, raw object/blob inspection and a raw trusted index. |
| `test_agent_descendants_cannot_access_publication_credentials` | A real descendant detaches and survives the harness parent. A protected runtime terminates the entire domain before sealing; no process remains able to modify input or read the late secret. With termination unconfirmed, no authorization/push occurs and the attempt reports `publication_input_unavailable`. |
| `test_publication_destination_is_trusted` | Change candidate remotes, branch, config includes and symlink paths; request publication. The only push is the inspected object id to the provider-configured repository and deterministic attempt branch. Record base, operation id and input/prepared/destination digests. A mismatched authorization is rejected; a different remote head produces conflict, never overwrite. |
| `test_publication_preserves_candidate_commits` | Create two commits, executable modes, binary blobs and a symlink blob. Sealed import retains exact commit ids/parents and blob contents. Trusted publication does not invoke candidate attributes or execute a gitlink. A secret added then deleted in newly published history fails `secret_leak`; required unsupported LFS objects fail `artifact_invalid`. |
| `test_publication_records_uncommitted_changes_once` | Start with committed HEAD plus tracked edits/deletions and non-ignored untracked files. Preparation creates one additional commit with that HEAD as parent and the selected raw tree. Repeated and concurrent finalization, including after worker restart, returns that exact commit, without a second commit or new timestamp. |
| `test_publication_sealing_rejects_mutable_aliases` | Export contains a path swap, escaping symlink, external hard link or special file; reject unsafe input or copy safe data without following aliases. Modify the old candidate storage after sealing; prepared bytes/digest remain unchanged on protected runtimes. Legitimate symlinks remain VCS blob data. |
| `test_finalize_recovers_prepared_revision` | Crash at sealing, prepared-result persistence, authorization and accepted-push-before-response barriers. Adopt from private checkpoint and use identical prepared revision/destination. Remote absent permits the same publication, equal succeeds, different conflicts, unreachable stays unknown. Missing/corrupt prepared state fails infrastructure rather than regenerating content. A changed workload under the attempt id fails `workload_conflict`. |
| `test_non_git_inspection_stream_scans_intermediate_history` | The independent directory-backed VCS emits a native revision chain whose intermediate version contains a secret removed from the final snapshot. Its provider-owned decoder emits the common complete content index; the generic trusted scanner rejects with `secret_leak`, while the decoder sees no scan secrets. A truncated index, missing intermediate version, corrupt blob/digest or false completeness fails before authorization. The same scanner works on Git without native-format branches in core. |
| `test_publication_authorization_is_late` | A session outlives the provider's first credential. No publish credential/parent enters the workload, candidate environment or journal. The worker acquires a fresh grant after inspection and after adoption; expired/rejected authorization can be replaced only for the same prepared binding. Static-provider mode asserts no invented TTL/scope guarantee. The grant's secret value is checked against prepared content before delivery to publication subprocesses. Coordinate issuance assertions with #43. |
| `test_required_execution_protection_rejects_local` | Local declares `protected_execution=False` and runs trusted lifecycle/publication tests on Linux/macOS with no additional flag or administrator permission. Setting namespace `require_protected_execution` rejects it with `IR021`; bypassed validation still produces `execution_protection_unsupported` before submission. This test does not pretend that same-user local code is contained. |
| `test_harness_launcher_preserves_control_boundary` | Through each real harness interface's deterministic driver, prove launcher argv/env/cwd, SDK configuration, policy callback, interruption and nested tool/subagent launches cross the selected candidate boundary. No control directory/channel or parent credential becomes candidate authority. The fake adapter proves supervisor behaviour only; a manual model smoke experiment records harness version separately. |

| implementation | common lifecycle/publication suite | hostile-code authority evidence |
|---|---|---|
| local on Linux/macOS | required, including real Git, repeated finalization and adoption | unsupported by design; required-protection rejection must pass |
| Docker on Linux CI | required, including worker restart and private-volume handoff | real container/descendant, journal, alias and secret-boundary cases required; default container presence alone proves nothing |
| independent runtime/provider | required without a shared worker filesystem, with native non-Git revision/input data | provider's actual process/service isolation and transfer tests required before it declares true; otherwise required-protection rejection |
| configured fake runtime/VCS | required, deterministic phase/recovery barriers | protocol evidence only; cannot certify OS isolation |

### Execution-recovery implementation gates (ADR-0017)

These named cases specify future behavior; this documentation change implements no runtime or test suite. Run integration cases against both PostgreSQL and SQLite using the same parametrized fixtures, injected time/randomness and explicit condition barriers with deadlines. Use configured protocol fakes validated by the runtime/VCS conformance suites, real local processes/Git and an independently controllable local execution service; never patch modules, environment or HTTP transports and never use a real model/code host. Local process cases run on Linux and macOS without a sandbox/VM/administrator setup; Docker and containerized PostgreSQL cases run on Linux CI. Separate protocol correctness evidence from OS/provider termination evidence.

| future test | required observation |
|---|---|
| `test_healthy_parallel_admission_survives_recovery_contract` | Healthy live attempts and their current grants allow ordinary parallel admission; a first claim awaiting its initial observation is not by itself run-wide recovery. Lease expiry sets `recovery_required` and blocks new admissions until live adoption with earlier authority closed or complete retirement/closure; already-running work can finish. |
| `test_partitioned_live_host_is_not_assumed_stopped` | Pause API reachability after submission while a real local session continues; advance injected time beyond two lease periods. Attempt stays non-terminal/unknown with the same reservations and retry counts, no new submit; recovery adopts the same original-domain handle after reachability returns. |
| `test_submit_crash_recovers_in_original_execution_domain` | Pause between dispatch grant, runtime acceptance and handle persistence in turn. Wrong host/domain, missing handle and unreachable registry never permit replacement. A live handle is adopted; only original-domain retirement plus every dispatcher closure permits confirmed loss and policy retry. |
| `test_retired_attempt_cannot_start_after_delayed_submit` | Pause an old dispatcher before runtime submit, preparation and finalize in turn; install retirement first and then release it. No delayed new effect occurs. In the reverse order retirement stops/settles the managed operation or stays unknown. Duplicate completed operations return recorded results without effects. |
| `test_recovery_retains_every_dispatcher_generation` | Create three claims in different worker incarnations, leaving the first paused. Latest-holder and runtime closure do not erase its grant or clear the barrier. It closes its local gate before acknowledgement; a fresh claim relays the original-domain evidence without stale-epoch API writes. Adoption remains read-only while it is unresolved. After closure, recover a current non-retired attempt whose push completed before a worker crash preceding `open_change_request`; finish only the stored operation from its authoritative checkpoint, with no agent restart/new commit. Cancelled/superseded/retired cases remain read-only. |
| `test_runtime_stop_does_not_imply_publication_quiescence` | Obtain runtime quiescence while a worker-host publisher/code-host call remains pending. Remote point-in-time absence and a new API epoch do not close it. No replacement/fresh publication occurs until authoritative provider or operator evidence settles all old authority. Use the independently controllable local service to delay acceptance and response separately. |
| `test_execution_replacement_requires_declared_authority` | Before affirmative original-domain runtime quiescence and every dispatcher/publication closure, replacement stays blocked. Complete evidence permits ordinary infrastructure retry and exactly-once reservation release; cancellation never retries. |
| `test_runtime_unreachable_is_not_confirmed_absence` | Original-domain lookup/recovery while unreachable or with missing registry stays unknown; empty lookup in another domain cannot certify absence. A found live original execution is adopted under the same id. |
| `test_reconnected_old_execution_cannot_publish_current_result` | Reconnect an expired/cancelled/superseded worker: stale epochs fail; a fresh recovery-only lease preserves historical diagnostics/usage but grants no submission/publication/current-output rights. Retirement blocks delayed effects; already-issued uncertain operations remain barriers until settled. |
| `test_operator_confirmed_domain_shutdown_unblocks_recovery` | After actual decommissioning and closure of every dispatcher/publication authority, scoped operator attestation records actor/evidence as human provenance and settles only applicable reservations/holds. A pending unreachable observe or stop cannot prevent versioned evidenced confirmation; delayed observations cannot reopen execution or overwrite newer evidence. |
| `test_registry_loss_cannot_certify_old_domain_absence` | Replace a host/registry under the same stable worker name and register a new incarnation/domain. Empty lookup in the new registry cannot retire the old domain, release reservations or restart its attempt. |
| `test_uncertain_execution_retains_reservations_after_cancel` | Cancel the run and clear a running node during a partition. Result authority ends immediately; old attempts retain every original scope and acquired target hold, all new admissions in that run wait, already-running independent work can finish, unrelated runs follow ordinary limits/target policy. Fresh recovery-only claims work after terminal run status, report historical usage and stop execution without submission/publication rights. Closure releases each reservation once and releases the target only if the ordinary run-release condition also holds. |
| `test_clear_downstream_waits_for_execution_retirement` | Manual retry records clear/cancel intent but starts nothing until all old barriers clear. Linked rerun is `API002` for every strategy/version/parameter variant while any barrier remains; a separately triggered run obeys normal target policy. Confirmed infrastructure loss follows its retry budget; cancellation does not retry; the replacement has a distinct publication branch. |
| `test_execution_reconciliation_is_audited_and_fenced` | Namespace or `platform` failure is `API005`; stale version/domain is `API002`; missing reason/evidence or incomplete closure attestation is `API003`. Identical idempotent replay returns its original result, changed payload is `API004`; concurrent valid requests apply once. `observe`/`stop` only persist intent and run no external calls under database locks; human evidence is visibly attributed, including for a terminal run. |
| `test_recovery_fences_survive_release` | After retirement and cleanup, replay the delayed submit/finalize and repeat retirement. No new effect, same receipt; registry loss yields unknown. Test real configured runtimes as well as the fake; a fake alone proves no process-isolation claim. |
| `test_uncertain_usage_and_recovery_surface_parity` | Read attempt/run/target/events/metrics after an observation gap: original domain, version, reason, last observation/time, all grants, retained scopes and permitted actions agree; cost is last-known/partial, never zero/complete. Later usage updates historical totals only. The reconcile API maps through shared-client CLI and `kora_admin`; curated tools remain fourteen and console mutations remain four. |

### Immutable execution-history contracts

[Data-model section 11.4](data-model.md#114-future-acceptance-contracts) defines the future integration/conformance matrix for saved inputs, schema closure, policy changes, request/recovery identity, retention and secret/transcript access. Its crash cases include sealing response loss, workspace preparation, runtime acceptance before handle persistence, changed dispatcher authority and changed branch HEAD. They must preserve the original request and ADR-0017 barriers. These are future executable gates; existing documentation/schema CI does not execute runtimes.

## 14. Harness mapping tables (FR-3.3, DD-27)

Resolved node-config field → harness option. The Claude Agent SDK and Codex app-server columns are the MVP adapters; the **ACP column is the design for the adapter that ships after MVP** (ADR-0004) and is kept here so the vocabulary stays harness-neutral. "runner" means enforced by the session runner from the SDK event stream regardless of harness; "warn" means the field cannot be honoured and `check_config` emits `IR008`.

| config field | Claude Agent SDK (`ClaudeAgentOptions`, `query` / `ClaudeSDKClient`) | Codex app-server JSON-RPC through kora's client (`thread/start`, `turn/start`) | ACP, after MVP (`initialize`, `session/new`, `session/prompt`) |
|---|---|---|---|
| `prompt` | `query(prompt=…)` | `turn/start` input | `session/prompt` |
| `model` | `model` | thread config `model` | agent-specific session config; warn when the agent exposes none |
| `system_prompt_append` | `system_prompt={type: preset, preset: claude_code, append: …}` | `model_instructions_file` override (replace semantics; adapter prepends the Codex defaults) | warn; adapter prepends to the first prompt |
| `skills` | materialised skill directories through `add_dirs` with `setting_sources=[]` | `skills.config[]` config override (DD-20) | warn unless the agent reads skills from the workspace |
| `subagents` | `agents={…}` | warn | warn |
| `hooks` | `hooks={…}` Python callbacks | `[hooks]` in the attempt's managed `requirements.toml`, next to kora's policy hook, so they run under `allow_managed_hooks_only = true` while repository hooks do not | warn |
| `mcp_servers` | `mcp_servers={…}` (pinned digests verified by the runner) | `mcp_servers.<id>` config | `session/new mcpServers` |
| `tool_policy` (DD-29, design review finding [one tool-policy field](../reviews/review-2026-10-15.md#one-tool-policy-field); coverage matrix in section 12) | `allowed_tools`/`disallowed_tools` derived from the policy for the harness's own fast path, **and** the `PreToolUse` hook callback → policy protocol for every call, in every permission mode | the `PreToolUse` command hook `kora-runner policy-hook`, declared as a managed hook in the attempt's `requirements.toml` with `allow_managed_hooks_only = true` → policy protocol; independent of `approval_policy`; `web_search = disabled` because hosted web search is outside the hook | `session/request_permission` → policy protocol for the kinds the agent declares; warn on the rest |
| `permission_mode` | `permission_mode` (`plan`, `acceptEdits`, `dontAsk`, `bypassPermissions`) | `approval_policy` (`never` for `dont_ask`/`accept_edits`), `bypass` → `danger-full-access` | `session/set_mode` where the agent offers modes; else warn |
| `sandbox` | `sandbox=SandboxSettings(…)` | `sandbox_mode` (`read-only`, `workspace-write`, `danger-full-access`) | warn; runtime isolation only |
| `trust_repo_config: false` | `setting_sources=[]`, auto-memory disabled, per-attempt config dir | user config ignored, project untrusted, `project_doc_max_bytes=0` | hermetic workspace overlay; warn when the agent reads project config regardless |
| `trust_repo_config: true` | `setting_sources=[project]` | project trusted | agent default |
| `network_policy` | runtime (DD-21); additionally `sandbox.network.allowed_domains` | runtime; additionally `sandbox_workspace_write.network_access` | runtime only |
| `output_schema` | `output_format={type: json_schema, schema}` → `ResultMessage.structured_output` | `turn/start outputSchema` → final message | warn; the runner validates the final text as JSON, else `artifact_invalid` |
| installed version (recorded only, DD-14) | SDK `init` message | `initialize` response | `initialize` response `agentInfo` |
| `limits.stall_after` | runner, from message timestamps, extended during an open tool call | runner | runner |
| `limits.timeout` | scheduler decides the state; the runner also holds the deadline and performs `interrupt()`, 30 s grace, kill at it, even when the control plane is unreachable | `turn/interrupt`, grace, kill | `session/cancel`, grace, kill |
| `limits.max_events` | runner, counting `turn`, `tool_call` and `tool_result` events; `max_turns` is also set as an additional native limit | runner, counting `item/completed` notifications | runner, counting `session/update` tool calls and agent messages |
| `limits.budget_usd` | `max_budget_usd` **and** runner from per-model usage on the result | runner from `turn/completed.usage` × price table | runner from usage in `session/update` when present |
| usage source | `ResultMessage` per-model usage with cost basis (includes subagents) | `turn/completed.usage` summed per turn; `partial` if killed mid-turn | agent-dependent; `unavailable` when absent |
| session handle (`resume`) | `resume=<session id>` | `thread/resume` | `session/load` when the agent declares `loadSession` |
| `env` / `secret_env` | `env=` (the runner builds a minimal environment) | `shell_environment_policy` + constructed env | constructed env of the agent process |
| harness credential (DD-30) | `ANTHROPIC_API_KEY` in the constructed env; base URL to the proxy post-MVP | `CODEX_API_KEY` in the constructed env | agent-specific variable |
| billing class | auth-method probe on the init message | auth file inspection; default `api_key` | `subscription` unless the adapter config declares otherwise |
| stop reason / error class | `ResultMessage.subtype` (`success`, `error_max_budget_usd` → `budget_exhausted`, `error_during_execution` → `harness_error`) | `turn/failed`, `error` → `harness_error` | `stopReason` (`end_turn`, `max_tokens`, `cancelled`, `refusal` → `harness_error`) |

Fields with the same semantics on every harness are the kora vocabulary; fields that only one harness has are mapped on that harness and warned on the others, never silently dropped (FR-3.3).
