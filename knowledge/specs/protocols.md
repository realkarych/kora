# kora MVP design — the eleven protocols and the harness mappings

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: NFR-5.1–5.2, NFR-10.2, FR-2.3, FR-3.3, FR-5.2, FR-7.1, FR-8.1, FR-10.1, FR-11.3, FR-12.4. Interfaces are Python `typing.Protocol` classes; DTOs are frozen dataclasses or Pydantic models with JSON-only fields (NFR-4.3). Signatures are illustrative; names are the contract. Owner decisions on design choices are referenced as **DD-n** ("design decision n") and recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md).

There are eleven protocols: the nine of the original requirements plus **sensor** (DD-2, section 11) and **policy** (DD-29, section 12). Harness versions are fixed per installation (DD-14); each adapter declares the harness version range it was tested with, and a worker refuses to start (and `kora doctor` reports) when the installed harness is outside it (current contract [ADR-0004](../../docs/decisions/0004-harness-adapters-over-vendor-sdks.md)). Nodes never pin a version.

## 1. Common rules

- Every protocol has a `name` class attribute, a `capabilities()` method returning a frozen dataclass of flags, a `config_schema` (JSON Schema for its deployment configuration, validated at start-up and shown by `kora plugins list`), and conformance cases marked `kora_conformance` (section 13).
- Implementations are discovered through **one entry-point group**, `kora.provider` (NFR-5.2, current contract [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md) as amended 2026-10-21). An entry point is named `<protocol>.<name>` and returns a factory of validated `ProviderMetadata(protocol, name, implementation, config_schema, version)` whose protocol and name equal the entry-point name ([provider loading](#provider-loading-issue-150)); `kora plugins list` prints what is installed per protocol; names are then used in the IR (`runtime.runtime: docker`) or in the deployment config:

```toml
[project.entry-points."kora.provider"]
"runtime.inhouse_ci" = "kora_inhouse:runtime_provider"  # returns ProviderMetadata(protocol=ProtocolName.RUNTIME, name="inhouse_ci", …)
"vcs.inhouse_vcs"    = "kora_inhouse:vcs_provider"
```

- I/O methods are `async` and cancellable; every failure is a `KoraProtocolError(code, message, retryable, details)`, and `retryable=True` maps to error class `infrastructure`. Observation/delivery failures use scheduler-owned integration backoff, not runtime/agent-attempt retries ([scheduler section 16](scheduler.md#16-external-observation-dispatch-adr-0021)).
- Implementations receive a `ProtocolContext` (namespace id, run id, attempt id, logger, secrets resolver, secret admission, request digest, deadline, clock and activity reporter; issue #204) and never import the scheduler. Its `secret_admission` is an admission-only view, set only in a provider-resolution context: a provider can admit a value into the caller's registration scope and has no sanctioned path to release the scope or reach the process diagnostics; the view keeps trusted provider code honest, it does not confine hostile code in the same process. For managed execution effects it also binds the API-saved request identity and inherited execution deadline/clock assumption before any workspace allocation/preparation, including host execution (ADR-0022). It grants no authority beyond the authenticated current dispatcher gate.

The logger in `ProtocolContext` is already bound by the caller to authorized operation scope. Every implementation follows [structured diagnostics](observability.md), including its [eleven-provider matrix](observability.md#all-eleven-provider-protocols), without configuring root handlers. Diagnostic output has no execution or event-table authority. Shared conformance covers safe fields, failure ownership, context isolation and the applicable sink/channel obligations alongside each protocol's existing tests.

### Provider loading (issue #150)

**Entry-point names.** An entry point in `kora.provider` is named `<protocol>.<name>`. `<protocol>` is one of the eleven values of `ProtocolName`: `runtime`, `harness`, `vcs`, `approval_channel`, `secrets`, `artifact_storage`, `artifact_type`, `observability_exporter`, `authentication`, `sensor`, `policy`. `<name>` matches `[a-z][a-z0-9_]{0,62}`. Provider names repeat across protocols (`local` is a runtime and an artifact storage, `git` a VCS), so the name alone cannot select an entry point. The harness value `acp:<agent>` of the IR is split at the colon by the harness-binding parser, which loads `harness.acp`; the part after the colon never reaches the loader.

**Metadata.** `ProviderMetadata` is a frozen dataclass whose construction raises `ProviderMetadataError(rule)` for a broken rule:

| field | rule |
|---|---|
| `protocol` | a `ProtocolName` |
| `name` | the name rule above |
| `implementation` | callable; its signature is typed per protocol |
| `config_schema` | a JSON object with `"type": "object"`, string keys, JSON scalars, finite floats, nesting depth at most 16 and at most 1 MiB of canonical JSON (issue #201), stored as a deep copy; validating a deployment configuration against it belongs to the composition roots that read configuration |
| `version` | 1 to 64 printable ASCII characters |

**Loading one provider.** `load_provider(protocol, name, diagnostics)` checks, in order:

1. the requested name against the name rule;
2. the reserved name `fake`, in every protocol;
3. the entries named exactly `<protocol>.<name>`, without importing anything;
4. ambiguity: entries from two distributions, or two copies of one distribution with a different value or version, are ambiguous and nothing is imported; an identical copy counts as one;
5. the entry's value, which must parse as `module:attribute`, and its module against the refused set: `kora.plugins.<family>.fake` for any single segment `<family>`, `kora.testing` and `kora.conformance`, each with its submodules, matched component-wise; nothing is imported;
6. the import and the factory call: the result must be callable and its zero-argument call must return a `ProviderMetadata`;
7. the metadata's protocol and name against the entry-point name;
8. the implementation's `__module__`, and that of the callable a `functools.partial` wraps, against the refused set. This is a best-effort check on the implementation object, consistent with "Not an isolation control" below.

The production source iterates `importlib.metadata.distributions()` and their entry points itself, because `entry_points()` keeps only the first distribution of each normalized name and would hide a second copy. It reads each distribution's `entry_points.txt` itself, reads the distribution's metadata only when it has a `kora.provider` entry point, and constructs an `EntryPoint` only for a value that parses, because newer Python versions refuse a malformed value while listing a distribution's entry points. One malformed entry point therefore fails only itself. The factory must return exactly a `ProviderMetadata`, not a subclass, so construction always runs the rules above. kora's own fakes are constructed by injection only, never discovered (issue #136); their conformance runs by injection, never as `kora conformance … fake`.

**Failures.** Every failure is a `KoraProtocolError` with `retryable=False` and class `config_error`:

| code | cause |
|---|---|
| `provider_name_invalid` | a requested or installed name breaks the rule; the details carry only its length |
| `provider_reserved` | the name `fake`, or a module in the refused set |
| `provider_not_installed` | no entry point has the name; an unprefixed entry point of the same name adds a hint naming the `<protocol>.<name>` form |
| `provider_ambiguous` | step 4 |
| `provider_load_failed` | the import, the factory call or a read of the implementation's module raised; the details carry the qualified exception type name, a dotted identifier of at most 128 characters, falling back to the nearest base class that has one |
| `provider_metadata_invalid` | an entry-point value that does not parse, a non-callable entry point, a result that is not exactly `ProviderMetadata`, or a `ProviderMetadataError` from one of the rules above; the details carry the rule identifier, and a `ProviderMetadataError` carrying any other text is reported as `provider_load_failed` |
| `provider_metadata_mismatch` | step 7 |

A `KoraProtocolError` has a bounded identifier as `code`, content-free text as `message` and a JSON object of safe fields as `details`. A loading failure's details carry the protocol and, when it passed the rule, the name; never a distribution name, the entry-point value, a module path or the provider's exception message. The error is raised outside the handler, so neither `__cause__` nor `__context__` holds the provider's exception.

**Listing.** `discover_providers(diagnostics)` loads every entry point and returns what loaded together with one failure for each entry that did not, including malformed or unprefixed names. Only listing commands and conformance sessions call it; `runner`, `executor` and `worker` call only `load_provider`, and `kora conformance <protocol> <name>` maps onto `load_provider`.

**When loading runs.** Loading is synchronous and runs at process composition or start-up, never on the request, scheduling, dispatch or event path, inside a transaction or under a lock. IR and API validation check provider names against the metadata the composition root loaded at start-up and build the same `provider_not_installed` error from that set. The composition root holds the loaded metadata for the process lifetime. Loading owns no closable resource and adds no cache; a failed load is non-retryable. Clients, threads and tasks begin only when a composition root calls the implementation, and that caller closes them.

**Not an isolation control.** The loader imports only the requested entry point's module and its parent packages. That keeps a process from importing providers it did not ask for; it does not contain a hostile provider, and the metadata check runs after the import and its side effects (ADR-0026, TM-12).

**Diagnostics and settings order.** Loading reports under the component `protocols.loading` ([observability](observability.md#7-component-coverage-and-activation)). That component is registered when `kora.protocols.loading` is imported, so a composition root that loads providers imports that module before it reads `KORA_LOG_COMPONENT_LEVELS`; otherwise an override for the component is refused as unregistered.

**Pending.** `test_runner_process_import_footprint` lands with the runner work item: start the real runner entry point under `python -I`, load its configured providers with `load_provider`, and assert that `sys.modules` holds no module of the ADR-0026 server set and none of its checked server libraries.

### Control-plane observation lifecycle (ADR-0021)

Control-plane providers are installed trusted cooperative code. Import, initialization, capabilities/configuration hooks and synchronous decoding must be bounded; I/O uses nonblocking clients and propagates cancellation. No provider hook, secret resolution or external storage/network call occurs in a database transaction or under run/scope locks. Capabilities/contracts needed there are previously validated immutable local data. This contract covers conforming slow-service failures; it supplies no hard isolation from blocking or cancellation-resistant Python, including standalone. Conformance checks representative behavior, not arbitrary code safety.

Deployment bindings identify a stable trusted endpoint/account/resource, installed implementation/build identity and configuration revision, credential references, namespace permissions and finite limits. Alias equality is not implementation or resource equality; aliases sharing a backend quota share its resource key. Missing/incompatible saved semantics fail explicitly rather than loading today's alias. A call uses authorized current credentials for its original resource, never a changed alias destination.

A bounded composite invocation declares its full external dependency-resource set before dispatch, including nested VCS, external secret and external artifact-storage backends. Local environment/file providers have bounded cooperative I/O but no remote backend quota. Acquire invocation/namespace/resource credits together before task creation. Nested calls, redirects and pagination are serial: at most one outbound request is active per invocation, with no hidden parallel calls, nested credit acquisition or undeclared backend. Credits bound local conforming work, not remotely accepted effects after timeout. HTTP body/result limits apply to both received and decoded bytes; bound pagination, redirects, strings, collections, nesting and diagnostic output too. Reject excess rather than accepting a truncated value. Call/dependency work fits one finite deadline and returns one result/error; the dispatcher owns retries and shared rate-limit cooldown.

On deadline the caller rejects late evidence and initiates cancellation without blocking scheduling. Credits remain held until actual task settlement; grace expiry is degradation, not termination proof. Read-only repetition can produce stale duplicate observations, consumed only through exact current-owner token checks. Mutating operations instead retain durable authorization/intent and reconcile uncertain outcomes; neither cancellation nor process exit rolls back an accepted remote request. [Data model section 14](data-model.md#14-external-observation-and-delivery-ownership-adr-0021) defines those records.

## 2. Runtime (FR-8.1)

[ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md) defines execution uncertainty, durable retirement and dispatcher closure. [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md) separates the lifecycle supervisor from candidate execution and introduces late finalization. These interfaces are design contracts; no implementation or isolation experiment has run.

```python
@dataclass(frozen=True)
class RuntimeCapabilities:
    supports_resume: bool
    portable_handles: bool
    enforces_network_policy: bool
    isolates_tool_secrets: bool
    credential_proxy: bool
    protected_execution: bool
    plan_executors: frozenset[Literal["runner", "worker_host"]]
    max_concurrent: int | None
    default_stall_after: timedelta

@dataclass(frozen=True)
class ExecutionDeadline:
    at: datetime
    scope_kind: Literal["run", "subgraph", "attempt"]
    scope_id: str
    clock_uncertainty: timedelta
    grace: timedelta

@dataclass(frozen=True)
class Workload:
    attempt_id: str
    execution_domain_id: str
    dispatcher_id: str
    execution_request: JsonObject
    execution_request_digest: str
    kind: Literal["agent", "command"]
    resolved_config: JsonObject
    workspace: WorkspaceTarget
    checkout: CheckoutPlan | WorkspaceRef
    publish: PublishPlan | None
    plan_executor: Literal["runner", "worker_host"]
    env: dict[str, str]
    secret_refs: dict[str, str]
    harness_credentials: dict[str, str]
    artifact_inputs: dict[str, ArtifactRef]
    resources: tuple[ResourceRef, ...]   # command resources of the node (issue #45)
    limits: JsonObject
    deadline: ExecutionDeadline | None
    session_spec: SessionSpec | None
    dispatcher_process: ProcessIdentity | None   # the current grant's bound process (issue #123)

@dataclass(frozen=True)
class ProcessIdentity:
    host_id: str | None
    boot_id: str | None               # required on Linux, null on macOS
    pid_namespace: str | None         # required on Linux, null on macOS
    pid: int
    start_time: int
    leads_group: bool                 # outside the identity-equality rule

@dataclass(frozen=True)
class GrantBinding:
    dispatcher_id: str
    worker_id: str
    incarnation_id: str
    lease_epoch: int
    execution_domain_id: str
    process: ProcessIdentity | None

@dataclass(frozen=True)
class TransferItem:
    name: str                         # no `/`, `..` or control characters
    size: int
    sha256: str
    kind: Literal["resource", "input", "script"]   # `script` only for the testing runner of fakes.md section 3

@dataclass(frozen=True)
class PublicationAuthorization:
    attempt_id: str
    execution_domain_id: str
    dispatcher_id: str
    operation_id: str
    prepared_digest: str
    destination_digest: str
    principal: PrincipalRef
    credential: SecretValue | None

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
    evidence_ref: str                 # names the registry record, not a journal index
    scope: str                        # what was observed; the local scopes are below

@dataclass(frozen=True)
class NormalCompletionQuiescence:
    attempt_id: str
    execution_domain_id: str
    record_ref: str
    observed_at: datetime
    scope: str                        # what the record attests; the local scopes are below

@dataclass(frozen=True)
class DispatcherClosure:
    quiescence: DispatcherQuiescence
    provenance: Literal["registry_acknowledgement", "runtime_observed_process_exit"]
    process: ProcessIdentity | None   # the observed identity, set for runtime_observed_process_exit

@dataclass(frozen=True)
class ClosureEvidence:
    runtime: TerminationReceipt | NormalCompletionQuiescence | None
    dispatchers: tuple[DispatcherClosure, ...]
    publication: PublicationOutcome | None

@dataclass(frozen=True)
class AttemptError:
    code: str
    message: str | None
    details: JsonObject | None        # the whole model at most 16 KiB

@dataclass(frozen=True)
class TerminalReport:
    outcome: Literal["succeeded", "failed", "cancelled", "lost"]
    error_class: str | None           # set for failed; a worker-reportable class
    error: AttemptError | None        # set for failed
    lost_reason: Literal["execution_gone", "checkpoint_missing", "result_unrecoverable"] | None
    evidence: ClosureEvidence

@dataclass(frozen=True)
class RecoveryEvidence:
    attempt_id: str
    execution_domain_id: str
    handle: Handle | None
    retired: bool
    termination: TerminationReceipt | None
    closed_dispatchers: tuple[DispatcherClosure, ...]   # with provenance and, for process exit, the observed identity (issue #123)
    normal_completion: NormalCompletionQuiescence | None   # the recorded normal-completion record, also after the runner exited
    publication_outcome: PublicationOutcome
    observed_at: datetime

@dataclass(frozen=True)
class Handle:
    runtime: str
    attempt_id: str
    execution_domain_id: str
    host_hint: str | None
    data: JsonObject                  # runtime-owned; on local {"format": "local/1"}

RuntimePhase = Literal["preparation", "execution", "sealing", "inspection", "awaiting_authorization", "publication", "complete", "failed"]

@dataclass(frozen=True)
class RuntimeStatus:
    state: Literal["queued", "starting", "running", "exited", "lost"]
    exit_code: int | None
    phase: RuntimePhase
    observed_at: datetime

@dataclass(frozen=True)
class AdoptedSession:
    handle: Handle
    phase: RuntimePhase
    prepared: PreparedPublication | None
    journal_next: int
    observational: bool

@dataclass(frozen=True)
class RawEvent:
    i: int
    at: datetime
    kind: str                         # a NormalisedEvent kind, `output`, `command_exited` or a supervisor kind
    data: JsonObject

@dataclass(frozen=True)
class UsageCoverage:
    version: Literal[1]
    observation_id: str | None
    observation_sequence: int | None
    completeness: Literal["complete", "partial", "unavailable"]
    observed_at: datetime | None
    attribution_gap: bool
    observation_gap: bool

@dataclass(frozen=True)
class Liveness:
    observed_at: datetime
    supervisor_alive: bool
    candidate_alive: bool
    journal_next: int
    last_event_at: datetime | None
    limited_events: int
    events: int
    input_tokens: int
    output_tokens: int
    usd: str | None
    known_usd: str | None
    pricing_status: Literal["estimated", "partially_priced", "unpriced", "not_billable"]
    usage_coverage: UsageCoverage
    last_event_kind: str | None

@dataclass(frozen=True)
class ArtifactRef:
    artifact_version_id: str
    payload_digest: str
    type_name: str

@dataclass(frozen=True)
class ResourceRef:
    name: str
    sha256: str
    size: int
    executable: bool
    storage_ref: str

@dataclass(frozen=True)
class PublicationOutcome:
    state: Literal["not_applicable", "not_dispatched", "unknown", "completed", "failed"]
    operation_id: str | None
    ref: VcsRef | None                # set when state is completed
    code: str | None                  # content-free; set when state is failed

class Runtime(Protocol):
    name: str
    config_schema: JsonObject
    def capabilities(self) -> RuntimeCapabilities: ...
    execution_domain_id: str
    async def workspace_target(self, attempt_id: str) -> WorkspaceTarget: ...
    async def submit(self, workload: Workload) -> Handle: ...          # spawns and registers the supervisor; returns in phase `preparation`, starts no candidate
    async def transfer_in(self, handle: Handle, item: TransferItem, chunks: AsyncIterator[bytes]) -> None: ...   # only in `preparation`
    async def start(self, handle: Handle) -> None: ...                 # re-hashes every input, then starts the candidate
    async def lookup(self, attempt_id: str) -> Handle | None: ...
    async def adopt(self, handle: Handle) -> AdoptedSession: ...
    async def status(self, handle: Handle) -> RuntimeStatus: ...
    def events(self, handle: Handle, from_offset: int) -> AsyncIterator[RawEvent]: ...
    async def heartbeat(self, handle: Handle) -> Liveness: ...
    async def finalize(self, handle: Handle, authorization: PublicationAuthorization) -> VcsRef: ...
    async def cancel(self, handle: Handle, grace: timedelta) -> None: ...
    async def recovery(self, attempt_id: str, grants: tuple[GrantBinding, ...]) -> RecoveryEvidence: ...   # evidence also when no handle exists
    async def quiescence(self, handle: Handle) -> NormalCompletionQuiescence: ...
    async def record_dispatcher_quiescence(self, attempt_id: str, acknowledgement: DispatcherQuiescence, grant: GrantBinding) -> None: ...
    async def retire(self, attempt_id: str, grace: timedelta) -> TerminationReceipt: ...
    async def release(self, handle: Handle) -> None: ...
```

**Model fields (issue #134).** These rules complete the declarations above; the local realization of the runtime fields is in "Local runtime" below.

- **`JsonObject`** and `JsonValue` are the bounded JSON types of [provider loading](#provider-loading-issue-150): string keys, JSON scalars and finite floats, nesting depth at most 16. Inside an event it is bounded by the event cap of section 3; an object outside events, such as `Handle.data`, is at most 1 MiB after canonical JSON encoding. Since issue #201 the types and `parse_json_object` live in `kora.ir.json`, which counts the canonical bytes (compact separators, UTF-8, no ASCII escaping) during its one walk and refuses an object past 1 MiB, or an integer past 14,000 bits, with the rule `json_size`, and a string or key that is not Unicode text with `json_scalar` or `json_key`; the limit therefore also applies to `KoraProtocolError.details` and `config_schema`. `JsonObjectField` is the one model field type over it, and `kora.ir.errors.safe_field_errors` is the one safe validation-error converter. No model of sections 2, 3, 6 and 12 has a bare `dict` field; section 8 changes under issue #138.
- **`Handle`** is immutable per attempt (on `local`, `data` is `{"format": "local/1"}` and holds no pid, socket, secret or capability): `adopt` returns the handle it was given. It holds no secret and no runtime control authenticator, launch-specification reference, dispatcher token or kill-handle capability. `attempt.handle` belongs to no public read projection; only the worker-authenticated claim and attempt-reporting routes carry it, and `RecoveryEvidence.handle` is copied neither into `attempt.recovery` nor into the recovery projection of the reads. With `portable_handles` false, `host_hint` is only a routing hint.
- **Runtime observations never establish `stopped`.** `queued`, `starting` and `running` support `execution_state = live`. `exited`, `lost`, `Liveness.candidate_alive = false` and `Liveness.supervisor_alive = false` record `unknown`, or keep it, until stopped evidence arrives, and no runtime observation moves `stopped` back. Only three sources establish `stopped`, each recorded with its own provenance: an original-domain `TerminationReceipt`, the runner's normal-completion runtime-quiescence record ("Local runtime" below, carried by the terminal report of issue #139), and operator `confirm_stopped` attestation. `DispatcherQuiescence` affects only dispatcher closure and `replacement_blocked` (ADR-0017). [Data-model section 4](data-model.md#4-state-machines) holds the mapping. Registry states (retired tombstones, retirement before any handle exists, calls after `release`, which raise `KoraProtocolError` and never return empty) are in "Local runtime" below.
- **`AdoptedSession.observational`** is the runtime registry's own view: true while an earlier dispatcher, any dispatcher the registry has seen other than the adopting one, has not yet acknowledged closure. It never authorizes effects; a worker resumes writes only through the API's permitted actions once every recorded grant is closed ([scheduler section 3](scheduler.md#3-submission-execution-uncertainty-and-recovery), step 6).
- **One journal sequence.** `RawEvent.kind` is a `NormalisedEvent` kind of section 3, a command kind (`output` and `command_exited {exit_code}`, which carry the line streaming of [ir.md section 4](ir.md#4-node-kinds-and-their-config) under the bounds of section 3), or a supervisor kind: `phase` and the families `runner.*`, `runtime.*`, `transfer.*`, `control.*` and `dispatcher.*`, which "Local runtime" below lists. `i` comes from the single journal sequencer shared by adapter and supervisor records and is assigned when a record is appended, so it has no holes and follows append order. Two runner kinds are fixed here, in the `runner.*` family: `runner.journal_gap {omitted}`, which the journal writer itself appends when queue space returns, outside the adapter queue and exempt from phase acknowledgement, with its own `i` and the count of the adapter records it stands for; and `runner.cap_reached {kind, omitted}`, which marks the `error` and `output` caps of section 3 with the omitted count, and the byte caps of "Local runtime" below with `kind` the capped file (`journal`, `stdout`, `stderr`, `runner_log`) and `omitted` in bytes.
- **`Liveness`** is pulled once per worker heartbeat poll, never pushed per event. `limited_events` counts `turn`, `tool_call` and `tool_result`, the `max_events` count of DD-36. `journal_next` is the runner's next journal sequence; it is not the API-acknowledged stream offset of `attempt.counters` ([data-model section 2](data-model.md#2-entities)). `last_event_at` follows the advance rules of section 3.
- **`ProcessIdentity`** comes from an injectable source (issue #136). On Linux, `boot_id` is `/proc/sys/kernel/random/boot_id`, `pid_namespace` the device and inode of `/proc/self/ns/pid`, `start_time` field 22 of `/proc/<pid>/stat`, and `host_id` `/etc/machine-id`. On macOS, `boot_id` and `pid_namespace` are null, `start_time` is the `proc_pidinfo(PROC_PIDTBSDINFO)` start in microseconds, and `host_id` is `IOPlatformUUID`. A missing or unreadable `host_id` gives an identity that supports no exit evidence. Two identities are equal when every field but `leads_group` is equal.
- **`GrantBinding`.** The adopting worker passes every recorded grant of the attempt, its own included, to `recovery`: recovery and adoption claims carry them as `earlier_grants`, and so does the heartbeat response while any grant other than the current one is open; `record_dispatcher_quiescence` carries the acknowledging grant's binding, so a worker-side acknowledgement can be checked against its bound process also before any `submit` ([api-and-agent-surface.md](api-and-agent-surface.md#worker-route-bodies-issue-139)).
- **`quiescence`** is a stop and quiescence control call that a closed dispatch gate permits, like `retire` (scheduler.md section 3, step 3).
- **`ArtifactRef`** references one artifact version. List inputs and command-input descriptors belong to issue #106, which builds on this element reference and changes the type of `Workload.artifact_inputs` to hold list inputs.
- **Terminal report and closure evidence (issue #139).** `TerminalReport`, `ClosureEvidence` and their parts live in `kora.protocols` with the terminal-report types (work item 6), and the worker sends them in the attempt's one write-once report and in heartbeat `evidence` ([api-and-agent-surface.md](api-and-agent-surface.md#worker-route-bodies-issue-139)). `NormalCompletionQuiescence` is the runner's record of normal completion ("Local runtime" below); its `scope` names what it attests, and the local runtime does not claim processes it cannot observe. `dispatcher_id` is an opaque, non-secret identifier: authorization comes from the token and grant check, never from it, so it may appear in logs, events and read projections, and it is never the runtime control authenticator. Relayed runtime and dispatcher evidence (registry acknowledgements, `TerminationReceipt` and `NormalCompletionQuiescence`) is trusted only as far as the current lease holder (TM-09), and local runtimes give cooperative guarantees only. The worker reads the normal-completion record with `Runtime.quiescence(handle)` and passes every recorded grant to `Runtime.recovery(attempt_id, grants)`, with `ProcessIdentity` and `GrantBinding` above.
- **`PublicationOutcome`.** `not_applicable` comes only from a sealed request whose `publish` is `None`; `unknown` covers a pending or unknown operation; missing state is `unknown`, never `not_dispatched` or `not_applicable`.

**Inherited lifecycle deadline (ADR-0022).** `ExecutionDeadline.grace` is 30 seconds; the nullable binding is fixed by the first executing claim and sealed in the saved request. The authenticated control context registers it in the original runtime checkpoint before `workspace_target` or pre-submit preparation; submission verifies the same binding. Every runtime/common executor applies it in its actual placement, including `worker_host` preparation and publication. Clip each plan/network/input/credential/inspection operation to its own finite bound and remaining deadline. A runtime unable to supply local cancellation in a declared placement is not conforming; no new provider protocol or Runtime method is added.

At locally observed expiry, persist the closed execution gate, interrupt active managed work and authorize no new useful phase. Escalation derives from the original absolute deadline plus 30 seconds, never from delivery/adoption time. Earlier cancel schedules stay earlier. Candidate code may still run during grace, and an already-authorized remote call can send/settle later; there is no generic external-effect fence. After expiry, completed-operation replay and bounded historical collection/reconciliation/retirement/cleanup remain allowed; remote absence or an unfinished checkpoint cannot authorize another push or change request. Their finite operation/output bounds never delay escalation. [Scheduler section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022) defines nominal UTC-to-monotonic thresholds, saved clock assumptions, sticky expiry and the functioning-supervisor partition model; neither threshold is physical termination evidence.

**Lifecycle and authority.** `kora-runner` is the lifecycle supervisor shipped with the runtime installation. It executes trusted preparation, hosts the adapter and policy client, enforces event/budget/stall limits, records observations, stops candidate execution, seals inputs and prepares publication. The candidate domain contains the harness binary, command workloads, tools, MCP servers, managed helper processes and all descendants. The adapter uses a runtime-supplied process launcher for argv, cwd, constructed environment, stdio and signals; the SDK/client and policy decision remain on the supervisor side. Merely setting a subprocess UID is not a complete protected launcher. The first slice requires both the Claude Agent SDK and Codex app-server paths to verify this contract independently: real launch, policy-hook interception, interruption, and nested tool, helper and supported subagent flows. Each adapter must supply its own real-interface evidence; replay and fake adapters do not establish interception. Unreproducible required policy-hook evidence for either path blocks slice readiness ([ADR-0001](../../docs/decisions/0001-own-control-plane.md), amended 2026-10-21).

The supervisor appends `{i, at, kind, data}` events to its **control journal**, outside the candidate root. On protected runtimes the candidate cannot read/write the control directory, replace a parent path, access its descriptors or impersonate its control channel. Plans, helpers, resolved checkout credentials and publisher metadata live there or in a separate private publication root, never under candidate `.kora/plan/`. Only the supervisor emits authoritative `phase` results; this kind is reserved, so the normalizer never forwards a harness payload as a phase result or platform VCS reference. Harness-derived events remain observations: protected journalling does not establish their honesty, completeness or tool-hook coverage. Candidate `.kora/` may hold harness configuration and checkpoints, but is never a lifecycle source of truth. Images disable harness auto-updates and record their digests on the attempt.

**One logical handle.** `submit` is idempotent on `attempt_id` and binds the saved canonical `execution_request_digest` (ADR-0018), not a serialization of every `Workload` field. The runtime verifies the supplied nonsecret request body and logical workload fields, binds allocated workspace/preparation-plan digests separately in its immutable checkpoint and rejects a changed request or changed previously accepted preparation as `workload_conflict` (`config_error`). Dispatcher/lease authority and injected secret/credential values never participate in this digest; changing those does not change the logical request or permit another execution. `lookup` returns `None` only for authoritative absence of a handle in the original registry, and raises retryable `KoraProtocolError` while unreachable or the registry lineage is unavailable. `None` does not prove that a paused worker cannot later submit; it never authorizes replacement by itself. `RuntimeStatus.lost` is also an observation, not a terminal attempt transition without the retirement/dispatcher evidence below. `adopt` returns the phase, prepared result if any, and journal position. `RuntimeStatus` retains `queued | starting | running | exited(code) | lost` and includes `phase`: `preparation | execution | sealing | inspection | awaiting_authorization | publication | complete | failed`. A harness exit alone does not make a publication-producing workload `exited`: it remains live through finalization. These phases do not add attempt states or consume another attempt/capacity slot. Workloads without a `VcsRef` output collect their ordinary outputs and complete without `finalize`.

Before a protected runtime begins inspection it confirms that the entire candidate domain stopped, including detached descendants, then seals a private handoff. Unconfirmed termination or sealing prevents authorization and fails with `publication_input_unavailable` (`infrastructure`); it does not authorize another concurrent execution. Local follows the lifecycle with ordinary process management in its trusted environment, without this adversarial guarantee.

**Late authorization.** After the supervisor reports `inspection` succeeded and `awaiting_authorization` with a `PreparedPublication`, the worker checks its current lease and desired state through the API heartbeat and obtains a fresh publication credential through `Vcs.acquire_credential` with purpose `publish`, bound to the operation, prepared digest and destination digest (section 4, [ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)). The parent credential remains with the backend's declared issuer, the control plane for the reference GitHub App backend; no publish credential is resolved at `submit`. The issued credential reports its enforced scope, operations, expiry, replacement, revocation and principal; a static credential reports itself as static and is never advertised as scoped or short-lived. Before dispatch the worker checks that every earlier dispatcher and its platform operations are closed (ADR-0017). The current worker owns a local dispatch gate; a lease check does not fence an already queued call. The worker calls `finalize` through an authenticated runtime control channel, not candidate stdio, files or environment. The authorization binds attempt, operation, prepared digest and destination digest; mismatches fail `publication_authorization_mismatch` (`config_error`). Credential values are redacted and never persisted. `PublicationAuthorization.principal` carries the complete `IssuedCredential.principal` or the saved trusted local principal. Persist its nonsecret operation/dispatch snapshot before dispatch and preserve the completed snapshot under the [VcsRef result contract](artifacts/README.md#5-upload-replay-and-result-ownership). The `credential` field has no default: omission is malformed and explicit null is permitted only by the saved trusted `local_credential_free` binding of #140; null in a credential-required binding is a mismatch, never a fallback. Principal, destination, inspection and late-authority checks still apply. Expired/rejected credentials can be reacquired for the same operation; no retry reruns the agent or inspection against mutable files.

`finalize` coordinates the selected common plan executor. With `runner`, publication runs in the runtime's trusted publication environment; with `worker_host`, the runtime adapter transfers sealed input to the private worker-host executor immediately after sealing, before prepare/inspect. That executor produces the stored `PreparedPublication`; `finalize` subsequently delivers authorization to it and consumes that existing checkpoint. VCS tools may exist only on that host. It never points a credentialed publisher at the exposed candidate directory. Placement is a runtime transport responsibility, not VCS-specific logic. A runtime declaring portable handles must also make sealed input, journal and publication checkpoint accessible to an adopting worker; host-local storage cannot support that claim. An active finalization is serialized per operation, and repetitions reconcile the same prepared revision (section 4). The worker opens the change request through the VCS API, uploads outputs/usage and requests `finishing`; output-source artifacts may return `staged` until atomic complete-result/key acceptance (ADR-0019), which is not another execution or publication grant; it keeps heartbeating through authorization and publication. An API fence is not an atomic fence against an external push already in flight.

**Saved request before effects (ADR-0018).** Before `workspace_target` or any preparation/submission effect, the worker seals the nonsecret logical request through the existing attempt-reporting API and receives its durable digest. It uses current policy revision and capability checks while the request is unsealed. The body owns exact input bindings, rendered config/feedback, composed execution policy, logical workspace/retry/publication intent, selected implementations/resources, limits and secret references; the original domain is bound as an immutable execution identity. Request sealing uses current execute authority and result authority, and does not grant or renew either. Sealing retries of identical content return the recorded descriptor read-only after current namespace/service/lease checks; an existing-record match precedes policy-revision revalidation so a lost response followed by a policy update cannot change the original request. An unsealed cancelled attempt has no fabricated request.

A workspace root may be allocated only afterwards. `WorkspaceTarget`, executor placement, checkout/publish plans and harness configuration files are derived using the saved logical values and trusted provider bindings. The runtime records their exact digests before applying them and compares duplicates against that checkpoint. Allocation paths and actual checkout results are observations, not reasons to re-render the prompt or change the semantic-request digest. Request and private checkpoint retention are separate from the lifetime of an API lease. New recovery authority adopts existing trusted state under ADR-0017; it does not reconstruct a request or restart preparation from current aliases, VCS HEAD or feedback. The existing session-spec builder may use the allocated root before checkout, but must preserve the saved logical request and its rendered text.

Actual retry fallback can be established only after runtime/harness checks. The request records intended strategy and prior session/artifact identity; the supervisor records selected strategy/fallback and installed harness version when known, bound to the request digest. A fallback permitted by the request is a recorded decision under the pinned semantics; it does not silently replace intended inputs. A contradictory runtime/session state is an explicit failure.

**Execution-domain registry and dispatch gates.** `execution_domain_id` is available without preparing a workspace or submitting work and identifies the durable authority/registry lineage. Persist it and each dispatcher grant in the API before any effect; authenticate runtime control calls through the authenticated control channel, bound to the opaque, non-secret dispatcher identity of that grant. `workspace_target`, preparation, submission and finalization may not bypass this control gate. The API epoch remains an API fencing value, not a provider-enforced external fence. Losing or replacing the registry requires a new domain id; even if lookup in the new registry is empty it cannot certify old-domain termination. No shared worker filesystem or separate coordination service is required: the runtime's existing control registry owns its fence and evidence. `PublicationOutcome` distinguishes no operation dispatched, pending/unknown, completed and confirmed failed, with the operation identity and reference when available; it cannot infer no operation from missing state.

Each trusted worker serializes its final lease/desired-state check and every effect dispatch with its local gate. On closure it atomically rejects all future dispatch under that grant, drains in-flight work or obtains authoritative provider fencing, then records `DispatcherQuiescence`. Closure is irreversible for that dispatcher id. A mere API rejection, process heartbeat gap or runtime receipt cannot certify these steps. The API retains **all** grants, including a worker paused before its first runtime call. `record_dispatcher_quiescence` authenticates the originating incarnation, binds every id and generation, and appends immutable/monotonic evidence in the original registry; it permits closing an old grant, never starting work. `recovery` returns this evidence even if no handle exists. A current claim performing recovery relays it through existing API reporting routes; stale epochs remain rejected for every API write. Missing/contradictory evidence is unresolved, never a fabricated acknowledgement.

**Retirement and termination evidence.** `retire(attempt_id, grace)` installs a durable per-attempt fence in the same registry/serialization boundary as `submit` and `finalize`, including when called before any handle exists. Racing submit either registers before the fence and is stopped with all managed candidate/publisher processes, or is rejected without starting. Finalization either settles its recorded operation or remains unknown; retirement may not return a receipt while a managed publisher or its external operation can still take effect. The fence also rejects later preparation and new publication dispatch. A queued duplicate of a completed operation can return the recorded result read-only, but never re-execute an effect. `retire` is idempotent: repeated success returns the same receipt; interruption/unreachability/uncertain termination is a retryable `KoraProtocolError`, with no affirmative receipt. `cancel` remains a cancellation request, not proof. A completed normal lifecycle records equivalent affirmative runtime quiescence and fences future dispatch; successful work remains successful. `release` is allowed only after the API has settled all dispatcher and publication recovery. Small registry entries, receipts, acknowledgement records and per-attempt tombstones survive bulky workspace/control-journal cleanup indefinitely; a future bounded-retention scheme must first prove no old dispatch can arrive.

A receipt binds the original attempt/domain and attests only to work controlled by that runtime. It does not certify independent worker-host publication or a worker's code-host call. The API's replacement barrier clears only after runtime quiescence **and** every recorded dispatcher and kora-managed publication is closed. Provider evidence or an operator's explicit attestation may establish closure of an inaccessible old authority, with provenance kept distinct. Already-issued agent/MCP effects are not rolled back and are not promised exactly-once. On local these are cooperative guarantees in the trusted development/test environment, without sandboxing or hostile same-user containment.

**Recovery state.** Before acknowledging each completed phase the supervisor durably records attempt/workload digest, immutable plans and their digests, trusted `WorkspaceRef`, journal sequence, phase, sealed input location/digest, `PreparedPublication`, inspection verdict and publication outcome. The checkpoint binds the API-sealed execution-request digest, exact derived plan/workspace identities and observed facts. Credential references/identities only are retained, never values or secret-derived hashes. Request/phase observations use the existing trusted journal and API event path; missing observations remain unknown, and duplicate journal sequence delivery is idempotent while conflicting content is rejected. A crash after sealing or preparation resumes from those bytes; no new commit is made. Loss/corruption of required private state is an infrastructure error, but leaves recovery unresolved until termination and dispatch/publication closure are proven. `release` removes bulky candidate, control-journal and publisher storage only after final output collection, terminal publication outcome and settled recovery; durable retirement/dispatcher registry evidence remains; partial usage/logs may still be collected on cancellation, but cancellation never initiates publication.

**Command resources** (issue #45). For a command workload with `resources`, the worker fetches each file by digest through the lease-checked route `GET /attempts/{att}/resources/{sha256}` and hands the bytes to the runtime through its transfer channel; no graph package or author filesystem is involved. The runtime materialises them into a private `resource_root` outside the candidate root, with the same descriptor-relative, no-follow creation rules as plan files, so parent replacement, symlinks and hard-link aliases cannot redirect the writes. On a runtime declaring `protected_execution`, `resource_root` and every parent are owned by a principal other than candidate code and are read-only to it. The executor re-hashes every file after materialisation and again immediately before the command starts; any mismatch fails the attempt with `integrity_error` before the command runs. `{resource_root}` is the only placeholder that names the directory. A runtime that cannot transfer resources fails validation with `IR009` and execution with `config_error`. A runtime without `protected_execution`, including `local`, materialises the files and reports `resource_protection: none`; the namespace policy `require_protected_resources` turns that into `IR024` at validation.

**Capabilities and reference runtimes.** `protected_execution` covers control-state integrity, whole-domain containment, immutable transfer and publisher credential isolation together. It is independent of `enforces_network_policy`, `isolates_tool_secrets` and `credential_proxy`. `enforces_network_policy` means the candidate's traffic can reach only the resolved allowlist: the reference `docker` runtime places the candidate container on a network without a direct route out and sends its traffic through an egress proxy that allows only the listed domains, and `local` declares false. `isolates_tool_secrets` means an MCP server that declares `secret_env` runs outside the agent's process tree and filesystem: the reference `docker` runtime starts each such server in a sidecar container of the attempt, on the attempt's private network with the server's own `network_policy`, and configures the harness to reach it over MCP's HTTP transport; `local` declares false. Validation applies `IR015` to both (ADR-0009 as amended 2026-10-18). Namespace policy `require_protected_execution` rejects a runtime declaring false (`IR021`); worker startup rejects a configured protected runtime whose installation cannot enforce its declaration. A missing executor is an error, not a warning that permits execution.

| runtime | lifecycle placement | protected execution | recovery and executors |
|---|---|---|---|
| `local`, Linux/macOS | host supervisor, candidate subprocess group, private directories for ordinary lifecycle structure | false; trusted development/tests, no sandbox, VM, administrator permission or extra trust switch | host-bound handles; `runner` and `worker_host` |
| `docker`, Linux engine (including a Linux VM on a macOS host) | host supervisor; separate candidate and publisher containers under one attempt handle | required supported configuration: candidate without control mounts, Docker socket, privileged mode or host PID namespace; confirm whole container stopped; private sealed transfer; publisher sees only sealed input and trusted metadata | host-bound handles; `runner`; Docker-dependent tests run on Linux CI |
| independent runtime | protected supervisor/control channel and isolated candidate/publisher environments supplied by the provider | declare true only with containment, transfer and OS/service evidence; otherwise rejected when required | no shared worker filesystem required; handles portable only when all recovery state is portable |
| fake runtime ([fakes.md section 5](fakes.md#5-the-fake-runtime)) | real protocol implementation, constructed by injection, with injected deterministic events, worker-loss and runner-death controls and phase barriers | proves protocol decisions, never OS isolation; security claims require real process tests | `runner`, nothing exposed on worker host |

Local process groups provide cleanup, not a hostile-code boundary. The same capability rules apply in standalone and shared installations. Neither reference runtime supports cross-attempt harness resume in MVP: `supports_resume = False`, because `release` destroys its harness checkpoint. `portable_handles = False` requires access to the original runtime domain, normally on the same host; the stable host id is only a routing hint and registry loss cannot impersonate the old domain; partition/loss policy is in scheduler.md section 3. `credential_proxy = False` remains the MVP default. Harness checkpoints and VCS revisions are separate from the lifecycle checkpoint: the VCS revision outlives release, the reference harness checkpoint does not. A future runtime declares `supports_resume` only if that harness state survives release and is reachable by the next attempt. Enterprise workers still connect outbound to the API, and default `stall_after` remains 15 minutes.

### Local runtime (issue #123)

Everything here is cooperative on `local` (ADR-0016): same-user code can read `KORA_ATTEMPT_TOKEN`, edit the registry and journal, and kill a worker to produce real exit evidence. The design follows the [local runner adoption probe](../research/local-runner-adoption-probe-2026-10-19.md), which ran on macOS only, and the [mechanisms probe of 2026-10-22](../research/local-runtime-mechanisms-probe-2026-10-22.md), which checked on GitHub's Linux and macOS runners the mechanisms its record lists; the runner work item verifies the rest on Linux and macOS CI, among them peer attestation on an accepted listening socket and from the connecting side, the pid read from a pidfd's `fdinfo`, the process start time on macOS, the exit status carried in a `NOTE_EXITSTATUS` event and every service-manager rule below.

**One detached `kora-runner` per attempt, found through a registry per execution domain.**

- **State root.** The runtime configuration field `state_root` defaults to `$XDG_STATE_HOME/kora/local` on Linux (falling back to `~/.local/state/kora/local`) and `~/Library/Application Support/kora/local` on macOS. It is opened `O_DIRECTORY|O_NOFOLLOW` and checked with `fstat` (owner the user, mode 0700); everything beneath it is opened relative to that descriptor. A symlink, a wrong owner or mode, or a network filesystem (`statfs` type) is refused with a named configuration error, never repaired.
- **`domain.json`** records the `execution_domain_id`, the creating `host_id`, and the pid namespace per `boot_id`. A changed `host_id` or a missing file is a lineage error: retryable, never `None`. A new boot only adds its pid namespace. Each runtime start appends the current `boot_id` to the boot log under the root-level lock file `boot.lock`.
- **Spawn.** `submit` starts the runner through the runner launch command injected into the runtime (issue #136). It double-forks into a new session, with stdin `/dev/null`, every descriptor closed except one readiness pipe, and a constructed minimal environment with no API token, database URL or worker configuration secret. The worker waits on the readiness pipe with a deadline and reaps only the launcher.
- **Registry.** `registry/<attempt>.json` is replaced atomically (write `registry/<attempt>.json.tmp-<nonce>`, fsync, rename, fsync the directory); a temporary file whose write or fsync failed is unlinked before the lock is released, each write first removes the attempt's leftover temporary files, and a temporary file left by a crash is removed at the next lock acquisition for that attempt and at `release`. Each attempt has its own lock file, `registry/<attempt>.lock`, acquired with `LOCK_NB` against a 2 s deadline, a timeout being retryable. A lock is held only for the read-compare-write of one transition, never across a spawn, the readiness wait, a retirement grace or a runner call; reads take no lock, because the atomic rename keeps them consistent; the stop path and kill escalation never wait on a lock (ADR-0022). The lock file is kept with the tombstone. The registry is the durable evidence store: every `DispatcherClosure`, the acknowledgement with its provenance and observed identity, and the quiescence record, live in the entry and survive into the tombstone, and `evidence_ref` and `record_ref` name the registry record. The runner checks for the fence under the lock immediately before starting any candidate.

| state | holds | written by |
|---|---|---|
| `unsubmitted` | dispatcher records only | the first evidence or acknowledgement recorded before any `submit` |
| `starting` | launch nonce, request digest, started at | `submit`, before spawn, from no entry or by compare-and-set from `unsubmitted` |
| `running` | runner `ProcessIdentity` (kept in every later state), socket path, dispatcher records | the runner, by compare-and-set from `starting` with its own nonce and no fence; otherwise it exits without starting a candidate |
| `completed` | the normal-completion quiescence record; a fence | `quiescence` |
| `retiring` | the fence | `retire` |
| `retired` | the tombstone: receipt, scope, dispatcher evidence, the normal-completion record if any, capped samples with counts; at most 64 KiB plus 512 bytes per recorded dispatcher grant | `retire` (from no entry, `unsubmitted` or `starting` with scope `no_runner_started`), the runner finishing `retire` from `running`, `completed` or `retiring`, the runner after a full-disk stop, or the worker-side fallback |

`released_at` marks `release` on a `completed` or `retired` entry.

| operation | no entry, `unsubmitted` | `starting` | `running` | `completed` | `retiring` | `retired` | after `release` |
|---|---|---|---|---|---|---|---|
| `submit` (same digest) | moves to `starting` | retryable until the runner registers | returns the handle | returns the handle | `attempt_retired` | `attempt_retired` | `attempt_retired` |
| `transfer_in`, `start`, `finalize` | refused: nothing submitted | retryable | allowed by phase; `journal_unavailable` under the full-disk fence | refused by the fence | refused by the fence | refused by the fence | `attempt_retired` |
| `lookup` | `None` | retryable | the handle | the handle | the handle | the handle | the handle, never `None` |
| `status` | no handle | retryable | the runner's observation | `exited`, phase `complete`, never `lost` | the last observation | `exited`, the tombstone's phase | as recorded |
| `quiescence` | refused: nothing submitted | retryable | allowed once the candidate exited; `journal_unavailable` under the full-disk fence | the recorded record, worker-side once the runner has exited | refused by the fence | the recorded record, if the entry completed first | as recorded |
| `retire` | writes the fence and a `no_runner_started` tombstone | writes the fence, sweeps for the launch nonce, then tombstones `no_runner_started` | allowed; retryable under the full-disk fence until the tombstone is written | allowed; the tombstone keeps the normal-completion record | continues the retirement; retryable until it ends | the recorded receipt | the recorded receipt of a retired entry; `attempt_retired` for a released completed entry, whose normal-completion record stands |
| `recovery` | the recorded dispatcher evidence | the recorded evidence, unresolved | allowed; unresolved under the full-disk fence | evidence with `normal_completion` | evidence, the retirement unresolved | evidence with the receipt | as recorded |
| journal, log and output reads | nothing to read | nothing to read | allowed | allowed | allowed | allowed | `attempt_retired` |
| `record_dispatcher_quiescence` | worker-side, under the lock | worker-side, under the lock | through the runner, or worker-side once the runner is gone; `journal_unavailable` under the full-disk fence | through the runner, or worker-side once the runner has exited | worker-side, into the entry | worker-side, into the tombstone | `attempt_retired` |

- **Entries before submit.** With no entry, or in `unsubmitted`, `lookup` returns `None`, the authoritative absence of a handle, which certifies nothing about earlier dispatchers ([scheduler section 3](scheduler.md#3-submission-execution-uncertainty-and-recovery), step 5), and `recovery` returns the recorded dispatcher evidence. In `starting`, `lookup` is retryable: until the spawn deadline (30 s) and after it, until `retire`, which writes the fence, sweeps for the launch nonce, checking identity before signalling, kills any match, and only then tombstones with scope `no_runner_started`, a choice made from registry state, never from whether the API has a handle. `retire` with no entry writes the tombstone itself, so a delayed `submit` finds the fence and returns `attempt_retired`. `submit` never respawns under an existing entry; a different request digest is `workload_conflict`. `lookup` returns the immutable `Handle` rebuilt from the entry.
- **Worker-side acknowledgements.** With no runner, the worker-side runtime appends an acknowledgement under the registry lock only when the caller's own `ProcessIdentity` equals the process the runner recorded for that grant or, for a grant that never reached the runner, the bound process of the `GrantBinding` passed with it; this check is cooperative on local.

**The control channel.**

- **Socket directory.** On macOS `confstr(_CS_DARWIN_USER_TEMP_DIR)/kora`; on Linux `$XDG_RUNTIME_DIR/kora` when the user manager lingers, otherwise `/tmp/kora-<uid>`. The path fits the 104-byte `sun_path` of macOS, and abstract sockets are never used. The directory is opened `O_DIRECTORY|O_NOFOLLOW` and checked owner 0700 with `fstat`; anything else is refused. The runner re-verifies its socket's inode every 60 s and rebinds it if the path was removed, repeating the checks if it recreates the directory. A refused or missing socket while the registry says `running` is retryable, never `lost`. The socket name is a 16-character hash of the execution domain id and the attempt id.
- **Frames.** A 4-byte length and a 1-byte type (`J` JSON, `B` bytes, `E` journal record), at most 1 MiB per frame.
- **Dispatcher peers.** The worker's incarnation is authenticated by the socket directory's 0700 mode, the kernel's peer attestation below, a peer uid equal to the runner's, and its attested `ProcessIdentity`; there is no shared secret, because same-user code could read one. Its first frame names `dispatcher_id` and `incarnation_id`. The worker authenticates the runner the same way before it sends that frame: the listening peer's attested `ProcessIdentity` must equal the registry's `running` runner identity, and its uid the worker's own. On first attach the runner records `(dispatcher_id, incarnation_id, attested ProcessIdentity)`, which never changes; a later attach under that id from another process is refused, and the attested identity must equal the grant's bound process from `Workload` or `recovery`, a binding recorded at `submit` or presented by the attaching worker from the API's grant list; the runner cannot tell a presented binding from one a same-user process supplies naming itself, which the threat model's `local` row states. A `registry_acknowledgement` is accepted only from the recorded process.
- **Launcher peers** hold a one-shot per-launch capability and may call only `launch_spec` for that launch (section 3).
- **Connection bounds.** A connection that has not completed its handshake (a dispatcher's attach, or a launcher peer's capability and the checks of step 2 of the harness launcher in section 3) within 5 s is closed; a launcher connection that has completed it stays open until `exec`, a failure record or the session's escalation; at most 16 connections per attempt are open at once, of which at most 4 follow `events`. `events` streams from the journal file one frame at a time, with no buffer per follower beyond that frame.
- **Kernel attestation and signalling.** The peer is attested by the kernel (`LOCAL_PEERPID` with `LOCAL_PEERCRED` on macOS, where when the pid is captured is verified by the runner work item, `SO_PEERCRED` on Linux, `SO_PEERPIDFD` where available). On Linux with `SO_PEERPIDFD`, the identity is read through that pidfd (its pid from the pidfd's `fdinfo`), and a pidfd whose process has exited, checked again after the start time is read, is refused, so a reused pid cannot carry the attestation. Without a pidfd (macOS, older Linux), the peer is refused when the start time looked up for its pid is later than the connection's accept time, both on one boot-relative clock (`/proc/<pid>/stat` ticks against `CLOCK_BOOTTIME` on Linux; `proc_pid_rusage` `ri_proc_start_abstime` against `mach_absolute_time` on macOS) with the start time rounded up to the next tick, so rounding refuses rather than accepts, and the lookup is repeated after the comparison. That still leaves a window: a peer can connect, hand its descriptor on and exit, and its pid can be reused before the accept; this residual is stated in the threat model's `local` row. On the worker's side the kernel reports the listener's identity as of `listen`, not as of the connection, so the worker anchors no time window there: it requires the attested pid's current identity (through its pidfd where available, otherwise its start time read after the connection) to equal the registry's `running` runner identity, pid and start time, so a reused pid fails the comparison, and it closes the connection without sending its first frame otherwise. A peer pid of 0, which a peer outside the runner's pid namespace reports, is refused. Signals to a single process go through `pidfd_send_signal` where available, or after a start-time recheck immediately before signalling. The runner signals a leader's group only once the leader is verified to lead its own group (pgid equal to pid), and then only while it holds the leader unreaped or after a recheck that the leader is alive; on Linux it uses `PIDFD_SIGNAL_PROCESS_GROUP` on the leader's pidfd, under the same precondition, where the kernel has it. On macOS, and on Linux without that flag, a race between the recheck and a group signal sent without the unreaped hold remains.
- **Operations.** `status`, `heartbeat`, `adopt`, `transfer_in`, `start`, `events{from}`, `transfer_out`, `cancel`, `finalize` (carrying `PublicationAuthorization`), `quiescence`, `retire`, `record_dispatcher_quiescence`. The kill handle is runner-internal and on no channel. Each incarnation keeps one persistent control connection; `control.attached` is journalled only on `adopt`, refused authentications are coalesced into one counted record, and the runner watches every connection, including one blocked on `events`, for end of stream.

**The journal.**

- **Format.** One canonical JSON line per `{i, at, kind, data}` record, on `O_APPEND`; `i` is assigned at append, and a reader ignores a trailing partial line. The supervisor kinds are `phase`, `runner.started`, `runner.candidate_started`, `runner.outputs_sealed`, `runner.quiescence`, `runner.retired`, `runner.journal_gap`, `runner.cap_reached`, `runtime.retired_without_runner`, `transfer.materialised`, `transfer.rejected`, `control.attached`, `control.detached`, `control.auth_failed` and `dispatcher.quiescence`; `EventSink` rejects all of them from adapters (section 3).
- **Writing.** One writer thread performs every write. `checkpoint.json` is replaced atomically like the registry entry, its temporary files handled the same way. Supervisor records, registry writes and `checkpoint.json` are durable before acknowledgement (`fdatasync` on Linux, `F_FULLFSYNC` on macOS), a few records per attempt, so tombstones and fences survive power loss; a sync over 5 s ends the attempt as `journal_unavailable`. Runner exit and stop escalation never join or wait on the writer thread, and diagnostics that cannot be written are dropped with a count instead of blocking. Adapter records reach the writer through the bounded queue of section 3 ("`EventSink` and journal back-pressure") and are batched every 50 ms or 256 records, the timer armed only while the batch is non-empty and the batch flushed before each supervisor record, so file order is `i` order; they survive a process crash once written, records still queued are lost with the runner, and the power-loss limit applies to them only.
- **Caps.** The journal is capped at 256 MiB, the stdout and stderr logs at 64 MiB each and `runner.log` (one writer) at 16 MiB; interpreter crash output goes to `runner.crash`, written only by `faulthandler` and fatal output, at most 1 MiB. Past a cap the runner writes one `runner.cap_reached` record and keeps draining the candidate's pipes, counting the discarded bytes. At the journal cap adapter records go to `runner.journal_gap`; supervisor records are still appended up to 1 MiB past the cap, beyond which only the kinds written at most once per attempt (`phase` once per phase, `runner.outputs_sealed`, `runner.quiescence`, `runner.retired` and `runtime.retired_without_runner`) are still appended, and every other kind is counted in memory and written once, as one counted record per kind, with the terminal supervisor records. At most 1,000 transfer items are allowed per attempt, which bounds the synced records. These values are proposals.
- **A full disk.** A runner whose registration write (`starting` to `running`) fails exits without starting a candidate and writes nothing, leaving the entry to `retire`'s `starting` path. In every state, the first journal append or sync that fails with `ENOSPC` or `EDQUOT` closes the journal for the process that saw the failure: that process appends nothing more to it, readers treat it as ending at its last complete line (a complete line whose sync failed stays readable and is counted in the tombstone as not durable), as after a crash, no `i` is reused, every `events` stream ends with a `journal_unavailable` frame, and that process's later records go only into the tombstone, as counts and the evidence they carry. A later writer in another process, the worker-side fallback, repairs the tail as a second writer below and continues `i` from the last complete line. Diagnostic and log writes are dropped with a count instead, and a failed index write disables the index, so readers scan. Outside `running`, and for a worker-side registry write in any state, a registry or journal write that fails that way refuses only its operation with a retryable error; the outcome and any normal-completion record stand, and `retire` stays retryable until its tombstone is written. In `running`, when a write of a supervisor record, an adapter-record batch, the registry or `checkpoint.json` fails that way, the runner, in this order: holds an in-memory fence, which needs no disk, that refuses `start`, `transfer_in`, `finalize`, `quiescence` and `record_dispatcher_quiescence` with a retryable `journal_unavailable` error, even while the entry still reads `running`, so nothing starts during the stop and a full-disk-stopped attempt is never recorded as a normal completion, while attach and `adopt` stay accepted; stops the candidate domain and any publisher domain (the stop path needs no disk), so a `finalize` in progress ends with its outcome `unknown` and recovery reports the publication for reconciliation ([ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md)); and closes the journal. The terminal evidence then goes only into the tombstone: the stop evidence, a first attach's dispatcher record, and the counts of the records not journalled (queued and later adapter records, and the supervisor records that would have followed), held in memory and bounded like the tombstone. The attempt fails with `journal_unavailable` (`infrastructure`). Until the tombstone is written, the runner retries it after 1 s and then doubling up to 30 s. Under the fence, `retire` starts no write of its own: it joins the runner's next scheduled retry, which writes its fence with the tombstone, and returns a retryable error until that retry succeeds. The runtime deletes nothing to make space: the workspace, the placements and the transcript stay in place for `release`. When the runner is gone, the worker-side fallback runs its own sweep, and its receipt carries only that sweep's scope; its journal closes on a failed append or sync in the same way, and its record then goes into the tombstone only. If the tombstone cannot be written, the worker-side runtime keeps the sweep result in memory in tombstone form (capped samples with counts, within the tombstone bound) and owns one retry timer per attempt with the same backoff; each retry re-reads the entry under the lock and carries every dispatcher record into the tombstone, so worker-side acknowledgements and process-exit evidence recorded meanwhile are kept; `retire` joins that timer and returns a retryable error until it succeeds, worker shutdown cancels the timer without joining it, drops the held result and writes nothing, and a restarted worker runs one new sweep. Until the tombstone is written, the runner keeps its socket and connections, `kora doctor` reports the attempt as stopped on a full disk for an operator to free space, and each failed write is a WARNING diagnostic, coalesced with a count. Recovery stays unresolved and retryable meanwhile; no evidence is claimed that was not written.
- **The stop path** never waits on the writer or a lock; with the journal unavailable, the stop evidence lands in the registry tombstone once it can be written. `control.detached` and the coalesced `control.auth_failed` record are written at most once per incarnation.
- **A second writer.** Before the worker-side fallback appends, under the lock with the runner confirmed dead, it repairs the journal: it truncates it to its last newline and records the number of discarded bytes. It appends `runtime.retired_without_runner` only when the journal holds none, so a restarted fallback never writes it twice.
- **Index and reads.** A sparse offset index every 4,096 records lets `events{from}` resume near a cap without a full scan. Once the runner is gone, the worker-side runtime reads the journal tail, the logs and the transcript descriptor-relative under the same caps and digests and relays them as segments.

**Transfer.** `TransferItem` names one item. Kind `script` carries a fake harness script to the testing runner of [fakes.md section 3](fakes.md#3-the-testing-runner-and-the-scenario-composition): it is transferred once, in `preparation`, and materialised under the runner's control directory outside every candidate-visible root, and the production runner refuses it. Its `name` is a single path component with no `/`, `..`, NUL or control characters and is never empty. Proposed limits are 1 GiB per item and 4 GiB per attempt; issue #106 owns input semantics.

- **`transfer_in`** is allowed only in `preparation`, and the worker streams from a spool file, never from memory. The runner writes `O_CREAT|O_EXCL|O_NOFOLLOW` beneath the input-root descriptor, hashing while it writes. On a match it fsyncs, sets mode 0400, renames with no replace (`RENAME_NOREPLACE` or `RENAME_EXCL`), fsyncs the directory and journals `transfer.materialised`. On a mismatch, a disconnect or a cancel it removes the temporary file; a mismatch is journalled `transfer.rejected` and fails with `integrity_error`. A different digest under an existing name is `workload_conflict`.
- **Sealing** resolves each output path beneath the output-root descriptor with `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)` on Linux and a component-by-component `openat(O_NOFOLLOW|O_DIRECTORY)` walk on macOS, so no intermediate symlink or `..` is followed; it opens the file `O_NOFOLLOW|O_NONBLOCK`, accepts only regular files (`S_ISREG`) on the root's device (`st_dev`) with one link (`st_nlink` 1) within per-file and total bounds, and records symlinks, hard-linked files, FIFOs and other special files as refused. It journals `runner.outputs_sealed`, a SHA-256 manifest of at most 1,000 entries, which keeps the record below the frame cap.
- **`transfer_out`** accepts only manifest names and streams 64 KiB frames, then a trailer digest. The worker spools to a file and keeps it only if the received digest, the trailer and the manifest agree, and deletes its input and output spools on success, failure and cancel. Each incarnation has its own spool directory, and worker start-up sweeps only directories whose incarnation's `ProcessIdentity` is gone; `kora doctor` checks free space under the state root. A pull is retried from the sealed source at most 3 times, then fails with `infrastructure`. Of the control directory only the two logs can be exported.

**Local provider evidence closes a dead worker's dispatcher.** `runtime_observed_process_exit` holds for a grant only when every condition holds:

1. **A bound identity.** The grant has a bound `ProcessIdentity`; a null process yields no evidence.
2. **The same host.** The `host_id` is present and equal to this host's; otherwise the grant stays unresolved.
3. **The process is gone.** If the `boot_id` differs and appears in this state root's boot log, the process has exited and the remaining conditions are not needed; a `boot_id` the log does not know, as on a cloned host, leaves the grant unresolved. Otherwise the pid namespace must be equal (else unresolved), and the pid must be absent or its start time differ.
4. **Its dispatch group is empty.** No process remains in the process group whose id is the bound pid. At start-up the worker checks `getpgid(0) == getpid()`, otherwise calls `setpgid(0, 0)`, or refuses to start, and its registered identity records `leads_group: true`; an identity without that record supports no exit evidence. Every effect subprocess it starts (a `worker_host` executor, `git`) stays in that group, and kill escalation for such a subprocess signals its verified pid through a pidfd or after a start-time recheck, never a separate group. A group's id cannot be reused while a member exists, so a surviving child that stays in the group keeps the grant open. Effect subprocesses run without detaching: `git` with `gc.autoDetach=false` and `maintenance.autoDetach=false` and without the credential-cache daemon, `ssh` with `ControlMaster=no`, and no agent auto-start. A descendant that still leaves the group with `setsid` or `setpgid` is not observed, and its effects escape the evidence; this limit is stated with the evidence.
5. **The runner holds nothing from it.** The runner has no open connection from that process, and every request it received from it has completed, or been aborted and journalled or, with the journal closed, held for the tombstone. Right after a death the runner refuses retryably until this holds, and the worker retries against a 5 s deadline; a refusal is never closure.

The evidence covers grants that never reached the runner, because the bound identity comes from the API's grant list; without a runner, the local runtime evaluates conditions 1 to 4 under the registry lock and appends the evidence to the entry. It carries the observed identity, which the API compares with the grant. It does not prove that the dead worker's worker-side effects completed: those keep the idempotent-submit and registry-lock rules, and publication still needs reconciliation ([ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md), clarified 2026-10-21).

**Honest receipts.** `TerminationReceipt.scope` and `NormalCompletionQuiescence.scope` name what was observed, and `attempt.recovery` records it. Quiescence holds within the recorded scope, and local scopes are cooperative.

| scope | where | stated limit |
|---|---|---|
| `subreaper+leader_group+env_sweep` | Linux; the runner is `PR_SET_CHILD_SUBREAPER` | non-dumpable processes hide their environment; a process that clears or overwrites its environment, or is started through an existing same-user daemon (the user manager over D-Bus, a terminal multiplexer server), is not seen |
| `leader_group+env_sweep` | macOS | a `setsid` descendant running a platform binary is invisible; a `setsid` descendant that clears or overwrites its environment, a group member that clears its environment once the leader has been reaped (no subreaper adopts it), or a process started through an existing same-user daemon, is not seen |
| `env_sweep` | the worker-side fallback when no leader identity is alive | as above, and any detached descendant without the token; same-user code that rewrites `attempt.token` hides the processes holding the real one and, after a worker restart, defeats the worker-side redaction of the real token |
| `no_runner_started` | registry state only | none: no candidate ever ran |

- **Leaders.** Each launch and each command is its own session and group leader, and the runner records every leader's identity.
- **Reaping.** One reaper, as subreaper on Linux, is woken by `SIGCHLD`, a pidfd per known child on Linux, or `kqueue` `EVFILT_PROC`/`NOTE_EXIT` on macOS, never by a `P_ALL` loop. On each wake-up it lists the runner's children (`/proc/self/task/*/children` on Linux; its own known children on macOS) and reaps only unregistered children (`waitid(P_PID, pid, WEXITED|WNOHANG)` on Linux; on macOS, where CPython has no `os.waitid`, `waitpid(pid, WNOHANG)` after the exit status was read from the `NOTE_EXITSTATUS` event), each after a 1 s grace from its first observed exit; a timer re-scans at the end of each grace and every 5 s while any child exists. It never touches a pid that an in-process waiter registered, including the SDK's own wait on the launch pid, which is registered when its launcher peer connects.
- **The worker-side fallback** applies when the runner is lost, and fails closed. It runs only once the runner's identity is confirmed gone; if the runner is alive but unresponsive, it first signals the runner by its verified identity. It signals only processes with original-group evidence: recorded leader identities still alive with pgid equal to pid, and processes holding `KORA_ATTEMPT_TOKEN` in a readable environment, with their identity rechecked before each signal. It never signals a bare numeric pgid. It issues a receipt only when its scope's condition holds (every leader is gone and the sweep is empty); when `attempt.token` is missing, unreadable or malformed, no environment sweep is performed, no receipt is issued and recovery stays unresolved; members of an original numeric pgid it cannot verify are listed by pid and start time only and leave recovery unresolved, with no receipt. It signals only individually verified processes. Environments and arguments read during the sweep are compared in memory and never retained in any record or diagnostic. It reads `KORA_ATTEMPT_TOKEN` for its sweep from the attempt's `attempt.token` file, mode 0600, created `O_EXCL|O_NOFOLLOW` at `submit` in the control directory, outside every candidate-visible root and every exported item, the only file that holds it. Each process that constructs or reads the token registers it for redaction in its attempt scope, under the registration rules of section 6, before it writes the file or sends a launch specification, and the worker-side runtime reads and registers it before it relays the journal, the logs or the transcript, refusing the relay retryably, with no content, while the file cannot be read, so diagnostics and relayed segments never show it. It appends `runtime.retired_without_runner` after the torn-tail repair above.

**Normal completion.** Following the worker sequence of [scheduler section 3](scheduler.md#3-submission-execution-uncertainty-and-recovery), the worker calls `quiescence(handle)` after closing its gate and acknowledging. The runner requires the candidate to have exited and every `finalize` to have settled with its publisher domain stopped, runs the domain stop with a 5 s grace, writes the `completed` state with the `NormalCompletionQuiescence` record under the lock, and returns the record; from then on `start`, `transfer_in` and `finalize` are refused. Once `completed` or `retired` is durably written, the runner closes its connections and exits when its last follower has disconnected or 60 s after the end of its last request or stream, whichever comes first; before that, no idle timer applies, so a runner whose worker is gone keeps the candidate for adoption. After it exits, `status` reports a completed entry as `exited` in phase `complete`, never `lost`, and `quiescence` and `recovery` return the recorded record worker-side.

**Release.** `release` removes, descriptor-relative without following links and idempotently, also on the worker-side path: the journal, the sparse index, the stdout and stderr logs, `runner.log` and `runner.crash`, `checkpoint.json`, the `control/launch/<id>` directories, `attempt.token`, the socket file, any temporary `transfer_in` file left by a runner crash, any temporary registry or checkpoint file of the attempt, and the placement directories (section 3). The registry entry, now a tombstone with `released_at`, and its lock file stay. A completed publication also keeps its one immutable result file and registry digest/size/operation binding under the following contract; no result file is retained for an attempt without a completed publication. Before deleting the checkpoint, durably retain every settled `PublicationOutcome` state, operation_id and failure code, including recorded null values, in the tombstone. Recovery returns those recorded values and never infers an outcome from file absence; unresolved publication still prohibits release.

**Linux service managers, sessions and containers.** Each runner is a plain detached session and the subreaper of its own candidates; kora starts no systemd unit. A systemd service that stops its whole control group on restart would stop the runners with the worker, so worker start-up refuses, and `kora doctor` reports with the fix, a worker service with `KillMode=control-group` or `KillMode=mixed` (the fix is `KillMode=process`), a worker inside a unit of a user manager that does not linger (logout stops the manager and its whole cgroup; the fix is `loginctl enable-linger`), a session with `KillUserProcesses=yes` (read from logind), and `PrivateTmp=yes`. Because runners and candidates stay in the worker unit's cgroup, worker start-up also refuses `OOMPolicy=kill` and `ManagedOOMMemoryPressure=kill` or `ManagedOOMSwap=kill`, which kill the whole cgroup, `kora doctor` reports such a setting on an ancestor unit or slice, and the unit's `TasksMax`, `MemoryMax` and CPU limits are shared with every candidate, which `kora doctor` reports, together with `OOMPolicy=stop`, the default, under which a candidate's OOM kill stops the worker unit, and `NotifyAccess=all`, under which a candidate can send service notifications for the worker's unit. A user manager that does not linger puts the socket directory in `/tmp/kora-<uid>`, which `kora doctor` reports; the `running` entry records the socket path, so a later change of lingering does not move a live runner's socket. A worker that is PID 1 of a container cannot keep runners across a restart, which `kora doctor` reports. The runner work item verifies all of these on Linux CI.

**Diagnostics.** The runner and the launcher are entry points: they configure diagnostics through the shared bootstrap onto `runner.log`, under components `runtime.local.runner` and `runtime.local.launcher`, with attempt and execution-domain context. Lifecycle outcomes (spawn, adopt, completion, retire) are INFO; refused authentications (counted), lost runners and fallback decisions are WARNING; per-record and per-frame activity is DEBUG only. A secret, a launch specification, an environment or a path below the state root never appears in diagnostics.

### Publication result retention and transport

The [VcsRef revision-1 result-ownership contract](artifacts/README.md#5-upload-replay-and-result-ownership) applies to `finalize`, `PublicationOutcome.ref` and `RecoveryEvidence.publication_outcome.ref`. A completed publication outcome is distinct from the registry's `completed` state. Repeat `finalize` only while the operations-by-state table permits it; after completion, retirement or release fences that call, read the exact outcome through `recovery`. Later worker enrichment durably freezes a separate value before its first upload and never replaces the runtime result. The authenticated supervisor summary derives and freezes the bounded top-level `runtime_result_fields` set, complete digest and saved contract together from the validated runtime result, for independent artifact acceptance and terminal binding.

**Retained result.** The original runtime domain owns at most one immutable push-result file per attempt, containing exactly the canonical VcsRef bytes, at most 1 MiB. It lives in private runtime state outside candidate-visible roots, opened descriptor-relative without following links. Its name derives only from trusted attempt identity, never a payload path. The existing registry records its operation identity, exact size and digest within the unchanged 64 KiB plus 512 bytes per grant bound. Result bytes are separate from that bound and survive retirement, release and restart indefinitely with the tombstone and lock. This is intentional retained history of at most 1 MiB per completed publication, not a bound on total state-root storage.

Durably record enough operation, dispatch and principal identity to validate the result before creating it. Under the existing serialization, write and sync one temporary file, publish its final immutable name without replacement, sync the parent directory, then atomically persist and sync the registry binding before acknowledging completion. Keep at most one final file and one temporary write per attempt. Remove failed temporary writes and sweep crash leftovers before retrying, preserving a valid unbound final result. Recovery checks the saved operation/dispatch/principal identity, byte count and digest and adopts the exact valid orphan left before registry completion; it never deletes it to regenerate values. Missing, corrupt or conflicting bytes remain unresolved infrastructure failure, never permission for a new publication. Preserve the full-disk failure policy and the rule that stop escalation never waits for writes or locks.

Keep one current or unresolved dispatch principal snapshot. A permitted retry may replace it only after authoritative absence and closure of every prior dispatch; control-plane issuance history retains its existing audit policy. Completed principal values remain in the immutable result. Credential values never enter these records.

**Publication-bearing responses.** The 1 MiB encoded-frame ceiling is unchanged. Partition finalize, adoption and recovery responses into a closed metadata frame, a publication-content section and separately streamed dispatcher evidence when present. `AdoptedSession.prepared` content is the canonical object of `repo`, `branch`, `commit`, `base_commit`, `changed_paths` and `diff_stat`; completed content is the exact canonical VcsRef. Each section is at most 1 MiB and travels in byte frames of at most 64 KiB, bound to the same authenticated request and operation. Metadata carries exact length and digest. Enforce the cumulative bound before allocation/decoding, reject extra, truncated or mismatched data, and return no partial typed result. Validate the complete push-only VcsRef with the actual authorized principal before effect dispatch; fitting prepared content alone does not prove that the completed result fits.

Dispatcher grants and closures travel in individually bounded frames or batches from one exact registry snapshot, with count, digest and completeness verification. Do not inline an unbounded grant array into metadata or truncate it to an arbitrary total-grant cap. Use backpressure, one in-flight response per connection and the existing finite operation deadlines. Disconnect/cancellation closes owned transfer resources; replay reads the same saved result and creates no effect. The local-runtime implementation defines the concrete encoding and conformance cases within these bounds.

This section bounds publication content and individual control frames; it does not establish bounded progress through arbitrarily large retained dispatcher history. The resumable grant-history contract in #226 is a prerequisite for the affected runtime, worker and API consumers and their DTOs. It must preserve every retained grant, current all-grant closure checks and the existing frame, request-body and per-operation limits. No complete-history transfer or recovery-progress guarantee is established by publishing the VcsRef contract.

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
    config_schema: JsonObject       # deployment config incl. `credentials: {ANTHROPIC_API_KEY: "secret://…"}` (DD-36) and `quota`: `resource`, the provider account or organisation whose limits apply; `groups`, group id to the model patterns it covers (`*` for account-wide; a model may fall in several groups); `vendor_groups`, the vendor limit-group names mapped to those ids; `release_batch` and `release_interval` (ADR-0024)
    egress_hosts: tuple[str, ...]   # hosts the deployment configuration needs (model API or gateway base URL); the runtime adds them to the allowlist and the resolved config shows the result (ADR-0009)
    tested_versions: str            # PEP 440 specifier of installed harness versions this adapter was tested with (design review finding: harness version health check)
    reserved_env: tuple[str, ...]   # environment names and `PREFIX_*` prefixes the adapter owns; a node `env` or `secret_env` entry matching one fails validation (`IR026`, ir.md section 10)
    def capabilities(self) -> HarnessCapabilities: ...
    def check_config(self, config: JsonObject) -> list[ConfigWarning]: ...   # compile-time: fields it cannot honour (IR008); never compares versions (DD-14)
    def build_session_spec(self, config: JsonObject, workspace: WorkspaceTarget, feedback: Feedback | None) -> SessionSpec: ...
        # everything the runner needs: SDK options (agents and the bundled output contract inline), the files to materialise
        # (the MCP configuration and the skills plugin), prompt, resume handle;
        # built by the worker before the checkout plan runs, so it sees the workspace root (section 4), not the commit;
        # slice 1 passes feedback=None, and the slice-1 protocol class types the parameter as None until Feedback has fields
    async def run(self, spec: SessionSpec, policy: PolicyClient, sink: EventSink, launches: LaunchContext) -> SessionResult: ...
        # executed by the runner inside the runtime: allocates each harness launch through `launches` and opens the SDK session, emits each mapped SDK message as a NormalisedEvent
        # (session_started | turn | tool_call | tool_result | permission_denied | usage | provider_limited | provider_retrying | result | error)
        # and reports every dropped message with sink.observe_dropped; the supervisor alone writes lifecycle `phase` results (sections 2 and 4);
        # routes pre-tool-call hooks to `policy`, honours sink.enforcement() and awaits sink.termination() beside the SDK stream
        # (max_events, budget, journal_unavailable, and stall and deadline from the runner's own timers)
    def finalise(self, summary: EventSummary, outcome: RunnerOutcome) -> SessionResult: ...
        # AgentSession fields, StructuredOutput payload, usage records with completeness, stop reason, error class
    def installed_version(self) -> str: ...     # reported by the SDK at connect and in its init message, checked against the trusted values of the installation; recorded on AgentSession, never compared with a node (DD-14)

@dataclass(frozen=True)
class SessionFile:
    name: str                       # no `/`, `..` or control characters; a skill's directory name
    digest: str                     # file digest, or a skill directory's resolved content digest
    placement: Literal["mcp_config", "skill"]
    storage_ref: str | None         # content source of a skill directory

Placement = Literal["harness_home", "harness_config", "harness_tmp", "harness_input"]

@dataclass(frozen=True)
class LaunchInputs:
    environment: Mapping[str, str]  # the adapter's own variables for this launch; a name another owner assigns is an error
    cwd: Path                       # the candidate's working directory, also given as `PWD`

@dataclass(frozen=True)
class LaunchEnd:
    cause: RunnerCause | None       # `launch_failed`, `integrity_mismatch`, `cli_exit_without_result` or `launcher_died`; None after a result or a runner-started stop
    exit_code: int | None
    signal: int | None

class Launch(Protocol):
    cli_path: Path                  # the per-launch launcher the adapter passes to the SDK
    async def kill(self) -> None: ...           # the kill handle of this launch; signals the verified leader identity, and its group only once the leader is verified to lead its own group (pgid equal to pid), under "Kernel attestation and signalling" in section 2; once the SDK has reaped the leader it signals nothing, and the domain stop covers the rest of the group
    async def ended(self) -> LaunchEnd: ...     # completes when the launch's leader has exited

class LaunchContext(Protocol):
    placements: Mapping[Placement, Path]        # supervisor-resolved per-attempt directories
    async def allocate(self, inputs: LaunchInputs) -> Launch: ...   # one launch; raises KoraProtocolError with `launch_rejected`

@dataclass(frozen=True)
class SessionSpec:
    harness: str
    adapter_version: str
    options: JsonObject             # a closed per-adapter model, validated by the adapter
    files: tuple[SessionFile, ...]
    prompt: str
    resume_handle: str | None
    price_table_id: str
    price_table: JsonObject

@dataclass(frozen=True)
class NormalisedEvent:
    i: int
    at: datetime
    kind: Literal["session_started", "turn", "tool_call", "tool_result", "permission_denied", "usage", "provider_limited", "provider_retrying", "result", "error"]
    data: JsonObject

RunnerCause = Literal["completed", "max_events", "budget", "stall", "deadline", "cancel", "provider_limit", "policy_bypass", "version_mismatch", "plugin_mismatch", "integrity_mismatch", "config_check_failed", "policy_error", "policy_timeout", "journal_unavailable", "cli_exit_without_result", "oversized_message", "launch_rejected", "launch_failed", "launcher_died"]

@dataclass(frozen=True)
class Enforcement:
    action: Literal["continue", "terminate"]
    cause: RunnerCause | None       # set when action is terminate

class EventSink(Protocol):
    def emit(self, kind: str, data: JsonObject) -> None: ...      # never blocks; raises KoraProtocolError for a kind it does not accept
    def observe_dropped(self, kind: str) -> None: ...             # liveness and bounded counters, no journal write
    def enforcement(self) -> Enforcement: ...                     # non-blocking in-memory read
    async def termination(self) -> RunnerCause: ...               # completes when a runner timer or check decides to stop

class PolicyClient(Protocol):
    async def decide(self, request: ToolCallRequest, layers: PolicyLayers, protected_paths: tuple[str, ...]) -> PolicyDecision: ...

@dataclass(frozen=True)
class RunnerOutcome:
    cause: RunnerCause
    termination: Literal["none", "interrupted", "killed", "killed_after_grace", "teardown_after_result"]
    exit_code: int | None
    signal: int | None

@dataclass(frozen=True)
class OutputCandidate:
    source: Literal["structured_output", "result_text"]
    value: JsonValue

@dataclass(frozen=True)
class UsageRow:
    model: str
    input_tokens: int
    cached_input_tokens: int
    cache_creation_tokens: int
    output_tokens: int
    reasoning_tokens: int
    known_token_fields: frozenset[str]
    cache_creation_by_ttl: Mapping[Literal["5m", "1h"], int] | None
    reported_usd: str | None
    observation_id: str
    observation_sequence: int
    observed_at: datetime
    completeness: Literal["complete", "partial", "unavailable"]
    basis: Literal["harness_list", "harness_managed", "price_table", "gateway"]   # the usage_record basis of data-model section 2
    billing_class: Literal["api_key", "subscription", "gateway", "managed"]
    delta_of_session_totals: bool
    source: JsonObject              # the raw harness entry, at most 8 KiB

@dataclass(frozen=True)
class SessionResult:
    session_id: str | None
    installed_version: str | None
    billing_class: str | None
    stop_reason: str
    terminal_reason: str | None
    api_error_status: int | None
    error_class: str | None
    error_summary: JsonObject | None    # {source, error_count, contract_digest, code}, content-free
    output: OutputCandidate | None
    output_diagnostic: str | None        # content-free code
    usage: tuple[UsageRow, ...]
    usage_coverage: UsageCoverage
    turns: int
    denials: JsonObject                  # capped sample with a count
    denials_complete: bool
    unconfirmed_calls: JsonObject        # capped sample with a count
    policy_bypass: bool
```

`UsageRow`, `UsageCoverage` and live monetary fields follow [data-model section 7](data-model.md#7-usage-records-and-completeness-fr-91). `known_token_fields` is a closed set of the five token column names; omitted dimensions remain unknown even when their observed lower bound is zero. Observation identity/order is stable within the attempt and preserved by relay/replay, including across grants; it cannot be regenerated from a heartbeat receipt. Unknown-model observations set an attribution gap instead of creating a synthetic row. Monetary strings use the exact decimal contract; the runner derives pricing coverage, and the engine derives persisted USD. The attempt-level coverage survives an empty `usage` tuple; its independent identity/sequence, initial null state and replay/refusal rules are defined in data-model section 7.2 and also apply to `Liveness`. Polls retain the source revision instead of generating new accounting evidence. A final result alone cannot clear an observation gap. The shared closed models, including bounds and validation, belong to #213 and #156.

`EventSummary` is the adapter's incremental state that `finalise` reads: the harness's parsed result fields (`ResultMessage` on `claude`), per-call usage accumulators by message id, the count of distinct message ids, decided `tool_use_id`s, held error results, harness-denial entries with the count of unmatched ones, whether usage arrived after the first result, and the output-channel decision. Every collection in it is capped by `max_events` or by a fixed limit below, with an overflow count. Usage is not a counted `max_events` kind: its identity/order state must remain bounded independently, using current cumulative model totals, at most 1,024 open messages and bounded source high-water state, never an ever-growing id set. Eviction cannot permit recounting; loss of exact deduplication preserves a gap. `ConfigWarning` and `ContractViolation` (section 12) are aliases of the [shared `Diagnostic`](diagnostics.md), owned by `kora.ir`, with the same source/path/offset and safe-message rules. `check_config` returns configuration-relative warning paths, with `IR008` for unsupported fields; its consumer rebases them before global ordering. The shared per-call list bounds apply before accumulation; overflow raises the fixed `diagnostics_limit` protocol error and returns no partial success list. The runner validates `SessionResult.output` against the bound `ArtifactType` before returning, so it produces `artifact_invalid` itself; the worker adds duration and provenance (issue #139), and issue #138 maps the result into `AgentSession`. The result's harness identity comes from `SessionSpec.harness` and the worker's provenance, so it is `fake` under the testing composition (issue #136).

| `RunnerOutcome.cause` | meaning | class |
|---|---|---|
| `completed` | the session finished by itself | from the section 14 result mapping |
| `max_events`, `budget`, `stall`, `deadline` | a limit or the inherited deadline stopped it | `events_exhausted`, `budget_exhausted`, `stalled`, `timeout` |
| `cancel` | it was cancelled | `cancelled` |
| `provider_limit` | a provider limit stopped it | `provider_limited` |
| `policy_bypass` | a confirmed policy bypass stopped it | `harness_error` |
| `version_mismatch`, `plugin_mismatch` | the installed harness version or built-in plugin list did not match the trusted values | `config_error` |
| `integrity_mismatch` | the binary digest did not match; a skill directory digest mismatch keeps the open class question below | `integrity_error` |
| `config_check_failed` | a check after connect failed: a `claudeai` server, or a repository MCP server named like a node server | `config_error` |
| `policy_error`, `policy_timeout` | `decide` raised, or timed out | `config_error`, `infrastructure` |
| `journal_unavailable` | the journal writer failed or stayed behind past its bound | `infrastructure` |
| `cli_exit_without_result`, `oversized_message` | the CLI exited by itself without a result, or a message exceeded its bound | `harness_error` |
| `launch_rejected` | a pre-launch check of the runner failed, such as an output schema over the platform's argument limit | `config_error` |
| `launch_failed`, `launcher_died` | the launcher failed to start or died | `infrastructure` |

The class of a cause holds also when the stop escalated to a kill; the precedence rules under Finalisation below decide between causes.

**Session events (issue #134).** Each `NormalisedEvent` kind has a fixed `data` shape and a content class. Fields marked `?` may be absent.

| kind | data | content class |
|---|---|---|
| `session_started` | `{session_id, harness_version, billing_class, plugins_digest, mcp_sources}` | content-free |
| `turn` | `{message_id, agent_id?, text?, truncated}` | bounded content |
| `tool_call` | `{tool, tool_use_id, agent_id?, output_channel}` | never content: never carries tool input |
| `tool_result` | `{tool_use_id, is_error, content?, truncated}` | bounded content |
| `permission_denied` | `{tool, tool_use_id, source: harness \| policy, pattern?, code}` | content-free |
| `usage` | `{observation_id, observation_sequence, observed_at, message_id, agent_id?, model?, input_tokens, cached_input_tokens, cache_creation_tokens, output_tokens, reasoning_tokens, known_token_fields, cache_creation_by_ttl?, reported_usd?, basis, billing_class, coverage: UsageCoverage}` | content-free |
| `provider_limited` | `{retry_after, scope: rate \| quota, vendor_group, model, binding}` | content-free |
| `provider_retrying` | `{model, retry_number, retry_after?}` | content-free |
| `result` | `{subtype, is_error, terminal_reason}` | content-free |
| `error` | `{code, text?, truncated}` | bounded content |

- **Bounded content** (turn text, tool-result content, `error` diagnostic text and command `output` lines) is redacted before journal append and upload, cut with the truncation marker, and stored in `session_segment` under the `transcripts` capability and payload retention; the journal holds the same truncated content and nothing more. Audit `event` rows promoted from `permission_denied` never carry content, and `PolicyDecision.reason` is a code or a pattern id, never echoed arguments.
- **Bounds.** These values are proposals the owner approved under issue #134; the runner work item enforces them.
  - A whole record, envelope `{i, at, kind}` included, is at most 64 KiB after canonical JSON encoding, below the 1 MiB control-channel frame payload.
  - Identifiers from the candidate (`tool_use_id`, `agent_id`, `session_id`, `message_id`, `model`, `vendor_group`) are at most 256 printable characters.
  - At most 100 `error` events per attempt, then one `runner.cap_reached` marker with the omitted count; `error` is not a `max_events` kind.
  - A command `output` line is at most 4 KiB, and an attempt carries at most 10,000 `output` lines or 4 MiB, whichever is reached first, then one `runner.cap_reached` marker with the omitted line count; the full streams stay only in the stdout and stderr logs that `CommandResult` references ([ir.md section 3](ir.md#3-nodes)).
  - Every capped sample (denials, unconfirmed calls, held error results) keeps at most 1,000 entries plus a count. A held error result that overflows its sample cannot be classified against `permission_denials`, so it fails closed: it counts as a confirmed policy bypass, also when no `ResultMessage` arrives. Usage accumulators are kept only for message ids still open, at most 1,024, with totals for at most 64 distinct models; an overflow of either marks usage incomplete.
  - The runner tracks at most 100,000 limited events per attempt, also when `max_events` is absent; reaching that ends the session with cause `max_events`, class `events_exhausted`. That bounds the decided `tool_use_id` set by the ceiling plus the calls denied during the stop grace. `max_events` has `maximum: 100000` in `ir.schema.json`.
  - The byte caps of the stdout and stderr logs and of the journal file, with their `runner.cap_reached` record, are in section 2, "Local runtime".
- **Accepted kinds.** From a harness adapter, `EventSink.emit` accepts exactly the ten `NormalisedEvent` kinds; `phase`, every supervisor kind of section 2, the command kinds and the runner markers raise `KoraProtocolError`. The command workload's emitter accepts only `output` and `command_exited`.
- **`last_event_at`** advances in memory with every harness `NormalisedEvent` and every SDK message dropped from the stream, with no journal write for dropped messages. Supervisor records and command `output` never advance it, so a worker re-attach does not reset the stall clock. It advances when the adapter consumes a message, and consumption never pauses for the journal, so journal slowness never looks like a stall. The runner evaluates `stall_after` on its own timer, independently of adapter polling, with the open-call extension of [scheduler section 4](scheduler.md#4-heartbeats-stall-detection-and-timeouts) (an unanswered `tool_use_id`), capped by the inherited deadline; `enforcement()` is never the stall or deadline trigger. A runner timer or check that decides to stop completes `sink.termination()` with its cause, which the adapter awaits beside the SDK stream and answers with the interrupt, so a silent stream still stops. When writer health and a stall both hold, `journal_unavailable` takes precedence.
- **`EventSink` and journal back-pressure.** The adapter never pauses SDK consumption, because the SDK's single reader also routes hook control requests, so a paused stream would delay policy callbacks and interrupts (read from the SDK 0.2.168 source, confirmed in work item 15). `emit` never blocks, from the stream or from a policy callback: it updates the in-memory counters and `last_event_at`, then hands the record to the one journal writer through a bounded queue of 4 MiB and 4,096 records, whichever is reached first. A record that does not fit is counted at once into one `runner.journal_gap` marker (section 2), which the writer appends when space returns; an attempt with a gap never reports a complete journal, and records are never dropped silently. Counters and `enforcement()` stay exact, because they are kept before the queue. Supervisor records and the normal-completion quiescence record never take this path: they are written synchronously, and the supervisor acknowledges a phase only after its record is durable, so a gap covers only adapter records. The runner evaluates writer health on its own timer: when the queue stays full for 30 s, or the writer reports an error, it stops the session with cause `journal_unavailable`, class `infrastructure`, through `termination()`, and `enforcement()` reports the same. Interrupt and kill escalation never wait on the journal writer (ADR-0022); where stop evidence lands while the journal is unavailable, and the durability classes, are in section 2, "Local runtime".
- **The adapter's closed exceptions,** in this order, before it calls `PolicyClient`: (1) once `max_events` is reached, deny every call, `StructuredOutput` included, without emitting it as a counted `tool_call`; (2) otherwise allow `StructuredOutput` as the output channel, only under its exact name, with `output_format` passed and with `agent_id` absent. Both are recorded as decided `tool_use_id`s for bypass classification, and the output-channel decision is recorded as `tool_call.output_channel: true`. An exception or timeout in `decide` denies the call and fails the attempt: an exception is deterministic, so it ends with cause `policy_error`, class `config_error`, and is never retried without end; a timeout ends with cause `policy_timeout`, class `infrastructure`, under the runtime-level retry bound.
- **Finalisation.** `run()` returns `SessionResult` on every path, built with `finalise(summary, outcome)`. The first cause the runner acted on is recorded and decides `termination`: `interrupted` when the interrupt ended the session, `killed_after_grace` when the kill followed the grace, `killed` when the runner killed at once without an interrupt (after an oversized message stopped the SDK reader), and `teardown_after_result` for the teardown after a parsed result; a stop that escalated to a kill keeps its cause's class (section 14). The cause feeds the class only through the two section 14 legs that name it: the class of the interrupt's cause, and the cause's class after a runner kill when no `ResultMessage` exists. The provider-limit row takes precedence as section 14 states, a confirmed policy bypass is `harness_error` over every limit cause, `provider_limit` included, because a bypass is a security event, and a held error result left unconfirmed because no `ResultMessage` arrived keeps the class of the end. Memory is independent of uncounted message volume and bounded by the caps above.
- **`SessionSpec`.** `options` is a closed per-adapter model carrying the subagents and the bundled output contract inline, `max_turns` and `max_budget_usd`, all derived from the resolved config and `Workload.limits`. It rejects environment, credentials and callbacks, `cli_path` and launcher fields, and `setting_sources`, `permission_mode`, `strict_mcp_config` and `stderr`, which the runner sets itself from `Workload.resolved_config` and its own constants. `files` lists only what is written as files: the MCP configuration file, and each skill directory of the skills plugin by name, resolved content digest and content source. Files are created with descriptor-relative no-follow creation; MCP configuration files hold secret references, never values, so no recorded digest is secret-derived. `prompt` must equal the sealed rendered text, checked by its digest. The `price_table_id` and complete `price_table` must equal the engine-selected snapshot in the sealed execution request, including missing prices; neither the adapter nor runner fetches replacements. The spec is checkpoint-bound, with its own digest recorded before it is applied, and is not itself part of the logical request digest; the logical request already binds the same price revision and values. The launcher, its directories and the kill handle are specified under "Harness launcher (issue #123)" below. The runner checks an output schema against the candidate platform's argument limit before launch, because `check_config` only returns compile-time warnings; an oversized schema fails the attempt with cause `launch_rejected`, class `config_error`, and nothing is launched.

Before starting a harness session, the runner verifies each materialised skill directory against its saved resolved content digest. A mismatch prevents session start; it must not silently use changed content or refresh the pin. Harness auto-updaters are disabled in installation images. These are the accepted supply-chain controls from the October 15 review, not a resource-delivery design: skill transport and the digest-mismatch error-class mapping remain [open questions](../architecture/detailed.md#open-implementation-questions). The conformance case `test_skill_directory_digest_mismatch_prevents_session_start` changes materialised content while retaining its pin and proves that the harness does not start.

The fake harness never parses an SDK stream: it reads one ordered script of steps and never chooses an enforced outcome, and it is constructed by injection, never discovered ([fakes.md section 4](fakes.md#4-the-fake-harness)); replaying recorded SDK streams is each real adapter's replay layer (section 13). Adapters are importable in supervisor installations; their harness subprocesses launch through the runtime boundary (section 2).

**First-slice defaults** (DD-36). The normalised event is `{i, at, kind, data}`; `max_events` counts `turn`, `tool_call` and `tool_result` events; termination is interrupt, 30 s grace, then kill; for timeout the nominal thresholds derive from the immutable inherited deadline and its fixed `D + 30s` escalation, not a delayed observation (ADR-0022). Each adapter selects the candidate output payload or reports that none exists; whether a selected payload is valid is decided only by the node's effective output contract (its type closure and inline schema, [data-model.md](data-model.md) section 11.1, [ir.md](ir.md) section 2.1), through the same bound `ArtifactType` implementation (section 8) that validates the artifact on upload, so the runner and the API cannot disagree. A payload taken from text must be exactly one strict RFC 8259 JSON value: only JSON whitespace around it, no code fences, no extraction from prose, no duplicate member names, and every number finite, so `NaN`, `Infinity` and an overflowing `1e400` are rejected; a payload from a structured channel must have finite numbers too. Usage maps per model into `usage_record` rows with completeness; the `AgentSession` payload fields are listed in [ir.md](ir.md) section 3.

**The `claude` adapter in the first slice** (issue #125). These defaults rest on the Claude Agent SDK 0.2.168 with its bundled CLI 2.1.301 as observed on 2026-10-19 ([probe record](../research/claude-agent-sdk-probe-2026-10-19.md)). Statements read from the SDK 0.2.168 source are marked *SDK source*, statements from the Claude Code documentation *documented* ([documented controls](../research/claude-code-documented-controls-2026-10-19.md)), and behaviour no run has shown *not yet observed*; the [Claude adapter contracts](#claude-adapter-contracts-issue-125) check every item per tested version.

- **Process.** The adapter opens one `ClaudeSDKClient` per session and sends one query. The first `ResultMessage` ends the session: the runner calls `disconnect()`, whose own teardown closes the CLI's input, waits about 5 s, then signals the launcher with SIGTERM and after about 5 s more with SIGKILL (SDK source); a signal from that teardown after a parsed result is teardown, not launcher death, so the result's class and usage stand, and the runtime then stops the rest of the candidate domain with the domain stop of its recorded receipt scope (section 2, "Honest receipts"). After that result, later messages only advance a bounded per-type counter; per-call usage among them is still added to the rows, which then become `partial`. The constructed environment disables background tasks (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, documented, not yet observed). The adapter registers no in-process SDK MCP server, because its tools would run with supervisor authority, and its callbacks only return decisions and record observations. It always sets the SDK's `stderr` callback, so CLI stderr reaches a bounded, framed channel and never the supervisor's stderr ([observability](observability.md) sections 5 and 6). A message over the SDK's per-message limit (`max_buffer_size`, 1 MiB, SDK source) stops the SDK's reader; the runner then kills through the kill handle (below), and the session ends with `harness_error`. Teardown runs in a supervisor-owned task that cancelling the session cannot cancel and that has its own bound; at escalation the adapter cancels its own outstanding control requests (initialize, interrupt, MCP status), and no escalation waits on an SDK call. The transcript is the CLI's own session file, so session persistence stays on; the supervisor collects it from the candidate-writable config directory before removing that directory, reading descriptor-relative without following links, only a regular file and only up to a size bound, recording any truncation, and otherwise records the transcript as unavailable. That stream events and dropped frames never enter that file is not yet observed.
- **Binary and version.** The adapter always sets `cli_path` to the runtime-supplied launcher ("Harness launcher (issue #123)" below), which starts, in the candidate domain, the CLI that the pinned `claude-agent-sdk` wheel bundles for the candidate's platform. Without `cli_path` the SDK would start its bundled binary itself on the supervisor side, or `claude` from `PATH` when no bundled binary exists; neither happens. Pinning the SDK therefore pins the harness: `tested_versions` is a range of CLI versions, and worker start and `kora doctor` check it against the version the supervisor installation's pinned SDK records for its bundled CLI (`__cli_version__`, a private module whose location the adapter's tests pin). Before launch the supervisor hashes the binary it will start and compares it with the `RECORD` entry of the candidate-platform wheel whose hash `uv.lock` pins; a mismatch prevents the start with `integrity_error`, and the digest is recorded on the attempt. On protected runtimes the binary is read-only and outside the candidate root; on `local` same-user code can rewrite the binary and its record alike, so the check there detects drift, not tampering. The `claude_code_version` that `get_server_info()` reports right after connect, and the init message repeats, is a candidate observation: when it differs from the trusted version, the session fails `config_error` before the prompt, and so does a built-in `plugins` list that differs from the reviewed set for the tested version, because vendor feature gates can change it within a pinned version; the list is checked as soon as the CLI reports it, and if that is only the init message of the first turn, the runner interrupts the session there (which applies is not yet observed). `kora-runner` runs with `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK` set, so the SDK's own `-v` probe (SDK source) never starts a candidate process. The SDK's `Transport` class is not replaced, because the SDK documents it as an unstable internal interface.
- **Launch inputs and environment.** The SDK starts its subprocess with the supervisor's entire environment plus `options.env` and cannot remove an inherited variable. The launcher therefore copies nothing it inherits: it takes from the SDK only the arguments after its own name and executes them verbatim without a shell, and it takes the binary, the cwd (the candidate's working directory, also given as `PWD`) and the complete environment from a launch specification that reaches it over the supervisor-private channel; it refuses to start without one and passes only descriptors 0, 1 and 2. That environment is exactly:
  - the workload's `env`, resolved `secret_refs` and `harness_credentials` (section 2, DD-30);
  - the variables section 2 assigns to the runtime for the candidate domain: on `local` at least `PATH` and the token the runtime uses to find escaped descendants, and the proxy settings of a runtime that enforces network policy;
  - the adapter's own `CLAUDE_CONFIG_DIR`, `HOME`, `TMPDIR`, `SHELL` (the image's `bash`), `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, the retry ceiling `CLAUDE_CODE_MAX_RETRIES` of the provider-limit paragraph below, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, or `DISABLE_AUTOUPDATER=1` in its place when the operator enables CLI telemetry in the adapter's deployment configuration (all documented, none yet observed);
  - the variables the pinned SDK version sets for its CLI, which the adapter writes into the launch specification: for 0.2.168 `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AGENT_SDK_VERSION` and `CLAUDE_CODE_SDK_READS_SESSION_STATE` (SDK source).

  Trace context (`TRACEPARENT`, `TRACESTATE`) is not passed. A node `env` or `secret_env` entry matching `reserved_env` fails validation with `IR026`; for `claude` that covers every name above and every variable that changes the harness's destination, trust or start-up: `ANTHROPIC_*`, `CLAUDE_*`, `KORA_*`, `NODE_*`, `BUN_*`, `BASH_FUNC_*`, `LD_*` and `DYLD_*`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` in any letter case, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `BASH_ENV` and `ENV`. The Bash tool runs every command inside a snapshot of the login shell that `SHELL` and `HOME` select, also under `setting_sources=[]`; the per-attempt home keeps the invoking account's shell configuration and exported variables out of it (not yet observed). On `local` this is hygiene, not isolation: same-user code can still read the account's files, the supervisor's process environment and the keychain. Host managed settings and the system shell profile are trusted installation inputs that hermetic mode does not exclude; images ship none. No secret travels in argv: MCP servers are passed as a configuration file, not as inline JSON, and the launcher's argv and environment are never logged; an output schema too large for the platform's argument limit fails before launch with cause `launch_rejected`, class `config_error` (above).
- **Per-attempt directories.** Trusted preparation creates four fresh, empty per-attempt directories outside `WorkspaceTarget.root` and every trusted root, with descriptor-relative no-follow operations, refusing a path that already exists or is a symlink. Three are candidate-writable: `CLAUDE_CONFIG_DIR`, `HOME` and `TMPDIR`. The fourth, the harness input directory, is read-only to the candidate on protected runtimes, like `resource_root`, and holds what kora writes for the launch: the MCP configuration file and the skills plugin, whose skill directories the runner verifies against their pinned digests (above). The directories are never reused and never part of checkout, export or publication; the supervisor removes them without following links once the transcript is collected, at the latest at runtime `release`. Neither a branch nor an earlier attempt can therefore pre-seed shell profiles, Git configuration, settings, memory or a symlink to an account's configuration through them; on `local` a surviving same-user process of an earlier attempt still can (section 2). They are the placements `harness_config`, `harness_home`, `harness_tmp` and `harness_input`; the runtime allocates them and the adapter receives their paths through `LaunchContext.placements` in `run`, so `build_session_spec` sees none of them ("Harness launcher (issue #123)" below). On Docker the allocation remains an [open implementation question](../architecture/detailed.md#open-implementation-questions). On the MVP runtimes a stored `resume` handle points at a checkpoint that no longer exists once the previous attempt was released (section 2).
- **Hermetic configuration.** `trust_repo_config: false` sets `setting_sources=[]` and `strict_mcp_config=True`: the repository's instruction files, settings, hooks, permission rules, `.mcp.json`, skills, subagents and commands are not loaded, and the session sees only the MCP servers kora passes. Claude Code reloads settings files that change or appear during a session (documented); whether `setting_sources=[]` also excludes a settings file the candidate writes into the config directory or the repository mid-session is not yet observed. `true` sets `setting_sources=["project"]` without `strict_mcp_config`, so `.mcp.json` loads as FR-3.4 requires; a repository server with the name of a node-declared server fails `config_error` after connect. Under `true` the repository's configuration, including project settings the agent itself writes during the session, is trusted code outside the policy (section 12). `setting_sources` is never left unset, because unset loads every source, including the invoking account's settings. The MCP connectors of a claude.ai account load regardless of `setting_sources`; the probe removed them with `strict_mcp_config`, and `ENABLE_CLAUDEAI_MCP_SERVERS=false` is documented to remove them in both modes (not yet observed). Because project settings can set environment variables under `true`, a server with `source: claudeai` in the init message or `get_mcp_status()` fails `config_error` after connect in either mode; the check can fire only under a claude.ai login. Claude Code's built-in plugins load in both modes and are checked as stated above.
- **System prompt, permission mode and hooks.** `system_prompt` is always the `claude_code` preset, with `append` when the node sets `system_prompt_append`; left unset, the SDK passes an empty system prompt. `permission_mode` is always passed, the resolved value or `dont_ask` when no layer sets one, because left unset the CLI picks a mode from its configuration directory (`auto` in a fresh one). A node's `hooks` are not passed and `check_config` warns `IR008`: supervisor-side SDK callbacks would run author code with supervisor authority, and command hooks in the candidate domain could return `updatedInput` or a permission decision outside the namespace policy; how node hooks reach the harness is an [open implementation question](../architecture/detailed.md#open-implementation-questions).
- **Event mapping.** `include_partial_messages=True` is always set, and the mapping is closed:
  - the init message becomes `session_started`;
  - assistant content becomes one `turn` per distinct message id, because the CLI sends one `AssistantMessage` per content block, and one `tool_call` per tool-use block;
  - each tool-result block becomes one `tool_result`, and the `ResultMessage` becomes `result`;
  - the `message_start` and last `message_delta` stream events of each API call become one `usage` event;
  - the provider-limit and retry signals of the section 14 provider-limit row become `provider_limited` and `provider_retrying` events ([ADR-0024](../../docs/decisions/0024-provider-limits-as-non-agent-failures.md)), at most `CLAUDE_CODE_MAX_RETRIES` retry events per call.

  Every other message (content deltas, thinking frames, task progress, hook events, rate-limit events that report no limit) is dropped or only updates fixed-size state in memory, the liveness timestamp for `stall_after` and the latest rate-limit status, and is never journalled per occurrence; unknown types the SDK passes through are dropped with a bounded counter, and the SDK itself discards unknown top-level types (SDK source). User text the CLI injects (the structured-output reminder, the interruption notice) and `<synthetic>` assistant messages are not counted. The adapter sets `forward_subagent_text=True`, so that a subagent's turns reach the stream and count like the main session's (SDK source, not yet observed); `StructuredOutput` calls count. The callback counts the call it decides as a `tool_call`, deduplicated with the stream by `tool_use_id`, so the limit does not depend on message order: once `max_events` is reached it denies every further call, `StructuredOutput` included, and the runner interrupts; that deny enforces the limit and is not a `permission_denied` event. `ResultMessage.num_turns` is not used: it arrives only at the end and matched the number of API calls in two of five probed sessions. The `AgentSession` turn count is kora's own count. `max_turns` is set as the additional native limit, no lower than `max_events`; if it ends a session first, the class is still `events_exhausted`.
- **Usage.** Each API call is tracked by message id: the model and the input and cache counts come from `message_start`, and the final counts come from the call's last `message_delta`, never from a sum of its deltas. `input_tokens` is the API's uncached input; cache reads become `cached_input_tokens`; cache writes become `cache_creation_tokens`; thinking tokens become `reasoning_tokens`, which `output_tokens` already includes. The 5-minute and 1-hour split of cache writes appears only in `ResultMessage.usage`, per query, and is normalized into `cache_creation_by_ttl` only when attributable to a single model and covering the same calls; main-loop-only query usage cannot supply the split for whole-tree model totals. A multi-model query never allocates that split by guesswork; equal write rates or zero writes can still permit exact pricing under data-model section 7.2. `AssistantMessage.usage` is a snapshot from the start of the call and is never summed. `ResultMessage.usage` covers one query, while `ResultMessage.model_usage` and `total_cost_usd` accumulate over the CLI process; only a fresh, one-query session without subagents makes those scopes coincide. With subagents, query `usage` covers the main loop while `model_usage` includes the whole tree; the adapter never combines unlike scopes. Final rows come from `model_usage` per model with the raw entry in `source`; `costBasis: list` becomes `basis: harness_list` with per-model `costUSD` normalized into `reported_usd` through the native SDK ingress rule of data-model section 7.3; the SDK's already numeric estimate is converted once from its shortest decimal representation, not treated as recovered wire precision; any other value is kept raw and maps to `price_table`, and rows summed from per-call events have basis `price_table`. No process-wide total is distributed among models. The init auth probe determines billing class; a subscription keeps USD null despite numeric native list costs. Presence metadata distinguishes omitted fields from zero. A validated final harness observation may correct the interim table estimate under data-model section 7.2; a post-result tail preserves partial token coverage and the last reported harness amount without inventing a hybrid monetary basis. Completeness follows [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md) and ADR-0004:
  - `complete` when a `ResultMessage` with usable usage is parsed without a retained attribution/observation gap, which in the probe also held after an interrupt that stopped a running tool (`terminal_reason: aborted_tools`);
  - `partial`, taken from that result, after an interrupt during streaming (`aborted_streaming`), because the call in flight may be missing (not yet observed), and when per-call usage arrives after the first result;
  - `partial`, summed from per-call events as a lower bound that includes the input counts of a call in flight, whenever no usable result exists: after a runner kill, the CLI's own exit, the launcher's death, an oversized message, or an empty `model_usage` after per-call events;
  - `unavailable` only when neither exists.

  Live estimates use the attempt's saved price map and source-observation time, under [data-model section 7.3](data-model.md#73-live-observations-and-bounds). Missing required rates or dimensions leave total USD null with a separately labelled known subtotal; subscription usage is not billable in USD. [Issue #140](https://github.com/realkarych/kora/issues/140) owns budget activation, native-limit configuration and pre-launch refusal for unsupported configured budgets; no disabled check silently accepts one. Usage across `resume`, subagent streaming and `aborted_streaming` remain unobserved. The MVP reference runtimes declare `supports_resume = False`. Dated source evidence and its limits are in [usage-pricing sources](../research/usage-pricing-sources-2026-10-23.md).
- **Termination.** The interrupt is `ClaudeSDKClient.interrupt()`: in the probe a running tool stopped within milliseconds and the query still ended with a `ResultMessage` carrying usage. Escalation fires at its fixed threshold whether or not `interrupt()` has returned, because its control request can wait up to 60 s (SDK source). The kill goes through the kill handle the runtime provides for that launch, never through the SDK's process object or the pid the CLI reports; `disconnect()` follows once the launcher has exited and releases the SDK's pipes and tasks, so its own escalation of up to about 20 s stays off the deadline path. Before a result is parsed, a signal the SDK sends to the launcher and any other death by a signal count as launcher death: the runtime terminates or retires the domain (section 2), usage is `partial`, and unless a runner-started stop caused it the attempt fails `infrastructure`. An exit of the CLI with a status before a result is `cli_exit_without_result` instead ("Harness launcher (issue #123)" below).
- **Structured output.** `output_format={type: json_schema, schema}` receives a self-contained, bundled form of the node's effective output contract; whether the CLI accepts keywords that only JSON Schema 2020-12 defines, and a root that is not an object, is not yet observed. The CLI adds a `StructuredOutput` tool, also when no other tool is enabled, validates its input, returns validation errors to the model and, when the model then answers in text, injects a reminder to call the tool (one in the probe); this enforcement is advisory, and `MAX_STRUCTURED_OUTPUT_RETRIES` is not set. The output is extracted only when the result maps to success; every other class wins, and nothing is uploaded as output. A non-null `ResultMessage.structured_output` is validated against the contract, with no fallback. When it is `null`, which `subtype: success` reports when the schema was not met, only `ResultMessage.result` is read, under the strict text rule above. Otherwise, and on `error_max_structured_output_retries` (a subtype found in the CLI binary, not yet observed), the attempt fails `artifact_invalid`. The diagnostic is kora's own, the bound `ArtifactType` result or the parse error, with the CLI's last validation error as an advisory observation. Because it derives from model output, the error details keep only a content-free summary (source, error count, contract digest and a code), and the bounded diagnostic text travels as an `error` session event, stored in a `session_segment` of the attempt with its retention and `transcripts` access ([ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md)); the CLI's `errors` strings are kept the same way.
- **Tool policy.** The `PreToolUse` callback is the only decision point; no `allowed_tools` or `disallowed_tools` list is derived from the policy (section 12). For a call the policy allows, the callback returns an explicit allow, which skips the permission prompt; in `plan` the mode still blocks every call that is not read-only, and under `dont_ask` a hook-approved call runs (documented, not yet observed). The adapter allows the CLI's synthetic `StructuredOutput` tool itself, without consulting the policy, only under the closed exceptions above: after `max_events` it is denied like every call, and otherwise it is allowed only when the tool name is exactly `StructuredOutput`, `output_format` was passed and the call comes from the main session rather than a subagent (no `agent_id`); the decision is recorded as the output channel. Every other call, including `mcp__<server>__StructuredOutput`, goes to the policy. The harness can stop a call before the callback in two ways, which the probe observed separately, and the runner keeps them apart. A permission rule denies the call: it appears in `ResultMessage.permission_denials` (observed for a settings deny rule), and the runner records a `permission_denied` event with `source: harness` and no pattern. A built-in tool's own input validation rejects the call: the CLI returns a result whose content begins with its pre-execution form `<tool_use_error>…` (observed for the Bash tool), the call never ran, and the runner records an ordinary failed `tool_result`, neither a denial nor a bypass. A `permission_denials` entry wins over that form; the form counts only for built-in tools, never for an `mcp__` tool, whose server returns its own error text, and never when it appears elsewhere in a result, for instance printed by a command; that a built-in tool's execution errors never begin with it is not yet observed. A `permission_denials` entry for a call the callback allowed is a harness-side denial as well, by a trusted repository hook, a built-in plugin hook or the permission mode. Any other tool result for a `tool_use_id` the callback never decided, other than the recorded output-channel exception, is a policy bypass, because a call that ran and then failed reports an error as well. A non-error result is a bypass at once. An error result is held and classified when the `ResultMessage` arrives, because only that message lists permission-rule denials; if the session ends without one, the attempt keeps the class of its end, and the held calls are recorded in the error details as unconfirmed, with the denial record marked incomplete. On a bypass the runner interrupts if the session still runs, the attempt fails `harness_error`, and the bypass is recorded; it is detected only while the CLI reports honestly. All these events are observations from the candidate-domain CLI: only entries matching an observed tool-use block become events, the `tool` name is bounded in length and character set, other entries are only counted, the `AgentSession` records whether its harness-side denials are complete (they are not when no `ResultMessage` arrives), and `tool_input` never enters an event.

**Provider limits** ([ADR-0024](../../docs/decisions/0024-provider-limits-as-non-agent-failures.md)). The adapter maps vendor signals to one normalised event, `provider_limited {retry_after, scope, vendor_group, model, binding}`, and to the attempt's error class. A rejected limit or a `usageLimitExceeded`/`rateLimitExceeded` error is `provider_limited`, with `retry_after` from the matching window's reset time when one is reported. Credits or billing exhaustion and account holds are `config_error`; authentication failures stay `config_error`; overload and server errors stay `harness_error`. Signals that the harness is retrying internally are normalised as `provider_retrying` events, which count as liveness for `stall_after`; the adapter sets the harness's own retry ceiling (for Claude Code, `CLAUDE_CODE_MAX_RETRIES`). The session runner interrupts a session that reports a rejected limit whose reset lies beyond `stall_after`, so it ends through the normal collection and publication lifecycle, and the attempt fails with `provider_limited`. The quota resource and groups come from the binding's `quota` configuration, trusted operator data available before admission; each adapter ships a default `vendor_groups` and `groups` for the limit groups its vendor documents (for Claude: `five_hour` and `seven_day` to an account-wide group, `seven_day_opus` and `seven_day_sonnet` to model groups), which the operator may override. The scheduler compares only declared group ids and model patterns and never interprets vendor names. A binding without `quota.resource` is its own resource. The mapping in section 14 is documented, not observed: the first slice records a trace per harness before it is relied on.

### Harness launcher (issue #123)

The runner gives the adapter a `LaunchContext`. The adapter passes `cli_path` to the SDK, uses `kill()` as the kill handle of that launch and reads how the launch ended from `ended()`. This section is the `local` realization; on Docker, where the launcher runs, how it drops to the candidate principal and the read-only input mount remain an [open implementation question](../architecture/detailed.md#open-implementation-questions). On `local` every check below is cooperative hygiene against same-user code (ADR-0016): the launch specification in transit is readable by it, as the [threat model](../../docs/threat-model.md) states.

- **`cli_path`** is `control/launch/<id>/claude`, created `O_EXCL|O_NOFOLLOW` with mode 0500. Its first line runs an absolute interpreter in isolated mode (`-I`: neither the working directory nor the script's directory on `sys.path`, no `PYTHON*` variables, no user site directory) on the minimal `kora-runner launch` script entry point, never `-m`, with no `PATH` lookup and no provider discovery.
- **The capability** is created in the launch directory `O_EXCL|O_NOFOLLOW` with mode 0400 when `allocate` is called; the launcher reads it `O_NOFOLLOW` after an owner and mode check.
- **The launcher** runs supervisor-side before `exec`:
  1. It reads its one-shot launch capability from its launch directory.
  2. It connects to the control channel as a launcher peer (section 2, "Local runtime"). The runner applies the kernel attestation of section 2 (a peer uid equal to its own, a peer pid of 0 refused, the pidfd or start-time rule) and checks that the peer's parent is the runner, that the peer's session id is the runner's own, which no process of the candidate domain can have because every launch and command leads its own session, and that its identity is not already a recorded leader. It consumes the capability at this first connection and unlinks the file.
  3. It fetches the launch specification with `launch_spec`: the binary path, the cwd, `PWD` and the complete environment. The runner first checks the binary's digest against the wheel `RECORD`; a mismatch ends the launch with `integrity_mismatch` and issues no specification. On `local` that detects drift, not tampering. The specification lives only in process memory and is dropped after delivery; it is never written to any file, journal, registry, checkpoint or log. The runner, and the launcher until `exec`, run with `RLIMIT_CORE` 0 and, on Linux, `PR_SET_DUMPABLE` 0, so no core file holds it.
  4. It closes every descriptor except 0, 1, 2 and the control connection, which it marks `FD_CLOEXEC`, so after `exec` only descriptors 0 to 2 remain.
  5. It calls `setsid()`, so each launch is its own session and group leader, and changes to the specification's cwd.
  6. It `exec`s the CLI with the SDK's arguments after its own name. A failure in steps 4 to 6 writes a failure record on the connection and never reaches `exec`. The runner reads end of stream without a failure record as `exec` success, and a failure record as `launch_failed`.
- **The launch's end.** The runner registers the launch pid when the launcher connects and observes its exit without reaping it, which the SDK does: `waitid(P_PIDFD, …, WEXITED|WNOWAIT)` on the pidfd taken at connection on Linux, and `kqueue` `EVFILT_PROC` with `NOTE_EXIT|NOTE_EXITSTATUS` on macOS. `ended()` then reports:
  - before `exec` succeeded: `launch_failed` for a refusal, a reused specification or a failure record, and `integrity_mismatch` for a binary digest mismatch; a launcher that dies after `launch_spec` without a failure record is indistinguishable from a quick exit after `exec` and is classified by the rules below. A launcher that never connects within 10 s of `allocate` ends `launch_failed`: the runner revokes its capability and unlinks the file, and signals that child after a start-time recheck, as for any signal by verified identity;
  - after `exec`, with no result and no runner-started stop: `cli_exit_without_result` for an exit with a status, and `launcher_died` for a death by a signal, including one the SDK sends;
  - after a result: the result's cause, with `termination: teardown_after_result`; after a runner-started stop: that stop's cause.

  When `ended()` resolves, whatever the cause, the launch's capability is revoked, so a later `launch_spec` for it is refused, and the runner removes `control/launch/<id>/`. The session-id check of step 2 assumes that the SDK starts `cli_path` in the runner's own session, without `start_new_session`; the adapter's tests pin this beside the other SDK-source assumptions, and a change fails closed, refusing every launch.
- **Environment ownership.** Each name has one owner, and a name assigned by two owners is an error, never an override; a collision ends `allocate` with `launch_rejected`. The runtime assigns `PATH`, a fixed trusted value that is never inherited, `KORA_ATTEMPT_TOKEN`, at least 128 random bits that grant no channel authority, the proxy settings of a runtime that enforces network policy, and, from `Workload`, the workload's `env`, the resolved `secret_refs` values and `harness_credentials`. The adapter supplies the rest of the list under "Launch inputs and environment" above through `LaunchInputs.environment`: for `claude`, `HOME`, `TMPDIR` and `CLAUDE_CONFIG_DIR` are the paths of the `harness_home`, `harness_tmp` and `harness_config` placements.
- **Placements.** The runner resolves and creates the four placement directories fresh at preparation, refusing a path that already exists, outside `WorkspaceTarget.root` and every trusted root. It checkpoints them, and they stay outside the request digest. `harness_input` holds the files `SessionSpec.files` lists, the MCP configuration file and the skills plugin, whose `SessionFile.placement` names the kind of file, not a directory; it is read-only to the candidate on protected runtimes. The runner removes the directories after transcript collection; `release`, including the worker-side path, removes any leftovers descriptor-relative without following links.

## 4. VCS (FR-5.2)

The [VcsRef revision-1 contract](artifacts/README.md) defines the shared payload, principal record, immutable behavior and supported native-revision domain. Head, push and approval observations follow its [fail-closed revision boundary](artifacts/README.md#6-current-observations-and-approvals). The VCS plans; kora's common executor runs its typed plans. [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md) supersedes collection in the candidate working copy. Requests carry no runtime identity. A VCS can supply Git or an independent native representation without changing the core.

```python
@dataclass(frozen=True)
class VcsCapabilities:
    change_requests: bool
    review_comments: bool
    ci_status: bool
    head_sha_watch: bool
    reviewer_requests: bool
    plan_executors: frozenset[Literal["runner", "worker_host"]]

@dataclass(frozen=True)
class ReviewerRequestOutcome:
    requested: tuple[str, ...]
    rejected: tuple[str, ...]
    reason: str | None

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

@dataclass(frozen=True)
class PrincipalRef:
    provider_instance: str
    stable_id: str
    kind: Literal["app", "bot_user", "service_user", "user", "local_service"]
    display_name: str

@dataclass(frozen=True)
class CredentialRequest:
    binding: str
    repo: str
    purpose: Literal["checkout", "publish", "api"]
    attempt_id: str | None
    operation_id: str | None
    prepared_digest: str | None
    destination_digest: str | None

@dataclass(frozen=True)
class CredentialRights:
    resource_scope: Literal["repository", "repository_set", "account", "unknown"]
    native_permissions: tuple[str, ...]
    operations: frozenset[Literal["read", "write_refs", "change_requests", "comments"]]

@dataclass(frozen=True)
class IssuedCredential:
    value: SecretValue
    principal: PrincipalRef
    rights: CredentialRights
    expires_at: datetime | None
    replacement: Literal["reissue", "rotate", "static"]
    revocation: Literal["native", "expiry_only", "manual"]

class Vcs(Protocol):
    name: str
    def capabilities(self) -> VcsCapabilities: ...
    async def acquire_credential(self, request: CredentialRequest) -> IssuedCredential: ...
    async def checkout_plan(self, request: CheckoutRequest) -> CheckoutPlan: ...
    async def publish_plan(self, request: PublishRequest) -> PublishPlan: ...
    async def open_change_request(self, ref: VcsRef, title: str, body: str) -> VcsRef: ...
    async def change_request_status(self, ref: VcsRef) -> ChangeRequestStatus: ...
    async def head_sha(self, ref: VcsRef) -> str: ...
    async def post_comment(self, ref: VcsRef, body: str) -> str: ...
    async def ci_status(self, ref: VcsRef) -> CiStatus: ...
    async def request_reviewers(self, ref: VcsRef, recipients: tuple[str, ...]) -> ReviewerRequestOutcome: ...
```

**Pending models (issue #134).** Three models get their fields with the first slice that needs them: `Feedback`, with the first slice that has a `fresh_with_feedback` retry or a `vcs` approval channel, whose `comments` reuse `FeedbackComment` of section 5; `ChangeRequestStatus` and `CiStatus`, with the first slice that has a code host or a `wait_event` sensor. `ChangeRequestStatus` keeps a `status` field with a `merged` value, which [ir.md](ir.md) and `examples/dependency-upgrade.ir.yaml` already read. The slice-1 git provider supports `head_sha` and declares `change_requests`, `review_comments`, `ci_status`, `head_sha_watch` and `reviewer_requests` false; capability checks refuse the unsupported operations before the call. The slice-1 `Vcs` protocol class leaves out `open_change_request`, `change_request_status`, `post_comment`, `ci_status` and `request_reviewers` until the first slice with a code host, so strict typing needs no placeholder; `HarnessAdapter.build_session_spec` receives `feedback=None` in slice 1 (section 3).

**Trusted roots and captures.** The runtime privately allocates `control_root` and `publication_root` outside `WorkspaceTarget.root`; `host_path` exposes only the candidate workspace for compatible preparation, never permission to publish there. `PlanFile.path` is relative to its named private area, rejects traversal/absolute paths, and is created with descriptor-relative no-follow operations. Parent replacement, symlinks and writable hard-link aliases cannot redirect private writes. Plans are immutable trusted provider output, not files loaded from the repository. Allowed placeholders in argv/env/stdin are `{candidate_root}`, `{control_root}`, `{publication_root}`, `{input_location}`, `{base_commit}`, `{commit}` and `{branch}`; all values come from supervisor state. There is no shell interpolation.

Captures are bounded typed outputs: `commit` and `branch` are validated single identifiers; `changed_paths` is a JSON array of relative paths, not newline-split text; `diff` is display text, not an executable validation source; `diff_stat` is display text. The provider-owned native decoder additionally emits the common `InspectionManifest` and `InspectionContent` index defined below; an opaque native manifest alone is not sufficient. Missing/malformed captures fail the phase. Core treats `PublicationInput.format` and revision strings as opaque; only the provider understands them. Locations name immutable bytes reachable through the runtime's transfer channel, not a worker-local filesystem assumption.

**Credentials and principals ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)).** Only the worker, its private worker-host executor and control-plane providers call `acquire_credential`, never code in the candidate domain. `checkout` is acquired immediately before preparation, `publish` only at late authorization (section 2) with the operation, prepared digest and destination digest set, and `api` per code-host call. `CheckoutPlan.credentials` and the publication steps name environment variables whose values come from the acquired credential of their purpose. The reference GitHub App backend issues in the control plane: the App signing key stays with `kora api`, and the worker requests each credential through the worker-facing issuance route bound to the attempt, its current lease epoch, the purpose and, for `publish`, the bound digests ([api-and-agent-surface.md](api-and-agent-surface.md) section 2). Other backends declare where their parent credential lives.

`IssuedCredential` reports what the backend enforces upstream: `rights.resource_scope`, the provider's own `native_permissions` with the mapped coarse `operations`, `expires_at` (null without native expiry), `replacement` and `revocation`, each independent of the others. A deployment binding declares the minimum `resource_scope` and `operations` it requires per purpose; a backend that cannot meet them fails acquisition with `config_error`, and nothing falls back to a broader credential. Reports state residual authority: a repository-scoped write token is not a single-push or single-branch grant. `expires_at` is independent of `replacement`: a static credential can still expire, and acquisition of a static credential past its `expires_at` fails with `config_error` instead of returning it. A credential that expires or is rejected is reacquired for the same request; an adopting worker reacquires instead of reusing a predecessor's credential. Values are redacted and never persisted; each issuance writes a `vcs.credential_issued` event with purpose, principal, rights, expiry and binding revision, and no value.

`PrincipalRef.stable_id` is the host's immutable account or app identifier, never a login, token id or alias. Each publication records the pushing principal and the principal that opened the change request. Each binding revision keeps the set of automation principals kora has used for it; a token replacement or a change of bot principal adds to the set. A backend declares whether its rotation preserves the principal.

Reference backends in MVP:

| backend | issuer | reports |
|---|---|---|
| GitHub App | control plane, from the App key | per-attempt installation tokens narrowed to the repository and the purpose's permissions; `expires_at` one hour after issue; `replacement: reissue`; `revocation: native`; principal kind `app` |
| static service credential | the secrets provider | `replacement: static`, `revocation: manual`; `expires_at` as configured by the operator or discovered from the host (for example a personal access token with an expiry date), null only when the credential has no native expiry; rights as configured by the operator, `resource_scope: unknown` unless the operator declares it |
| GitLab project or group token | the secrets provider | the token's scope names in `native_permissions`; date-based `expires_at` when configured; `replacement: rotate` or `static`; principal kind `bot_user` with the token's user id |

Git transport uses the Git CLI with credential-helper and SSH mechanisms; an SSH agent socket or key is a credential and never enters the candidate domain. A non-Git VCS supplies its own plans and backends.

**Preparation.** The selected executor creates an empty candidate root, materializes plan helpers only in control storage, checks required tools and executes the checkout plan with a minimal environment and checkout credentials. It captures `commit`, optional `branch`, and `changed_paths` for a `VcsRef` checkout. The supervisor constructs `WorkspaceRef` using the trusted request and persists it before starting the harness. For a `VcsRef`, `base_commit` comes from that input and protected-path checks run before a command gate. Checkout secrets/helpers and credential stores must not be inherited by candidate code. Reclaiming an attempt adopts a completed preparation rather than deleting a live workspace; a pre-submit absence alone permits no preparation anew while an earlier dispatcher may resume. Recovery observes/adopts the same attempt or retires it under section 2; only a never-dispatched first claim can prepare without resolving earlier authority.

**Export and sealing.** `PublishPlan.export` runs, if needed, in the candidate domain after the session, with no checkout, publication or code-host API credential. Its output is untrusted data until validated. The supervisor then terminates that entire domain and copies the export and selected workspace content into a private `PublicationInput`. Reads must resist path replacement, traversal, symlink dereference and hard-link aliases; symlinks that are legitimate VCS data remain data, not paths the publisher follows. Reject special files or unsupported encodings rather than guessing. Copied bytes have no writable alias in candidate storage. The supervisor fixes their digest before acknowledging sealing. Local uses the same copy/validation path without claiming that a hostile same-user process cannot interfere.

**Prepare and inspect publication.** `prepare` and `inspect` execute only in fresh publication storage, without VCS credentials, candidate configuration, executable attributes, helpers, plugins or live candidate processes. `prepare` yields one exact native revision; it is persisted once with the input and plan digests. `inspect` derives changed paths, review diff and the complete newly published content/history from that revision. kora checks protected paths, scans all newly published content for resolved secret values and common key patterns, and verifies a nonempty change against the trusted base. A hit is `secret_leak`; malformed/empty/unsupported content is `artifact_invalid`. Relevant secret values are available only to the trusted scanner and redactor, not the provider's subprocess environment; the publish credential acquired later is also checked against prepared content before it is delivered to a publish step. Success emits `PreparedPublication` and `awaiting_authorization` from the supervisor. `prepared_digest` binds the native prepared data and its metadata; `destination_digest` binds provider identity, trusted remote, repository, branch, base and transport settings, not just candidate display strings. `operation_id` is deterministic per attempt/publication output. The existing per-attempt publication plan produces the workspace revision; declared outputs referring to that workspace use the same prepared operation rather than allowing different destinations under one identity.

**Inspection handoff to the scanner.** The provider-owned decoder runs as an inspect step in private publication storage, with no scan secret values in its environment. Its required `inspection` capture is an `InspectionManifest`: the exact native revision and prepared digest, a content-index location/digest, and `complete=true`. The content index is a bounded JSON-lines stream of `InspectionContent` records. Each record identifies an opaque native revision, a relative path (or no path for revision metadata), kind, immutable content location, digest and byte length. It enumerates every file/symlink version and revision metadata newly reachable from the trusted base through the prepared revision, including intermediate versions later removed. A non-Git system with snapshot-only publication enumerates that complete new snapshot and its metadata. Native decoding/reachability stays provider-owned; core reads only this common interchange.

The trusted scanner pulls raw bytes through the authenticated private transfer channel, with backpressure, checks index/content digests and sizes against the manifest and binds its verdict to both prepared and inspection digests. It does not run the decoder with secret values or scan a display diff. Provider configuration supplies finite index/count/content-byte and inspect-time bounds, checked at startup; exceeding them is failure, not truncation. Malformed records, missing blobs, changed digests, unsupported kinds, a missing terminal completeness assertion or an incomplete enumeration fail `artifact_invalid` before authorization. Transfer unavailability is infrastructure failure, never successful inspection. Provider conformance verifies native reachability/completeness against real published history, not just a boolean assertion. The checkpoint retains this immutable index/content and verdict for recovery and for checking a newly acquired publication secret before delivering it to the publishing process.

**Finalize.** Publication steps execute only against the prepared revision and trusted destination, with a late credential supplied through section 2's authorization. No inspect/publish step may have `cwd=candidate`; only export and initial checkout may execute there. The authorization is kept out of argv, URLs, journal and persisted plan files; an ephemeral private helper or descriptor passes it only to the publication process. Output is redacted. The resulting `VcsRef` is assembled from trusted request/base, the prepared revision and verified remote result, not mutable workspace captures. `open_change_request` preserves every recorded runtime field/value and adds only absent, verified `url`/`opened_by` and `commit_url`/`compare_url` under the [enrichment and durable recovery contract](artifacts/README.md#5-upload-replay-and-result-ownership). Before create dispatch, persist attempt/output/runtime/destination identity and the acquired full API principal; reconcile actual creation/opener, retain learned effects even when artifact validation fails, and durably freeze the validated candidate before first upload; this remains a code-host API call made by the worker and idempotent on head/base by search before create. Its dispatch participates in the worker gate and must settle or be authoritatively fenced before that dispatcher acknowledges closure; a successful runtime retirement alone cannot settle it. Issuance backend design remains #43.

**Placement.** Both protocols declare `plan_executors`; prefer `runner`, otherwise `worker_host`. No common executor fails validation with `IR009` and fails execution with `config_error`. The names describe trusted executor placement, not permission to run a publisher inside the candidate domain.

| executor | preparation | publication | capability requirement |
|---|---|---|---|
| `runner` | supervisor in the runtime's execution infrastructure | separate trusted publication environment coordinated by `Runtime.finalize` | installed plan tools and code-host connectivity in the corresponding trusted phases; protected authority when required |
| `worker_host` | common host executor before candidate launch where `host_path` is exposed | runtime adapter transfers sealed bytes to a private worker-host publisher; same `finalize` contract | host client/tools and durable private checkpoint; no credentialed access to the candidate root |

Git declares both placements; local declares both, Docker and the configured fake runtime declare `runner` in MVP. An independent provider may expose no host path and still support complete preparation, sealed transfer, inspection and publication. The VCS sees no runtime name. The runtime adapter coordinates the common executor; it does not implement Git or another VCS.

**Git reference behaviour.** Provider `remotes` map logical repositories to trusted transport settings. Checkout uses a new repository and an explicit requested revision/branch; helpers and an empty hooks directory are private control files, never `.kora/plan/`. The agent may commit locally; kora performs publication. A candidate-side export produces a Git object bundle for the selected HEAD plus a raw selected worktree manifest: all tracked paths and untracked non-ignored paths, excluding candidate `.kora/` and `.git/`. Submodule gitlinks are data; no submodule command is executed. LFS publication is rejected as unsupported in MVP rather than executing a candidate filter or silently omitting required objects.

After sealing, the publisher imports validated, size-bounded objects into a fresh bare repository using strict object checks, with no inherited Git config/environment, alternates, object directories, hooks, fsmonitor, external diff, textconv, credential helper or candidate attributes. Validate that the selected HEAD descends from the recorded checkout commit and that the trusted base is present; never fetch candidate-selected prerequisites. Imported existing commit objects are retained unchanged. A separate trusted index uses raw bytes (`hash-object --no-filters` and index entries), modes and symlink blobs from the manifest to create the final tree, without `git add` or checkout in the candidate repository. If the final tree differs from HEAD, prepare exactly one additional commit with that HEAD as parent and message `kora: <node_path> #<n>`; persist its complete bytes and object id before inspection. Clean HEAD needs no extra commit. The trusted commit identity for that additional commit comes from provider configuration; existing candidate author fields are preserved and do not substitute for the authenticated publishing principal.

Checks read raw blobs and newly reachable commits between the trusted base and prepared HEAD, including intermediate content; diff is presentation only. `git archive` is not an exact raw-tree transfer because repository export attributes can omit or transform files. Push the explicit prepared object id to `refs/heads/kora/<graph>/<run_id>/<node_path>/<n>` at the configured remote, with create-only compare-and-set semantics. A candidate branch name or remote cannot redirect it. Remote equality with the prepared object is success; another value is `publication_conflict` (`artifact_invalid`) and is never force-overwritten. Candidate commits or explicitly granted code-host credentials cannot redirect this platform operation, but candidate code given its own publication credential can make independent side effects (FR-6.1b).

**Idempotency and failure.** Store the `PreparedPublication` and native bytes before accepting late authorization. Concurrent `finalize` calls for an operation serialize; identical repetitions return one `VcsRef`, different bindings fail. After a lost response, query the trusted destination with safe provider configuration: the same revision is success, authoritative absence permits retry of the same prepared revision only before the inherited deadline, after earlier dispatchers and in-flight calls are closed and while the attempt is not retired, unexpected content is conflict, and unreachable is still unknown (`infrastructure`, retryable). Do not recreate commits, repeat collection against mutable candidate files, or claim success from a missing response. Record each reconciliation as `vcs.publish_repeated`. Lost sealed/prepared state fails `infrastructure` and leaves any existing remote branch discoverable under its deterministic name; another attempt has another branch and never adopts it. There is no database outbox: the persisted attempt and the runtime's private phase checkpoint retain the operation identity. Lease/result fencing still governs API uploads; it cannot revoke an external push already in flight. A point-in-time absence while an older call remains possible is not a settled outcome. Unknown kora publication retains the execution-recovery barrier and reservations; after retirement or deadline expiry only recorded results/read-only reconciliation can be returned, with no new push or change-request creation (ADR-0017/ADR-0022). A reconciled operation outcome after expiry is historical evidence, not current-result/success authority.

Target identity validation occurs at API/scheduler source-result acceptance, after physical closure, not before every external effect. A rejected exported source result cannot become current, but already produced branches and arbitrary candidate/MCP effects are not rolled back. All target-source output reports, including the final session/result artifact, retain immutable full-payload identity for exact read-only replay; selected digest fields alone cannot establish equality. See [data-model section 12](data-model.md#12-immutable-run-target-identity-adr-0019).

## 5. Approval channel (FR-7.1, FR-7.2a)

```python
@dataclass(frozen=True)
class ApprovalLookupPage:
    request_ids: tuple[str, ...]
    next_cursor: str | None

class ApprovalChannel(Protocol):
    name: str
    def capabilities(self) -> ApprovalCapabilities: ...
    async def request(self, req: ApprovalRequestDto) -> str: ...
    async def lookup(self, approval_request_id: str, cursor: str | None) -> ApprovalLookupPage: ...
    async def poll(self, channel_request_id: str) -> ApprovalResponseDto | None: ...
    async def feedback(self, channel_request_id: str) -> list[FeedbackComment]: ...
    async def notify(self, channel_request_id: str, event: Literal["invalidated", "expired", "cancelled", "resolved"]) -> None: ...
    async def cancel(self, channel_request_id: str) -> None: ...
```

Built-in: `cli` (the API itself is the channel; `poll` reads the row) and `vcs` (publishes the `ReviewBundle` as a change-request comment with facts and narrative apart, maps review states to decisions, returns the head SHA and the approver's login and permission level with every response). The channel never decides validity: the scheduler compares SHA and digests and applies the DD-35 rules (approver is not the author and not kora's bot; on the namespace's `approver_allowlist` when set; "merged" counts only when the channel configuration sets `accept_merged_as_approval`).

Approval views render artifact text under the [VcsRef inert-presentation rule](artifacts/README.md#7-summary-and-availability), preserving facts/narrative boundaries and renderer-owned structure. Presentation encoding never changes the bound payload, revision or digests used by the scheduler.

A push to the candidate branch during review is not the channel's business either: the scheduler observes the head of the bound `VcsRef`'s branch through the VCS protocol's `head_sha`, moves the request to `invalidated` and fails the node with class `candidate_moved` (scheduler.md section 5); the channel learns of it through `notify("invalidated")`, and a response it reports afterwards is recorded and ignored.

`ApprovalCapabilities` retains `push_notifications`, `carries_identity` and `observes_head_sha`. `ApprovalRequestDto` binds the stable approval id, saved form/context, immutable SHA/digests and the saved versioned `run_cost` of [data-model section 7.4](data-model.md#74-immutable-approval-cost). Every channel presents that cost next to the form, including nullable USD, known subtotal, completeness and billing state, and reuses it unchanged on delivery/retry. The response form cannot replace accounting data. Response DTOs carry payload, the reviewer's `PrincipalRef` and permission, timestamp and observed SHA/digests; the scheduler, not the channel, derives the author and automation classification from the recorded publication principals and the binding's automation-principal set (section 4, ADR-0023), so a token replacement or bot change cannot let earlier automation or the author approve; feedback retains author/association/body and the existing collaborator filter. Only shared scheduler/API transitions decide validity.

`lookup` is strictly read-only on the configured original channel resource: enumerate every request/comment carrying the approval marker, including duplicates, through bounded pages and resumable cursor. Default at most 100 ids per page, within installation byte limits. Persist progress between calls and repeat scans with backoff to find late creation. Empty/complete pagination means none observed in that scan, not proof an earlier authorized write cannot appear. A provider with affirmative operation-closure evidence may expose that evidence through its delivery reconciliation metadata; lookup absence cannot invent it. `request` reconciles the same marker before creation while its local intent remains open; search-before-create does not establish generic exactly-once publication.

| request/gate state | durable desired state and channel delivery |
|---|---|
| Current pending request/open wait | `open`: reconcile/create under its stable marker and poll |
| Approved awaiting fresh VCS head verification | `closed`: no new create/poll authorization, `notify(resolved)` and transport `cancel`; local gate still waits |
| Approved and gate succeeded | Remains closed; canonical status shows approved revision and completed gate |
| Approved and gate failed/superseded | Remains closed; canonical status separates immutable approval from failed/current gate |
| Rejected | `closed`: notify resolved and cancel transport |
| Failed (`approve_when` could not be evaluated, [ir.md section 7.7](ir.md#77-evaluation-errors)) | `closed`: `notify(resolved)` linking the canonical status, which shows `failed`, and cancel transport; never shown as approved |
| Invalidated, expired, cancelled or historical owner | `closed`: corresponding terminal notification and transport cancel; never create during lookup |

`cancel` closes the channel interaction, not the immutable approval audit decision. If native code-host reviews cannot be disabled, mark the kora interaction closed; subsequent responses have no local authority. `notify(resolved)` says the request is no longer actionable and links canonical status; it never claims pending head verification succeeded. All terminal messages remain compatible with closure, even if remote delivery is delayed, duplicated or reordered. The API, not channel text, is authoritative.

Persist dispatch authorization before calling. A terminal revision prevents new create authorizations/retries; a preterminal authorized invocation may still send after cancellation. Record learned ids from all late results on the original approval even if their revision is obsolete, then close each id. Do not discard an effect result by applying the stale-read rule. Retain compact terminal reconciliation identity/progress without form/context payload holds, runtime reservations or an abandonment shortcut. Original-resource authorization failures are visible, not retargeted. The exact postcommit lifecycle is [data-model section 14.3](data-model.md#143-approval-delivery-intent-and-uncertain-effects).

**Requested reviewers** ([ADR-0025](../../docs/decisions/0025-notifications.md)). After opening the change request, the `vcs` channel calls `request_reviewers` with the channel configuration's `request_reviewers` list: provider-specific opaque identifiers, such as a GitHub login or team slug, that the provider validates and reports as `requested` or `rejected`. An empty list sends no request and leaves the host's own routing unchanged. A provider declaring `reviewer_requests: false`, or a failed request, writes `vcs.reviewers_request_failed` and never fails the node; success writes `vcs.reviewers_requested`. The call uses a code-host `api` credential. Requested reviewers grant nothing: ADR-0008, the approver allowlist and the principal checks decide what counts.

## 6. Secrets (FR-10.1)

```python
class SecretsProvider(Protocol):
    name: str
    config_schema: JsonObject       # includes `namespace_prefixes`: namespace -> allowed reference prefixes
    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue: ...   # secret://<provider>/<name>; refuses references outside ctx.namespace's prefixes
    async def list_names(self, prefix: str) -> list[str]: ...                           # for validation of aliases, never values
```

Secret references are **bound to the namespace** (current contract [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md)): the provider configuration maps each namespace to the prefixes it may reference; `kora validate` enforces it (`IR007`) and `resolve` enforces it again at submission time. Resolution happens inside the runtime adapter at submission time, after the nonsecret request has been durably sealed (ADR-0018); the saved request contains references only and current namespace authorization is checked again. Credential injection is outside semantic hashing and never serialized back into history; `SecretValue` registers itself with the redaction filter that every captured stream, error detail and VCS result passes through (FR-10.2). Built-in: `env`, `file`, and `scenario`, which serves only the values a scenario declares and is the only secrets provider a scenario run loads. What secret handling guarantees and does not guarantee in MVP is stated in [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md) (amended 2026-10-18): an agent node's secrets fail closed without enforced egress, an MCP server's secrets stay outside the agent on runtimes declaring `isolates_tool_secrets`, the model API key stays in the harness environment until a credential proxy exists, and redaction covers literal values only.

The `env` provider never resolves or lists a name beginning with `KORA_`, including `KORA_TOKEN` and `KORA_MCP_TOKEN`, or a configured name holding the worker's own credential. This exclusion applies even when namespace prefixes otherwise permit the reference. Refusals are content-free and do not reveal whether a forbidden variable exists; listing omits forbidden names and returns no values. It prevents ordinary configured secret resolution from forwarding control-plane credentials, not same-user process or filesystem access. [Issue #192](https://github.com/realkarych/kora/issues/192) implements this #141 contract through `test_env_secrets_never_resolve_kora_variables`, including resolution and listing, allowed-prefix overlap and present/absent forbidden names. Its existing provider conformance and observability obligations remain required.

**`SecretValue` (issue #134).** It is a plain slotted class in `kora.protocols` with a private attribute, not a dataclass or Pydantic model.

- **No rendering path reveals it:** `repr`, `str`, `__format__` and f-strings, logging, pickling, `copy`, `dataclasses.asdict` and Pydantic `model_dump`; serialization raises. Equality is identity, and there is no value hash.
- **`reveal()`** is the only reader, used only by trusted code that builds a process environment or credential input: the harness launcher, the command and MCP server environments, the checkout and publication steps, and the VCS provider's acquire and publish path. A static test enforces that `reveal()` appears only in `kora.runner`, `kora.executor` and the `plugins.runtime`, `plugins.harness`, `plugins.vcs` and `plugins.secrets` families, and that the private attribute is read nowhere outside the class and the transfer codec (`kora.protocols.secret_value`); `kora.conformance.secrets` and `tests/` are exempt, because they check the value against what they admitted.
- **Crossing a process boundary.** It crosses the worker credential route and the runtime control channel only as a typed transfer field that hides its input in validation errors and is never logged, persisted or digested. The encoded field type has the same rendering rules as `SecretValue`: no `repr`, `str`, logging or `model_dump` path reveals it. The transfer codec in `kora.protocols` is the only other sanctioned access path, and its serialization of the field into a response or a control-channel frame is the one sanctioned exception to the rendering rules; `kora.models` declares only the encoded field, never `SecretValue` or a plain string; `kora.api` and `kora.worker` hold a `SecretValue` only to hand it to the codec or to a trusted consumer, and never reach the plain value. Every receiving process decodes the field through the codec on receipt (the worker on the issuance response, the runner on the control channel), so the value is registered before any capture. The issuance response is exempt from idempotency replay storage ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)), and, under issue #134, kept out of access and debug logs, client response logging and HTTP caching (`Cache-Control: no-store`).
- **Registration.** Every process that constructs or deserializes the value (API issuance, worker, runner, publication helper) registers it with the redaction filter of `kora.observability` and gets a bounded registration scope back; `SecretValue` hands the plain value to the filter itself. When registration fails, the value cannot be revealed and secret admission fails, content-free. A scope is released only after its dependent captured output has been sanitized: transcript collection, stderr drain, segment upload and the publication helper's output. In the API, the scope is released when the issuing request completes, so registrations never accumulate across issuances. A value constructed while decoding the transfer field registers into the attempt scope of the decoding process, never into the object's lifetime. That scope is released at every end of the attempt in that process (success, failure, cancellation, lease loss, adoption by another worker and shutdown), once its dependent output is sanitized. A live mask is never evicted silently; on a `finalize` retry, the earlier scope is released once that try's publication output has been sanitized. The filter belongs to work item 1, and registration lifetime and rotation evidence to work item 14.
- **Forms and admission (issue #204).** A value registers its literal and the bodies of its two JSON-escaped forms, `json.dumps(value, ensure_ascii=True)` and `json.dumps(value, ensure_ascii=False)` without their quotes, each only when it differs from the literal; identical forms count once in a scope. Text that UTF-8 cannot encode, such as a lone surrogate, is refused before any form registers. A value that needs escaping therefore takes up to three of the registry's entries and bytes, which reduces the effective capacity for such values. Admission is all or nothing: when any form is refused, the forms already registered for that value are released before #148's content-free `SecretAdmissionError` propagates, and no `SecretValue` exists. The transfer field validates its own input (type, encodable text, a 16 KiB bound and admission) and raises one fixed `secret_transfer_invalid` error with no context and no exception chain, so validation errors read through `errors(include_input=False, include_context=False)` carry nothing of the value.

Secret registration lifetime and finite admission/matching limits follow [the logging content contract](observability.md#5-safe-content-and-trust-boundaries): outstanding captured output retains its required masks, and insufficient protection fails secret admission before exposure. An ordinary diagnostic-sink outage does not remove coverage or stop scheduling.

## 7. Artifact storage (FR-4.4)

```python
class ArtifactStorage(Protocol):
    name: str
    async def put(self, payload: bytes | AsyncIterator[bytes], digest: str, content_type: str) -> StorageRef: ...
    async def get(self, ref: StorageRef) -> AsyncIterator[bytes]: ...
    async def exists(self, digest: str) -> StorageRef | None: ...      # content-addressed dedupe
    async def delete(self, ref: StorageRef) -> None: ...                # idempotent; called only by the retention task
```

The storage protocol's `digest` argument is the full-payload byte digest (`artifact_version.payload_digest`, canonical JSON bytes for structured values), never the artifact type's selected-field approval digest. Content-addressed deduplication and integrity checks use that complete identity.

Built-in, both in MVP (FR-4.4, ADR-0005): `local` for standalone and development (directory tree `artifacts/sha256/ab/cd/<hex>`, `StorageRef = local://sha256/<hex>`, DD-36) and `s3` for docker-compose and organisation deployments, against any S3-compatible service through the `obstore` library (ADR-0013; objects under `<prefix>/sha256/ab/cd/<hex>` in one configured bucket, `StorageRef = s3://sha256/<hex>` resolved against the configured bucket and prefix so that references survive a bucket move; GCS and Azure are reachable through the same library without a new implementation). `s3` is the accepted organisation-deployment store; capacity and retention need sizing against measured traffic. The [load assumptions](../analysis/control-plane-load-estimate.md) do not establish that the volume cannot fit local disk. Workers never reach the store: they upload through the API, and the API always proxies downloads; presigned URLs were dropped from v1 (DD-37).

## 8. Artifact type (FR-4.1–4.3)

```python
class ArtifactType(Protocol):
    name: str                       # "VcsRef" or "namespace/Name"
    parent: str | None
    schema: dict                    # JSON Schema of the payload
    def validate(self, payload: dict) -> list[ValidationError]: ...
    def digest_fields(self, payload: dict) -> list[str]: ...     # which fields participate in gate digests (FR-7.2a)
    def summary(self, payload: dict) -> dict: ...                # saved content result; route projections are separate and CEL never reads it
```

The [built-in revision catalogue](artifacts/README.md) publishes VcsRef revision 1: closed schema, `kora.vcs-ref-validation.v1`, complete-payload digest selection and identity summary. Its byte/depth/path and strict-decoding predicates supplement generic JSON Schema validation; shape validity does not establish publication provenance. Whole-payload selection is stored as `[""]`, the root JSON Pointer. Built-ins ship as immutable schema/behavior revisions, expanded into the deployed contract (ADR-0018). A bound `ArtifactType` instance takes schema/parent and exact behavior identities from the saved graph-version contract, not the current registry alias. Custom types are registered through the API (`PUT /artifact-types`) as schema, parent and declarative behavior descriptors, with an optional trusted installed entry-point binding for custom `summary`. Registry writes update the authoring alias, never any deployed closure. Registration checks JSON Schema draft 2020-12 and the supported closed-reference rules in [ir.md](ir.md) section 2.1; deployment uses embedded definitions, not replacement aliases.

The generic `validation` identity is `kora.json-schema-2020-12.v1`; `kora.json-payload.v1` selects the whole canonical payload for digesting and returns the payload as the generic identity summary. A custom identity pins provider-declared semantics and the trusted installation's immutable implementation binding. It is not executable persisted data or an automatic package download. A reused identity with mismatched implementation metadata is incompatible; required semantics must be supported before the affected validation/execution operation. Compatible upgrades retain bindings required by active runs.

At production/upload, store the applied contract digest, validation outcome, actual digest-field selection and returned summary/projection. User-content diagnostics and projection values follow payload retention even if small/inline; content-free outcome/selection/digests remain metadata. Built-in content-bound approval projections are computed once under the saved contract and reused, never recomputed with a replacement plugin. Historical reads/diffs load these saved results and explicit availability; they do not call today's `summary`, `digest_fields` or validator. Branches, fan-out collection and approvals share these contracts/results despite having no physical runtime attempt. CEL expressions read neither `summary` nor any other type behavior: kora core cuts their projections from the validated payload under the graph version's frozen path sets ([ir.md section 7.5](ir.md#75-projections-and-typed-conversion)), separately from approval digest projections. Required expired payloads or unavailable behavior cause explicit failure, not current-value fallback.

## 9. Observability exporter (FR-11.3, NFR-9.2)

This exporter consumes durable domain events, independently of the [component diagnostic stream](observability.md#1-streams-and-authority). Its own failures use bounded diagnostics and independent counters; they are not recursively exported as new domain events. Diagnostic level filtering or overflow cannot drop durable event rows.

```python
class ObservabilityExporter(Protocol):
    name: str
    config_schema: dict
    async def on_event(self, event: EventRow, ctx: ExporterContext) -> None: ...   # every row of the append-only event table, in order
    async def flush(self) -> None: ...
```

One event concept (current contract [protocols](protocols.md)): exporters are loaded in the control plane only and consume the `event` table, so an export can be **replayed** from any point by re-reading rows; the scheduler feeds them through a bounded queue with drop-with-metric and an exporter never blocks a transition. Session facts reach the table because the API promotes them on upload: `vcs.*` from the worker's publish results and `permission_denied` from the runner's segments become rows. The built-in `otel` exporter follows the GenAI semantic conventions (current contract [protocols](protocols.md)): a run is an `invoke_workflow` span with `gen_ai.workflow.name = <graph id>`; on `attempt.finishing` it reads the attempt's session segments through `ctx.segments(attempt_id)` and builds the `invoke_agent` span, `CLIENT` kind when the harness runs remotely or managed, `INTERNAL` otherwise, with `gen_ai.usage.*` from the usage records; `gen_ai.conversation.id` is the harness session id, so kora spans join with Claude Code's own OpenTelemetry output on `session.id`. Langfuse and Datadog exporters follow the same interface.

## 10. Authentication (FR-12.4, NFR-4.4)

Standalone defaults and client credential selection follow [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md) and [the bootstrap contract](scheduler.md#10-standalone-sqlite-versus-postgres): two personal tokens, one owner, no new token kind or purpose flag. The authenticator returns each configured principal's actual flags/capabilities; the MCP server adds no permission model. The env secrets provider's `KORA_*` exclusion belongs to #141 and is implemented by #192.

```python
class Authenticator(Protocol):
    name: str
    config_schema: dict
    async def authenticate(self, request: RequestInfo) -> Principal: ...
        # kind (user | service), owner, namespaces [{namespace_id, transcripts}], approve, platform, and for service: runtime
    async def revoke(self, token_id: str) -> None: ...
```

Two principal kinds (current contract [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md), DD-32): a **user** principal is a personal token bound to a person and a namespace list, with exactly two flags, `approve` (may respond to approvals) and `platform` (may publish presets, namespace policies and tool policies), and a per-namespace `transcripts` capability for reading transcripts and session segments (current contract [protocols](protocols.md)); no roles or groups. A **service** principal belongs to a worker, is bound to a namespace list and may only claim attempts of its runtime in those namespaces and heartbeat, upload and transition attempts whose lease it holds (checked by lease epoch). Every by-id route joins on `namespace_id` against the principal's list (current contract [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md)). Built-in: `static_token` (MVP). OIDC implements the same `Principal` so routes do not change. There are no tokens for agent nodes: nodes never call kora (DD-5).

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

Built-ins: `change_request_status` and `ci_status` (through the VCS protocol; observation is the `ChangeRequestStatus` or `CiStatus` DTO), `http` (GET a URL with optional headers from secret references; observation is `{status, body}`; the URL must match the namespace's `http_allowlist` and link-local and cloud-metadata addresses are always refused, current contract [protocols](protocols.md)). Adopters register sensors through the `kora.provider` entry point and reference them by name in `poll.sensor`.

## 12. Policy (DD-29)

`Policy.validate` returns `ContractViolation`, an alias of the [shared `Diagnostic`](diagnostics.md). Paths are relative to the checked policy, and the caller owns document rebasing and error severity. Invalid policy remains `IR013`; the existing `IR019` namespace-coverage and `IR008` node-warning rules below are unchanged. The shared per-call count/byte limits are enforced before appending; overflow raises `KoraProtocolError` with code `diagnostics_limit`, fixed safe text and no payload details. Incomplete provider validation cannot authorize deployment or become warning-only success.

The policy protocol is the **decision point for tool calls**. The session runner invokes it from the harness's `PreToolUse` hook before every tool call the harness routes through that hook, the harness's output channel excepted (below); a deny stops the call and travels as a `permission_denied` session event that lands on the `AgentSession` artifact and is promoted to an event row on upload (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)). Approval requests are not the decision point: the Codex app-server raises `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` only when the agent needs to leave its sandbox, never for a command or an edit inside `workspace-write`, and `item/permissions/requestApproval` belongs to its `request_permissions` tool ([harness policy hooks](../research/harness-policy-hooks.md), checked 2026-10-16; [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)).

**Mechanism per adapter.** `claude`: the Agent SDK `PreToolUse` callback registered without a matcher, the only decision point: no `allowed_tools` or `disallowed_tools` list is derived from the policy, because a harness rule cannot express argv-level indirection or protected paths and a harness deny would override the per-layer `allow_deny` evaluation. The harness runs the callback before every tool call it does not reject itself, in every permission mode per its documentation, and deny decisions hold under `bypassPermissions`. Calls the harness rejects itself never reach the callback: a permission rule's denial appears in `ResultMessage.permission_denials` and becomes a `permission_denied` event with `source: harness`, while a call that a built-in tool's own input validation rejects never runs and is an ordinary failed tool result, not a denial (section 3); a call without a callback decision that is neither is a policy bypass that fails the attempt. The [probe of 2026-10-19](../research/claude-agent-sdk-probe-2026-10-19.md) observed this order for a settings deny rule and the Bash tool's own validation, and observed denies under `bypassPermissions` only, of shell and file-edit calls and of a tool on an in-process SDK MCP server, a kind the adapter does not use; stdio and HTTP MCP servers, the other permission modes, a callback allow under `dontAsk`, subagent calls and hosted tools rest on the [documentation](../research/claude-code-documented-controls-2026-10-19.md) until the scripted-model conformance cases below run. Under `trust_repo_config: true` the repository's configuration is trusted code outside the policy: per the Claude Code documentation its hooks run in parallel with the callback and can return `updatedInput`, which replaces the input after the decision; a project `disableAllHooks` setting may switch off the callback itself (not yet observed); its settings can change the harness's endpoint, proxy or TLS trust; and Claude Code reloads project settings the agent writes during the session, so the model can extend that configuration. Namespace denies are therefore not guaranteed under `true`, while protected paths are still checked at gate time; the namespace accepts this residual with `allow_trust_repo_config` ([ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md), amended 2026-10-19). In both modes Claude Code's built-in plugins, whose list the adapter checks against a reviewed set, may carry hooks of their own. `codex`: a `PreToolUse` command hook, `kora-runner policy-hook`, that reads the hook input from stdin, obtains the decision from the runner and exits 0 to allow or 2 to deny with the reason on stderr; the runner declares it as a managed hook in the attempt's `requirements.toml` with `allow_managed_hooks_only = true`, so the hook is trusted by policy and the repository's own hooks never load; `approval_policy` stays `never` and plays no part in the policy. `acp` (after MVP): `session/request_permission`, which an agent may or may not raise; that adapter declares its coverage per agent and ships only once verified.

**Coverage matrix.** Each adapter declares in `HarnessCapabilities.policy_coverage` (section 3) which call kinds its hook intercepts, and `check_config` and `kora validate` compare every `tool_policy` pattern with it (`IR008`, `IR019`, [ir.md](ir.md) section 10). For the MVP adapters, under every kora `permission_mode` and `sandbox` value, as the Claude Code and Codex documentation and the Codex sources stated on 2026-10-16; for `claude`, the probe of 2026-10-19 observed the shell and file-edit rows, and the MCP row for an in-process SDK server only, under `bypassPermissions`:

| call kind | pattern examples | `claude` | `codex` |
|---|---|---|---|
| shell command | `Bash`, `Bash(etl *)` | intercepted (`Bash`) | intercepted (`Bash`) |
| file edit | `Edit`, `Write` | intercepted (`Edit`, `Write`, `NotebookEdit`) | intercepted (`apply_patch`; the hook matcher accepts `Edit` and `Write` as aliases) |
| MCP tool call | `mcp__<server>__<tool>` | intercepted | intercepted |
| tool call inside a subagent | any pattern | intercepted; the hook input carries `agent_id` | intercepted; subagent hooks carry the parent session id |
| web and other hosted tools | `WebSearch`, `WebFetch` | intercepted (built-in tools) | **not interceptable**: Codex web search is a hosted tool outside its hooks; the adapter sets `web_search = disabled` in the attempt configuration and the runtime network policy is the layer that holds |
| harness-internal actions (project instructions, skills, memory) | none | not a tool call; governed by `trust_repo_config` through `setting_sources` and, for MCP server configuration, `strict_mcp_config`; auto-memory is off in both modes (`CLAUDE_CODE_DISABLE_AUTO_MEMORY`, per-attempt config directory) | not a tool call; governed by `trust_repo_config` through `project_doc_max_bytes` and `allow_managed_hooks_only` |

**Three layers.** The hook is one of three layers and the only one that sees tool names and arguments: (1) runtime isolation bounds the process regardless of what the agent calls (filesystem, network policy, credentials, process rights; `RuntimeCapabilities.enforces_network_policy`, DD-21); (2) the permissions of external services bound what a call can do (the rights of the tokens a session holds, the capabilities an MCP server exposes, publication through the worker rather than through the session); (3) the harness hook denies a call before it runs. A policy is the set of rights these layers enforce; a tool-name or argv check does not prove the absence of a forbidden effect inside an allowed program, so the shell-chaining rule below complements the first two layers and never replaces them.

**Fail closed.** A `tool_policy` pattern that names an uncovered call kind is error `IR019` when it comes from the namespace policy, because the platform team's rule would otherwise run unenforced, and warning `IR008` when it comes from the node alone.

```python
@dataclass(frozen=True)
class ToolCallRequest:
    harness: str
    tool: str                       # harness tool name, e.g. "Bash", "Edit", "mcp__yt__query"
    tool_use_id: str
    agent_id: str | None
    arguments: JsonObject
    workspace: WorkspaceRef
    node_path: str

@dataclass(frozen=True)
class ToolPolicy:
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    extensions: JsonObject          # the `x-` fields other implementations read

@dataclass(frozen=True)
class PolicyLayers:
    namespace: ToolPolicy
    node: ToolPolicy

@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reason: str                     # a code or a pattern id, never echoed arguments

class Policy(Protocol):
    name: str
    config_schema: JsonObject       # schema of the `tool_policy` object nodes carry
    def validate(self, policy: JsonObject) -> list[ContractViolation]: ...   # kora validate, IR013
    async def decide(self, request: ToolCallRequest, layers: PolicyLayers, protected_paths: tuple[str, ...], ctx: ProtocolContext) -> PolicyDecision: ...
```

One policy applies to every call (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md), amended 2026-10-21): the node's `tool_policy` and the namespace's `tool_policy` (set by the platform team, DD-32 `platform` flag) are evaluated **per layer**, and `decide` receives both layers, not a merged value. A call is allowed only when no `deny` pattern of either layer matches it and every layer with a non-empty `allow` has a matching pattern, so a namespace deny always wins and a node never widens a namespace allow list. An empty `allow` adds no restriction; a layer with only denies relies on its deny patterns. Order inside an array has no effect. `allow_deny` ignores `tool_use_id` and `agent_id`, so decisions stay deterministic for identical requests; `agent_id` is read only by the adapter's output-channel exception (section 3). The one exception is the harness's own output channel: on `claude` the adapter allows the synthetic `StructuredOutput` tool without consulting the policy, only under the closed exceptions of section 3 (exact name, `output_format` passed, main session, and never after `max_events`), without evaluating either layer. An exception or timeout in `decide` denies the call and fails the attempt with cause `policy_error` or `policy_timeout` (section 3). `IR013`, checked by kora's own validator for every policy implementation and wherever a `tool_policy` is validated, the namespace's included, rejects a pattern whose tool name is exactly `StructuredOutput`; a wildcard pattern stays valid and never applies to the output channel (ADR-0009, amended 2026-10-19). The MVP implementation `allow_deny` matches the allow and deny patterns of each layer in the harness's tool-pattern syntax. A shell call with chaining metacharacters, `-c`, `--exec` or other indirection matches every `Bash(...)` deny of either layer, so a deny cannot be bypassed through a wrapper, and an allow pattern never matches such a call, so it is denied whenever a layer with a non-empty `allow` applies (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)). "Other indirection" (`env`, `xargs`, `find -exec`, a shell script) stays open-ended: argv matching is the hook layer, not proof that an effect is absent (three layers above). It denies `Edit` and `Write` on `protected_paths` whether or not an allow matches (DD-34). Cedar and OPA arrive as plugins through the same interface, reading their own `x-` fields per layer, without an IR change. The policy runs inside the runner, so the decision is local to the session and never requires the agent to reach kora.

## 13. Conformance suites (NFR-10.2)

Each protocol ships `kora.conformance.<protocol>`, cases marked `kora_conformance` that an adopter imports into a test module. The cases take the provider under test from fixtures of the opt-in plugin `kora.conformance.plugin` (`kora_<protocol>_provider`, `kora_<protocol>_exercise`, and for secrets `kora_secrets_resolvable` and `kora_secrets_missing_reference`), which raise until the adopter overrides them; nothing is discovered or parametrised over entry points until `kora conformance` returns with issue #140. The plugin imports only pytest, and `kora.conformance` needs the `kora[conformance]` extra (issue #204). Every protocol module also carries the common cases of section 1: frozen and consistent capabilities, an object `config_schema`, a bounded content-free `KoraProtocolError` for the exercise's failing input, and provider logging through `ctx.logger` without root reconfiguration. Outline:

- **Runtime**: idempotent submit and workload-digest conflict; authoritative absence versus unreachable lookup; original-domain adoption and protected journal offset; declared portable recovery state; constructed candidate environment; local deadlines and cancellation; one logical handle/capacity reservation through publication; late `finalize` only after inspection, binding checks and serialization; no candidate-visible publication credential; preparation crash recovery through the supervisor checkpoint; release removes bulky candidate/control/publication roots but retains the retirement/dispatcher registry. ADR-0017 adds the execution-recovery gates below. `test_workspace_preparation_recovers_after_worker_crash` uses the configured fake runtime with no shared host filesystem, crashes at phase barriers and recovers the same trusted `WorkspaceRef`. OS isolation is verified only on actual protected runtimes (table below).
- **Harness adapter**: recorded SDK event streams yield the expected normalised events, usage completeness, `max_events` termination at the exact count, structured output and its fallbacks, hermetic behaviour (a repo hook that must not run), policy hook invoked before every tool call the harness does not reject itself, installed version recorded and checked against `tested_versions`. The `claude` adapter's cases are the [Claude adapter contracts](#claude-adapter-contracts-issue-125).
- **VCS**: checkout captures the requested base and protected paths; all private files stay outside the candidate tree; native sealed transfer, fresh metadata, fixed inspected revision and trusted destination; no runtime name in requests; same prepared revision on repeat/concurrent finalization; change-request search before create. `test_publish_and_open_change_request_are_idempotent` repeats finalization and creation concurrently using the same operation/head/base and asserts one published revision and one change request. `test_publish_reconciles_after_response_loss` loses the push/create response through an injected provider implementing the real protocol, then compares the same remote revision and reuses the existing request; conflicting remote content fails without overwrite. `test_vcs_runtime_cross_product` runs git and a directory-backed independent non-Git VCS through preparation, execution, sealing, inspection, authorization, publication and release on local and Docker in Linux CI, and on local and the configured fake runtime in macOS CI. The fake runtime exposes no worker-host root. Adopters run the same contract on their runtime/VCS pair; unsupported executor/protection combinations fail validation, with the limitation in the report.
- **Approval channel**: request → read-only paginated marker lookup → `None` → response with identity → stale response reported with its observed SHA → cancel and notify idempotent, including `resolved`; response loss, late/multiple external ids and every desired-state row above. Terminal reconciliation never creates or rewrites approval audit status.
- **Secrets**: resolution, missing-reference error shape, redaction in a captured stream.
- **Artifact storage**: content-addressed dedupe, streaming beyond memory, `get` of a reference written by an earlier instance of the implementation, `delete` after which `exists` returns nothing and `get` fails, and a repeated `delete` that succeeds. The suite runs against `local` on every CI job and against `s3` with an S3-compatible server started through `testcontainers` on the Linux job, which has Docker.
- **Artifact type**: validation errors carry JSON paths; `digest_fields` stable across key order.
- **Observability**: a slow exporter does not delay a run; drops are counted.
- **Authentication**: scope enforcement per operation; revoked tokens are refused; a provider whose validation is not a bounded local read caches it per credential for at most the re-check interval of the [bounded run-read wait](api-and-agent-surface.md#bounded-run-read-wait-adr-0002).
- **Sensor**: cooperative cancellation, declared serial dependency calls and bounded decoded results; `config_schema` rejects bad config with a path; `observe` is side-effect free when called twice; an `until` expression over the documented observation shape evaluates; `min_interval` is honoured by the scheduler.
- **Policy**: `validate` rejects a malformed policy with a path; a namespace deny beats a node allow; decisions are deterministic for identical requests; **a deny reaches every call kind the adapter declares in `policy_coverage`, under every permission mode**, for every call the harness does not reject itself, through the adapter's `PreToolUse` hook (Claude: the SDK callback; Codex: the managed command hook; after MVP ACP `session/request_permission` for the kinds the agent declares), and the denied call never reaches the tool (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)); a pattern on a kind outside the coverage is rejected by validation (`IR019`) or warned (`IR008`), never left unenforced; a node allow cannot widen a non-empty namespace allow list, and a call with chaining metacharacters, `-c`, `--exec` or other indirection matches every `Bash(...)` deny and no allow pattern (`test_tool_policy_layers`); a protected path cannot be edited; a pattern whose tool name is exactly `StructuredOutput` is rejected with `IR013` for every implementation. `test_required_policy_is_enforced_or_rejected` runs the matrix of section 12 against the real harness interface with a deterministic driver (recorded streams where the interface allows no driver): for every covered kind and permission mode an allowed and a denied shell command, file edit and MCP tool call, judged by whether the side effect happened, not by the presence of a `permission_denied` event; for every uncovered kind, validation of a node carrying a namespace pattern on it returns `IR019`. On `claude` the matrix runs on the scripted-model layer of the [Claude adapter contracts](#claude-adapter-contracts-issue-125), with no derived `allowed_tools` or `disallowed_tools` list, in every mode including `dont_ask` and `plan`: a callback allow runs the call, except that `plan` still blocks a call that is not read-only; a deny blocks it; an allow-listed command still reaches the callback; a protected-path `Edit`, `-c` indirection inside an allow-listed command, a subagent's call and a hosted tool's call are denied when the policy denies them; each is judged by its side effect. A live smoke test against a model is a manual experiment outside CI, recorded with the harness version; the fake adapter proves the runner's handling of a decision, never a real hook.
- **End to end**: the canonical graph on the fake harness against the runtime and VCS under test (NFR-0.2), through the testing runner of [fakes.md section 3](fakes.md#3-the-testing-runner-and-the-scenario-composition).

Which of these cases the fakes pass, which do not apply to them and which they never claim is the applicability matrix of [fakes.md section 8](fakes.md#8-conformance-applicability); their own contracts are in [fakes.md section 10](fakes.md#10-contracts-and-measurements).

### External observation conformance gates

The future database/race contracts are [data-model section 14.4](data-model.md#144-future-acceptance-contracts). Reference VCS, channel, sensor and external secret/storage implementations exercise real local service stalls, deadlines/cancellation, bounded parsing/streams/pagination, shared-alias cooldown, dependency-credit accounting and same-reference credential repair. Use configured protocol fakes with the same conformance suite, explicit conditions/barriers and injected time/randomness; no mocks or patched transports. DB cases run on PostgreSQL and SQLite. No runtime implementation exists yet; documentation CI does not execute these gates. Blocking/noncooperative installed Python is outside the chosen fault model and must not be advertised as contained by these tests.

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
| `test_harness_launcher_preserves_control_boundary` | Through each real harness interface's deterministic driver, prove launcher argv/env/cwd, SDK configuration, policy callback, interruption and nested tool/subagent launches cross the selected candidate boundary. No control directory/channel or parent credential becomes candidate authority. Its `claude` case runs on the scripted-model layer of the [Claude adapter contracts](#claude-adapter-contracts-issue-125): the real bundled CLI starts through the launcher passed as `cli_path`; the SDK's `-v` probe launches nothing; a hook decision crosses the boundary to the supervisor's callback; a tool is interrupted while it runs; nested tool, subagent and stdio MCP server processes run in the candidate domain without any supervisor marker and end with the kill; arguments carrying shell metacharacters and newlines arrive byte-identical with no shell side effect; and the kill goes through the runtime's kill handle without the SDK's `disconnect()` escalation. The fake adapter proves supervisor behaviour only; a manual model smoke experiment records harness version separately. |

| implementation | common lifecycle/publication suite | hostile-code authority evidence |
|---|---|---|
| local on Linux/macOS | required, including real Git, repeated finalization and adoption | unsupported by design; required-protection rejection must pass |
| Docker on Linux CI | required, including worker restart and private-volume handoff | real container/descendant, journal, alias and secret-boundary cases required; default container presence alone proves nothing |
| independent runtime/provider | required without a shared worker filesystem, with native non-Git revision/input data | provider's actual process/service isolation and transfer tests required before it declares true; otherwise required-protection rejection |
| fake runtime and the directory-backed fake VCS (`kora.plugins.vcs.fake`) | required, deterministic phase/recovery barriers, as [fakes.md section 8](fakes.md#8-conformance-applicability) marks each case | protocol evidence only; cannot certify OS isolation |

### Command-resource contracts (issue #45)

These are **future executable test contracts**; documentation CI does not run them. Runtime cases run on the Linux CI job with Docker and on the fake runtime; metadata cases run on PostgreSQL and SQLite.

| test | setup and observable assertion |
|---|---|
| `test_compile_exit_then_deploy` | Compile a graph with resources, delete the graph module and the original resource files, then deploy from the IR and its resource bundle alone; the version deploys and runs. A bundle file edited after compilation is refused before any request |
| `test_resource_set_digest_vector` | The compiler, the API and the runtime each compute the documented test vector of `ir.md` section 4, including the name with a quote and a non-ASCII letter, and reject names with control characters or backslashes |
| `test_resources_run_without_graph_package` | A worker host without the graph package or the author's files runs a command node from fetched resources and records their digests |
| `test_graph_runs_on_repository_without_gates` | The canonical graph runs against a target repository that has no `tools/gates/` directory |
| `test_changed_content_same_name_is_new_version` | Editing a resource without renaming it changes its digest and deploys a new graph version; runs of the old version keep executing the old bytes |
| `test_deploy_rejects_missing_or_mismatched_resource` | Missing, extra and wrong-content files in the deploy body are refused with structured errors naming the resource |
| `test_resource_digest_mismatch_fails_attempt` | Bytes altered in storage or in transfer fail the attempt with `integrity_error` before the command starts |
| `test_candidate_cannot_alter_resources` | On a `protected_execution` runtime, agent and command code cannot write, rename, replace or symlink-redirect a resource or any parent of `resource_root`; the executed digest is the pinned one |
| `test_unprotected_runtime_reports_protection_none` | On `local` the `CommandResult` says `resource_protection: none`; with `require_protected_resources` validation fails with `IR024` |

### Agent-secret contracts (ADR-0009, amended 2026-10-18)

These are **future executable test contracts** for issue #42; documentation CI does not run them. Runtime cases run on the Linux CI job, where Docker is available; validation cases run everywhere. Metadata cases run on PostgreSQL and SQLite.

| test | setup and observable assertion |
|---|---|
| `test_agent_secret_rule_fails_closed` | `kora validate` refuses with `IR015` an agent node with `secret_env` and no `network_policy`, one on a runtime without enforced egress, an MCP server with `secret_env` and no own `network_policy`, and one on a runtime that isolates tool secrets but does not enforce egress; the namespace policy `allow_unenforced_agent_secrets` lifts each |
| `test_agent_cannot_read_mcp_server_secret` | On `docker`, an MCP server declares `secret_env`; the agent's environment, files and `/proc` show no trace of the value, while a tool call that uses it succeeds |
| `test_docker_egress_blocks_unlisted_host` | On `docker`, a request from the candidate to a host outside the allowlist fails; the listed host and the adapter's declared model host succeed |
| `test_unenforced_secrets_require_explicit_option` | On `local`, a trigger of a graph whose agent node has secrets is refused without `allow_unenforced_secrets`; with it the run starts, records the option and shows it in the run view; a rerun of that run, pinned and with `graph_version: latest`, is refused without the option and accepted with it, and never inherits it |

### Scenario contracts (issue #46)

These are **future executable test contracts** for [dsl.md](dsl.md) section 4; documentation CI validates the scenario files against [scenario.schema.json](scenario.schema.json) and runs none of them. Two fake VCS backends serve scenarios: the independent directory-backed VCS, and a local Git repository with an in-process code-host fake; both pass the same VCS conformance suite. Metadata cases run on PostgreSQL and SQLite.

| test | setup and observable assertion |
|---|---|
| `test_scenario_selectors_are_strict` | A missing, an unused, a duplicate and an ambiguous outcome each end the scenario with `error` naming the selector; `optional: true` suppresses only the unused case |
| `test_scenario_retry_then_success` | `implement@1` fails with `stalled` and `implement@2` succeeds; back-off passes on virtual time; two attempts are recorded |
| `test_virtual_time_waits_for_real_commands` | A timer due while a real command runs fires only after the command exits |
| `test_watchdog_releases_processes` | A hanging command is terminated at the limit; the result is `error: watchdog_expired` and no child process survives |
| `test_scenario_isolation_is_enforced` | A command that opens a network connection fails, and resolving an alias the scenario does not declare is an execution error; on a host that cannot enforce the network the record says `network: unenforced` and strict mode fails. On `docker` a command cannot read a credential file planted on the host (`filesystem: isolated`); on `local` the record says `filesystem: shared` |
| `test_scenario_actions_are_ordered` | Reject, then a public retry on `settled`, then approve on the new activation: each action fires once in file order, the approval responds to the second activation, and an action left armed is reported unused |
| `test_scenario_response_follows_graph_form` | A graph whose human-input form requires `rollout_percent: number` and `signed_reason: string` receives a scenario `respond` carrying both fields; the ordinary approval API validates it against that form and the run proceeds, a response missing `signed_reason` is refused with `API003` and the scenario ends with `error`, and `reviewer` is recorded as reviewer metadata, never as a form field |
| `test_scenario_settled_waits_for_running_command` | A real command blocks on a barrier the test controls while no other work is claimable and no wait is open; an armed `settled` retry action stays armed, and the command is neither cancelled nor cleared, until the barrier is released and its result is recorded; only then does the action fire |
| `test_empty_fan_out_succeeds` | An empty `over` list gives a succeeded fan-out with empty outputs on both `skip_failures` values; a non-empty list whose instances all fail still fails |
| `test_scenarios_run_on_both_fake_vcs` | A Git-independent scenario passes on the directory-backed VCS and on the local Git backend |
| canonical scenarios | Each file in [examples/scenarios/sql_optimization/](examples/scenarios/sql_optimization/) passes with the status its `expect` states |

### Code-host identity contracts (ADR-0023)

These are **future executable test contracts** for [ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md); documentation CI does not run them. Each runs against two configurations: the GitHub App backend over a local Git repository with an in-process code-host fake, and the independent directory-backed VCS with an issuance backend of its own and opaque revision and change-request identifiers. The static-credential configuration runs the same cases to show its weaker reports. Metadata cases run on PostgreSQL and SQLite.

| test | setup and observable assertion |
|---|---|
| `test_publication_credential_after_initial_expiry` | The issuer's first credential expires during the session. Publication acquires a fresh `publish` credential after inspection and succeeds; no credential or parent credential appears in the workload, candidate environment, files, journal, logs or events |
| `test_publication_reacquired_after_worker_recovery` | The worker stops between authorization and `finalize`. An adopting worker reacquires for the same operation and finalizes the same prepared revision; there is one publication |
| `test_unauthorized_purpose_or_repository_refused` | Requests for another repository, another purpose, a stale lease epoch or a different destination digest are refused before any credential is returned |
| `test_backend_reports_actual_guarantees` | Each configuration reports `resource_scope`, `native_permissions`, `operations`, `expires_at`, `replacement` and `revocation` as specified in section 4; a static credential with a configured expiry reports that `expires_at` with `replacement: static`, and acquisition after it fails with `config_error`; a binding requiring more than the backend offers fails with `config_error` and receives no broader credential |
| `test_credential_issuance_is_never_replayed` | Two issuance requests with one `Idempotency-Key` both run the current lease, epoch, purpose, repository and digest checks, and nothing holding a value is persisted: the `idempotency_record` table, caches, logs and events contain no credential value. On a backend with `replacement: reissue` or `rotate`, the two requests return distinct credentials and a request after the first expires returns a fresh one. On the static configuration both requests may return the same valid value, and after its configured `expires_at` every request fails with `config_error` until the operator replaces the credential |
| `test_self_review_excluded_across_principal_change` | After a token replacement and after a change of bot principal, reviews by every historical automation principal and by the recorded publication principals do not count; a review by the triggering person counts |
| `test_core_independent_of_git_and_github` | The directory-backed VCS passes every case above without Git refs or installation identifiers |

### Provider-limit contracts (ADR-0024)

These are **future executable test contracts**; documentation CI does not run them. The fake harness emits the normalised limit and retry events with chosen values, and time comes from an injected clock. Metadata cases run on PostgreSQL and SQLite. A recorded trace per real harness, taken in the first slice, checks the mapping of section 14 separately.

| test | setup and observable assertion |
|---|---|
| `test_provider_limit_does_not_consume_agent_attempts` | A node with `max_attempts: 1` reports `provider_limited` with `retry_after` two hours ahead; it waits in a `wait` row holding no worker or runtime slot and succeeds on the next attempt after the clock passes the reset; its agent attempt count stays 1 |
| `test_provider_limit_pauses_admission_for_quota` | The pause applies when the limit is observed, while the reporting attempt's publication and closure are still unresolved; nodes whose models an account-wide, a model-specific or two overlapping groups cover are held, a model outside the paused groups is admitted at once, two aliases with one `quota.resource` share the pause, and an unmapped vendor group pauses the whole resource |
| `test_provider_release_is_paced` | Five instances wait on one quota whose pause expires; after an ordinary pass, after a scheduler restart past every wake and after a delayed tick, and for never-attempted instances held by the pause, admission admits at most `release_batch` per `release_interval`, oldest first |
| `test_provider_limit_ceiling_fails_node` | Without `retry_after`, back-off continues until `max_wait`; with a reset seven days away and `max_wait: 6h`, the node fails with `provider_limited` at six hours; a shared pause extended repeatedly by other attempts does not extend the node's deadline, and quota-caused admission waits count towards it |
| `test_provider_wait_budget_covers_never_attempted_peer` | Attempt A observes a limit on a shared quota; instance B becomes ready afterwards, has never executed and is refused admission with reason `provider_pause`. B's `provider_wait_deadline` is set at that first refusal to the refusal time plus `max_wait`. Other attempts then extend the shared pause repeatedly beyond that deadline: B's admission wait wakes at the deadline, not at the pause's `until`, and B fails with `provider_limited` at `max_wait` after its first refusal. The same holds when the scheduler restarts between the refusal and the deadline |
| `test_provider_limit_keeps_partial_work` | A session stopped for a distant reset ends through the collection and publication lifecycle; the next attempt with `fresh_with_feedback` starts from that `VcsRef` |
| `test_provider_wait_counts_towards_inherited_deadline` | A run whose inherited deadline passes during the wait fails with `timeout` |
| `test_internal_retry_is_liveness_not_stall` | `provider_retrying` events keep the session alive past `stall_after`; with no events at all the session ends `stalled` |

### Notification contracts (ADR-0025)

These are **future executable test contracts**; documentation CI does not run them. Receivers are in-process HTTP servers, time comes from an injected clock, and metadata cases run on PostgreSQL and SQLite.

| test | setup and observable assertion |
|---|---|
| `test_vcs_channel_requests_reviewers` | Against the in-process code-host fake, a published change request has the configured reviewers requested; an unknown recipient is reported `rejected` and the node continues |
| `test_delivery_survives_restart_and_retry` | The receiver fails twice and the scheduler restarts; one delivery succeeds with the same `webhook-id` |
| `test_late_committing_event_is_delivered` | An event created before and committed after another event is delivered |
| `test_slow_receiver_holds_no_lock` | A receiver that never answers holds no transaction, connection or lock; scheduling and SQLite writes continue; backlog is visible in metrics |
| `test_sender_crash_and_two_replicas` | A crash after the send and before the acknowledgement redelivers; two replicas never send under one claim; a stale acknowledgement changes nothing |
| `test_destination_checked_at_send` | A resolved secret that now names a host outside the allow-list, and a redirect, block delivery and mark the subscription `failing` |
| `test_subscription_lifecycle` | Start point, selection change, destination change, disable, enable and delete behave as `scheduler.md` section 19 states |
| `test_subscription_creation_orders_against_events` | On PostgreSQL, an event transaction that has read no subscriptions and a concurrent subscription creation: either the event commits first and gets no delivery, or the creation commits first and the event gets one; there is no event committed after the subscription without a delivery |
| `test_disable_races_uncommitted_delivery` | An event transaction inserts a pending delivery while a disable runs: after the disable commits no new send is authorised for that record, and a send claimed before the disable may complete |
| `test_recovery_entry_notifies` | A worker whose original execution domain becomes unreachable leaves its attempt in recovery; `attempt.recovery_required` is delivered before any reconciliation, once per entry, and an ordinary first claim awaiting its first observation sends nothing |
| `test_notification_carries_no_payloads` | A `slack` and a `telegram` message for `approval.requested` contain the console and change-request links and no secret value, transcript or artifact payload |
| `test_subscription_reads_need_no_platform` | With one token that has namespace access and no `platform` flag, listing and getting a subscription return the same `state`, failure and backlog fields over HTTP, `kora notifications list` and `show`, and `kora_reference`; the same token's put, delete, enable and disable are refused with `API005` over HTTP, the CLI and `kora_admin` |

### Execution-recovery implementation gates (ADR-0017)

These named cases specify future behavior; this documentation change implements no runtime or test suite. Run integration cases against both PostgreSQL and SQLite using the same parametrized fixtures, injected time/randomness and explicit condition barriers with deadlines. Use configured protocol fakes validated by the runtime/VCS conformance suites, real local processes/Git and an independently controllable local execution service; never patch modules, environment or HTTP transports and never use a real model/code host. Local process cases run on Linux and macOS without a sandbox/VM/administrator setup; Docker and containerized PostgreSQL cases run on Linux CI. Separate protocol correctness evidence from OS/provider termination evidence.

| future test | required observation |
|---|---|
| `test_healthy_parallel_admission_survives_recovery_contract` | Healthy live attempts and their current grants allow ordinary parallel admission; a first claim awaiting its initial observation is not by itself run-wide recovery. Lease expiry sets `recovery_required` and blocks new admissions until live adoption with earlier authority closed or complete retirement/closure; already-running work can finish. |
| `test_partitioned_live_host_is_not_assumed_stopped` | Pause API reachability after submission while a real local session continues; advance injected time beyond two lease periods. Attempt stays non-terminal/unknown with the same reservations and retry counts, no new submit; recovery adopts the same original-domain handle after reachability returns. |
| `test_submit_crash_recovers_in_original_execution_domain` | Pause between dispatch grant, runtime acceptance and handle persistence in turn. Wrong host/domain, missing handle and unreachable registry never permit replacement. A live handle is adopted; only original-domain retirement plus every dispatcher closure permits confirmed loss and policy retry. |
| `test_retired_attempt_cannot_start_after_delayed_submit` | Pause an old dispatcher before runtime submit, preparation and finalize in turn; install retirement first and then release it. No delayed new effect occurs. In the reverse order retirement stops/settles the managed operation or stays unknown. Duplicate completed operations return recorded results without effects. |
| `test_recovery_retains_every_dispatcher_generation` | Create three claims in different worker incarnations, leaving the first paused. Latest-holder and runtime closure do not erase its grant or clear the barrier. It closes its local gate before acknowledgement; a fresh claim relays the original-domain evidence without stale-epoch API writes. Adoption remains read-only while it is unresolved. After closure, recover a current non-retired attempt whose unchanged deadline/expiry guard is still eligible and whose push completed before a worker crash preceding `open_change_request`; finish only the stored operation from its authoritative checkpoint, with no agent restart/new commit. Repeat after deadline expiry: only recorded-result/read-only reconciliation and closure are allowed, no new push/change request. Cancelled/superseded/retired cases remain read-only. |
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
| `test_uncertain_usage_and_recovery_surface_parity` | Read attempt/run/target/events/metrics after an observation gap: original domain, version, reason, last observation/time, all grants, retained scopes and permitted actions agree; cost is last-known/partial, never zero/complete. Later usage updates historical totals only. The reconcile API maps through shared-client CLI and `kora_admin`; no curated tool is added and console mutations remain four. |

### Immutable execution-history contracts

[Data-model section 11.4](data-model.md#114-future-acceptance-contracts) defines the future integration/conformance matrix for saved inputs, schema closure, policy changes, request/recovery identity, retention and secret/transcript access. Its crash cases include sealing response loss, workspace preparation, runtime acceptance before handle persistence, changed dispatcher authority and changed branch HEAD. They must preserve the original request and ADR-0017 barriers. These are future executable gates; existing documentation/schema CI does not execute runtimes.

### Protocol model contracts (issue #134)

These are specified contracts for later work items; the first two rows are executable for their protocol-model legs since issue #204, and their worker-route and control-channel legs wait for work items 11, 14 and 17. Every database leg runs on PostgreSQL and SQLite.

| contract | evidence required when implemented | work items |
|---|---|---|
| `test_secret_value_never_rendered` | `repr`, `str`, f-strings, logging, pickling, `copy`, `asdict`, `model_dump`, JSON serialization, validation errors and exception chains of a model holding a `SecretValue`, and of a worker-route or control-channel model holding the encoded transfer field, contain no part of the value; only `reveal()` returns it, and only the transfer codec serializes the field | 6 |
| `test_secret_reveal_is_confined` | a static check finds `reveal()` only in the allowed packages, and the private attribute only in the class and the transfer codec, with the `kora.conformance.secrets` exemption of section 6 for `reveal()` | 6 |
| `test_secret_registration_lifetime` | registrations are bounded, released only after dependent output is sanitized, released per `finalize` reacquisition and at every attempt end, and survive the control-channel transfer and adoption by a new worker; the registration count and bytes return to baseline across repeated attempts in one worker process | 14 |
| `test_handle_and_events_are_secret_and_capability_free` | handles, adopted sessions, journal records and liveness contain no secret, control authenticator, dispatcher token or kill-handle capability; the node view's `attempts[]` has no `handle` | 14, 15 |
| `test_runtime_observation_is_not_quiescence` | `exited`, `lost` and a dead candidate record `unknown`; only a receipt, normal-completion quiescence or attestation sets `stopped`; dispatcher quiescence does not | 9, 17 |
| `test_adapter_cannot_emit_supervisor_kinds` | from a harness adapter `EventSink` accepts only the ten `NormalisedEvent` kinds and raises for `phase`, every supervisor kind, the command kinds and the runner markers | 14 |
| `test_event_content_is_bounded_and_classified` | `tool_call` never carries input; turn, tool-result, error and command-output content is redacted, bounded, marked and stored only in transcript segments; promoted audit events are content-free | 14, 15 |
| `test_command_output_volume_is_bounded` | journal and upload sizes are equal for 10^4 and 10^6 output lines, and RSS stays flat; the full streams are in the log files | 13, 14 |
| `test_tool_policy_layers` | a node allow cannot admit a call outside a non-empty namespace allow list; a deny in either layer wins; chaining, `-c` and indirection match every `Bash(...)` deny, and under an allow-only layer such a call is denied; the protected-path deny applies regardless of allow. Unit-level over explicit layers; the seeded namespace-policy legs stay pending under issue #140 | 14 |
| `test_policy_failure_denies` | an exception in `decide` denies and fails the attempt with cause `policy_error`, class `config_error`; a timeout with cause `policy_timeout`, class `infrastructure` | 14 |
| `test_adapter_exception_order` | after `max_events`, `StructuredOutput` is denied; the output channel is recorded as decided | 15 |
| `test_runner_outcome_covers_every_stop` | every cause maps to the one class of the cause table, an escalation keeps its cause's class, a confirmed bypass after a limit cause is `harness_error`, an overflowed held error result counts as a bypass, and `journal_unavailable` wins over a simultaneous stall | 14, 15 |
| `test_journal_backpressure_never_blocks` | with a slow or stuck journal writer, SDK draining, policy callbacks and interrupts keep their latency; a stuck writer followed by a silent stream still stops through `termination()`; an adapter record that does not fit is counted in the gap marker at once and the journal is marked incomplete; supervisor records are durable before acknowledgement and never fall into a gap; a stuck writer ends the attempt `journal_unavailable`, never `stalled` | 14, 15 |
| `test_finalise_memory_is_bounded` | memory is independent of uncounted message volume and bounded by the caps, including a stream that names more than 64 models | 7, 15 |
| `test_session_spec_is_closed` | a spec carrying environment, credentials, `cli_path`, callbacks or runner-owned options is rejected; file placement is closed; the prompt digest matches the sealed text | 15, 17 |
| `test_liveness_counters_reach_attempt` | each `Liveness` counter reaches its `attempt.counters` field once per heartbeat; the no-event-row rule is `test_counter_only_heartbeat_writes_no_event` of [scheduler.md](scheduler.md) | 17 |

### Claude adapter contracts (issue #125)

These are **future executable test contracts** for the `claude` adapter defaults of section 3 and its column in section 14; documentation CI does not run them. Each names its evidence layer:

- *Replay*: wire-level CLI recordings, stamped with the CLI version and refreshed whenever `tested_versions` changes, are played into the real adapter and SDK by a replay executable started as the launcher's target. It speaks the CLI's control protocol, with no mocks or patched transports; variants not yet observed are constructed from the CLI's result schema and marked until a recording replaces them. Replay proves normalisation, not interception.
- *Scripted model*: the real bundled CLI starts through the launcher, with the adapter's base URL pointed at an in-process Messages API server on loopback that returns scripted responses, so the CLI makes real tool, hook, subagent and MCP calls deterministically without a model provider (not yet observed to work with the tested CLI). It is the deterministic driver named above for `claude`.
- *Connect*: the real bundled CLI starts without a prompt or credentials.
- *Manual*: a live session against a model outside CI, recorded with the harness version.

The connect and scripted-model layers run on the Linux job inside a container without an external network, and each case asserts that no outbound connection was attempted; when the tested version cannot run that way, the case moves to the manual experiment and the gap is reported. Time comes from an injected clock and synchronisation from explicit conditions with deadlines; no case waits on a real grace period.

| test | layer | setup and observable assertion |
|---|---|---|
| `test_claude_session_ignores_repository_settings` | connect, scripted model | Canaries in the repository (instruction files, settings hooks, a deny rule, settings `env`, `.mcp.json`, a skill, a subagent, a command), pre-seeded `.kora/claude`, `.kora/home` and a symlink to an account configuration in the checkout, settings files the scripted agent writes into the config directory and the repository during the session, and in the invoking account (`~/.claude` instructions and hooks, `~/.claude.json` servers, a login-shell profile). Under `trust_repo_config: false` nothing of the repository or the account loads or runs, no marker appears, no memory file is listed and the shell snapshot carries no account configuration; under `true` the repository's items load, the account's do not, and the residual of section 12 is recorded as observed for the tested version. The init `plugins` list equals the reviewed set; a repository MCP server named like a node server fails `config_error`. That no `source: claudeai` server appears is checked in the manual experiment, because only a claude.ai login has such servers |
| `test_claude_session_receives_only_constructed_environment` | runtime | The launcher starts a recording executable in place of the CLI, on local on Linux and macOS and on Docker on Linux. Its environment is exactly the set of sections 2 and 3; a marker variable and a marker descriptor of the supervisor are absent; its stderr is not the supervisor's; its argv carries the preset system prompt, the permission mode and the MCP configuration file path, and no secret, control path or launch reference; the SDK's version probe starts no candidate process; the launcher refuses to start without a launch specification. The per-attempt directories lie outside the workspace and every trusted root, start empty, differ per attempt, carry no marker an earlier attempt planted, are refused when the path already exists or is a symlink, and are removed without following a symlink planted inside them. A node whose `env` sets a reserved name, `HTTPS_PROXY` and `NODE_OPTIONS` among them, fails validation with `IR026`, and an output schema over the argument limit fails `config_error` before launch |
| `test_claude_binary_digest_mismatch_prevents_session_start` | runtime, connect, scripted model | A binary whose digest differs from the pinned wheel's record is refused before launch with `integrity_error`; a reported `claude_code_version` that differs from the trusted version fails `config_error` before any prompt; a built-in `plugins` list outside the reviewed set fails `config_error` as soon as the CLI reports it, interrupting the first turn when that is where it arrives |
| `test_claude_skills_delivered_as_plugin` | connect, scripted model | Under `setting_sources=[]` the materialised skills appear as `<plugin>:<skill>` in `get_server_info()`, and the repository's skills do not; the scripted model invokes a plugin skill and its content reaches the session |
| `test_claude_usage_from_stream_events` | replay | One `usage` event per API call from its last `message_delta`, with the model and input counts from `message_start`; in the fresh no-subagent fixture their same-scope sum equals `ResultMessage.usage`, while a sum of `AssistantMessage.usage` would not; whole-tree subagent assertions compare with `model_usage`, never the main-loop query total. A normal end and an interrupt with `aborted_tools` give `complete` rows from `model_usage` with the raw figures in `source`; an interrupt with `aborted_streaming` and usage after the first result give `partial`; a cut before the `ResultMessage` after a kill, after the CLI's own exit and after an oversized message, and an empty `model_usage` after per-call events, give `partial` rows that include the input of the call in flight; nothing at all gives `unavailable`; `reasoning_tokens` is not added to `output_tokens`; per-call usage priced with the attempt's price table feeds `counters.usd`, and, once #140 activates enforcement, crossing `budget_usd` plus headroom ends the session with `budget_exhausted`; before activation unsupported configured budgets are refused; a model without a price row leaves `counters.usd` null; a constructed subagent variant counts the subagent's calls; duplicate starts/deltas, final normalized per-model `costUSD`/basis corrections, equal/unequal or absent TTL splits, unknown reasoning, subscription numeric cost, model attribution and unchanged source timestamps follow data-model section 7. Native numeric `costUSD` (including zero and fractional values) normalizes through section 7.3; booleans, non-finite, negative and overflowing values are refused, and normalized float inputs remain invalid. Constructed subagent variants establish normalization only, not real forwarding. |
| `test_claude_max_events_counted_by_kora` | replay | Several `AssistantMessage`s per message id, injected user text and a `<synthetic>` message: turns equal distinct message ids, tool results equal tool-result blocks, the injected and synthetic messages count nothing and `StructuredOutput` calls count; a callback request that arrives before its assistant message is counted once; at `max_events: N` the callback denies further calls, a `StructuredOutput` call included, and the runner interrupts at exactly N; `num_turns` is ignored |
| `test_claude_stop_reason_mapping` | replay | Every row of the section 14 stop-reason mapping, with the provider-limit row taking precedence over non-success results and runner interrupts for a provider limit, while a `success` after limits the harness retried internally stays success: `success` with `is_error: true` for each listed error, a `success` racing the runner's interrupt, a policy bypass, a limit cause followed by a bypass, a limit cause followed by a provider limit, a provider limit together with a confirmed bypass (issue #134), a `launch_rejected` pre-launch check, and a stream that ends without a `ResultMessage` after a kill, after a CLI exit, after launcher death and after a failed launch |
| `test_claude_structured_output_fallback_validates_schema` | replay, scripted model | A valid tool payload is accepted, and a valid payload under a non-success class is neither extracted nor uploaded; a non-null `structured_output` that fails the contract gives `artifact_invalid`; `null` with valid JSON text is accepted; `null` with schema-invalid JSON, fenced JSON, prose followed by JSON, `NaN`, `1e400`, a duplicate member name, empty or whitespace-only text, or `error_max_structured_output_retries` gives `artifact_invalid`; `error_max_turns` with `null` gives `events_exhausted`. The error details hold only the content-free summary, and the diagnostic is an `error` session event in a `session_segment`. A contract with a `urn:kora:artifact:` reference gets the same decision in the runner and on upload. A namespace policy that denies unlisted tools does not block `StructuredOutput`, while `mcp__x__StructuredOutput`, a subagent's `StructuredOutput` call and a `StructuredOutput` call without `output_format` reach the policy |
| `test_claude_harness_denials_are_recorded` | replay, scripted model | A call a permission rule denies before the callback becomes one `permission_denied` event with `source: harness`, not duplicated by a callback denial of the same `tool_use_id`; a call rejected by a built-in tool's own input validation, whose CLI result begins with the pre-execution form, is a failed `tool_result` and no `permission_denied` event, while a `permission_denials` entry for such a call makes it a denial; an undecided call with any other error result, including one whose output merely contains the pre-execution marker and an `mcp__` call whose error text begins with it, is a policy bypass, classified when the `ResultMessage` arrives, and after a kill without one is recorded as unconfirmed while the kill's class stands; on the scripted-model layer a built-in tool's execution error never begins with the pre-execution form; an entry for a call the callback allowed is a harness-side denial; a non-error result for a call no callback decided interrupts the session as a policy bypass; a kill before the `ResultMessage` marks the record incomplete; entries without a matching tool-use block are only counted; an oversized tool name is bounded; no event carries `tool_input` |
| `test_claude_stream_volume_is_bounded` | replay | Two runs with the same turns, tool blocks and API calls and 10,000 and 100,000 content deltas, with thinking, task-progress, rate-limit, subagent text and thinking, and repeated `api_retry` frames and a stderr line of 1 MiB without a newline: journal, upload and transcript sizes are equal across the two runs, `provider_retrying` events stay within `CLAUDE_CODE_MAX_RETRIES` per call, events are relayed before the result (NFR-3.2) with recorded relay latency, peak and post-quiescence heap and RSS do not grow with the delta count, and a slow event sink delays neither policy callbacks nor interrupt acknowledgements |
| `test_claude_session_releases_resources` | replay, runtime | One long-lived runner repeats success, interrupt, an unacknowledged interrupt followed by a kill, launcher death, an oversized message, cancellation, shutdown, a deadline that expires during initialize, an MCP status request that is never answered, a CLI that exits only after the SDK's teardown signal following a successful result (the result's class and `complete` usage stand), a session file replaced by a symlink or a FIFO (not followed, transcript recorded unavailable, teardown not blocked), and a target that leaves a detached child. Afterwards no launcher, CLI, descendant or MCP process, SDK task, thread, callback or pending control request remains; descriptor count, heap and RSS stay flat across cycles; the per-attempt directories are removed; escalation happens at its threshold however long any SDK call takes. A runner that crashes mid-session is covered by the local runtime's adoption contracts (issue #123) |

### Local runtime contracts (issue #123)

These are specified contracts for later work items; nothing here is executable evidence yet. Every database leg runs on PostgreSQL and SQLite, and every process leg on Linux and macOS.

| contract | evidence required when implemented | work items |
|---|---|---|
| `test_worker_restart_adopts_live_session` | a `SIGKILL`ed worker's runner and candidate keep running; a new incarnation adopts and resumes from the acknowledged offset with no duplicate, gap or second candidate | 14, 17, 21 |
| `test_dead_worker_dispatcher_closed_by_process_exit` | the grant closes with `runtime_observed_process_exit` and the observed identity, including a worker killed in the middle of `start` and one blocked on `events` | 14, 17 |
| `test_live_old_worker_keeps_adoption_observational` | a paused old worker keeps adoption observational until it exits | 14, 17 |
| `test_unconnected_grant_closed_by_process_exit` | a grant whose worker died before its first runtime call closes from the recovery grant list, with and without a runner | 9, 14, 17 |
| `test_process_exit_evidence_requires_same_host_and_pid_namespace` | a different pid namespace, a missing `host_id`, a moved state root and an unknown `boot_id` (a cloned host) each leave the grant unresolved; a `boot_id` from this root's earlier boots closes it; after a reboot the domain stays valid | 14 |
| `test_worker_child_effect_keeps_dispatcher_open` | a surviving effect subprocess of the dead worker keeps the grant open; `git` started by the worker runs with detaching disabled, so its maintenance child stays in the group; a worker started as a non-leader child (through `uv run`) becomes its group leader or refuses to start | 14, 17 |
| `test_stale_incarnation_claim_binds_its_own_process` | a claim from an older incarnation binds that incarnation's process, and a duplicate identity tuple, another worker id in a non-portable domain and an exit item for the reporting incarnation's own grant are refused; a re-registration after a reboot is refused retryably until the earlier lease lapses | 9, 17 |
| `test_lost_runner_is_reported_not_restarted` | a killed runner yields `lost` with the journal tail and leftovers; nothing restarts under the attempt id | 14 |
| `test_reused_pgid_is_never_signalled` | after runner loss, an unrelated group reusing the original numeric pgid receives no signal, including from the sweep; unverified members leave recovery unresolved | 14 |
| `test_receipt_names_its_scope` | every receipt and quiescence record, including the fallback's, names its scope; the stated escapes stay outside it, including a descendant with a cleared environment and one started through an existing same-user daemon; the fallback's list of unverified members holds only pids and start times; a fallback after a full-disk stop names only its own sweep's scope; a rewritten `attempt.token` leaves a descendant holding the real one outside the sweep, as the scope's limit states | 14 |
| `test_starting_entry_resolves` | a `starting` entry is retryable until the deadline; a runner registering after the fence exits without a candidate; a runner whose registration write fails with `ENOSPC` exits without a candidate and writes nothing; a late nonce match is killed; `retire` with no entry writes a tombstone that a delayed `submit` finds | 14 |
| `test_registry_states_and_release` | the operations-by-state table holds, including `retire` and `recovery` after `release`; an `unsubmitted` entry accepts `submit` and retires as `no_runner_started`; a worker-side acknowledgement before `submit` is checked against the passed grant binding; after `release` exactly the tombstone and lock remain, plus one immutable result file when a completed publication exists; no temporary registry/result file remains, including after a crash; failed, not_dispatched and not_applicable outcomes retain state/operation/code including null through release/restart without a result file, never inferred from absence | 14 |
| `test_vcs_ref_result_survives_release` | near/exact-cap publication results survive retirement, release and restart unchanged; every interruption of file/directory/registry durability recovers the exact valid orphan or fails unresolved for missing/corrupt/conflicting state; one current dispatch snapshot and bounded temporary files survive repeated failures without credential persistence | 6, 13, 14 |
| `test_vcs_ref_control_transfer_is_bounded` | near/exact-cap completed and prepared adoption content, many-grant snapshots, full-result size refusal with the authorized principal before dispatch, truncated/extra/wrong-digest chunks, response loss and cancellation preserve bounds and read-only replay | 6, 13, 14 |
| `test_completed_attempt_never_looks_lost` | after normal completion and runner exit, `status` reports `exited` in phase `complete`, `quiescence` and `recovery` return the recorded record, and `finalize` is refused; a runner that has no worker attached keeps running until `completed` or `retired` is written, and exits after either | 14 |
| `test_registry_lock_is_bounded` | a stopped lock holder delays neither `lookup` nor kill escalation past the bound | 14 |
| `test_control_channel_rejects_impostors` | another process on the path, which the worker refuses because its identity is not the registered runner's, a peer with another uid, a peer pid of 0, a reused pid, a forged entry, a known dispatcher id from a new process, a process whose identity differs from the grant's bound process, a pidfd whose process has exited, a peer started after the accept time, a listener whose identity differs from the registered runner's as seen from the worker, including a reused runner pid, a launcher peer with another uid or pid 0, and a launcher calling anything but `launch_spec` are refused; a handshake that stalls is closed after 5 s, a launcher connection that completed its checks stays open past 5 s, a kill before `setsid` signals only the launcher and leaves the runner's group alone, a kill after `setsid` signals the launch's group and a kill after the SDK reaped the leader signals nothing, and connections and followers beyond their caps are refused | 14 |
| `test_journal_classes_and_caps` | durable supervisor records; batch ordering; caps with draining; supervisor records still appended at the cap; supervisor records past the cap coalesced beyond 1 MiB; `ENOSPC` while a candidate keeps writing stops the candidate, refuses a later `start`, `transfer_in`, `finalize` and `quiescence`, stops a `finalize` in progress with its outcome `unknown`, deletes nothing, fails the attempt `journal_unavailable` and leaves recovery unresolved and `retire` retryable until space returns, then the tombstone is written; the same with the runner lost and the worker-side fallback, and with the runner lost while holding the in-memory fence before any durable write; after a failed adapter batch, append or sync nothing more is appended, readers stop at the last complete line, no `i` is reused and the tombstone counts the records not journalled; a runner or a fallback whose append succeeds and whose sync fails leaves that line readable and counted as not durable, and the fallback, also after a restart, writes `runtime.retired_without_runner` at most once; under the fence `record_dispatcher_quiescence` is refused retryably, attach and `adopt` are accepted and a first attach's record reaches the tombstone, and `events` streams end; worker shutdown cancels the fallback's retry timer and writes nothing; in `completed` and `retiring`, a failed write closes the journal and refuses only its operation, leaving the normal-completion record standing; `kora doctor` reports an attempt stopped on a full disk; `retire` under the fence writes only through the runner's scheduled retry; `EDQUOT` behaves as `ENOSPC`; the worker-side relay never shows the token, also after a worker restart and adoption, and is refused without a readable `attempt.token`; a fallback without a readable `attempt.token` issues no receipt; failed registry writes leave no temporary files across repeated retries; torn-tail repair; a blocked writer delays neither stop escalation nor runner exit | 14 |
| `test_transfer_digest_mismatch_is_rejected` | corrupted push and pull; traversal, symlink, intermediate-symlink directory, hard link, other-device, FIFO, non-manifest and replace legs | 14 |
| `test_launcher_takes_spec_from_private_channel` | the launcher refuses without a specification or with a reused one; it passes only descriptors 0 to 2; no specification bytes appear under the state root; each launch-end class is reached, including `exec` success read from end of stream; a process that is not the runner's child, an orphan reparented to the runner and a recorded leader, each presenting a valid capability, are refused; a module planted in the cwd does not run in the launcher; a failed `setsid` never reaches `exec`; an environment collision is `launch_rejected`; a binary digest mismatch is `integrity_mismatch`; a CLI exit with a status is `cli_exit_without_result`; a launcher that never connects is stopped and a connection after the timeout is refused | 14, 15 |
| `test_runner_environment_excludes_worker_credentials` | the runner's environment holds no API token, database URL or worker secret | 14 |
| `test_state_root_and_socket_dir_refuse_unsafe_ownership` | a symlink, a wrong owner or mode, a network filesystem and a pre-created foreign directory are refused | 14 |
| `test_runner_survives_worker_service_restart` | a worker running as a systemd service with `KillMode=process` is restarted, and a session ends with `KillUserProcesses=no`, and the runner and its socket stay in place; a recorded socket path stays in use after lingering changes; `KillMode=control-group`, `KillMode=mixed`, a user-manager unit without lingering, `KillUserProcesses=yes`, `PrivateTmp=yes`, `OOMPolicy=kill` and `ManagedOOM*=kill` are refused; `kora doctor` reports the same settings on an ancestor unit, the shared unit limits, `OOMPolicy=stop`, `NotifyAccess=all`, a socket directory under `/tmp` and a worker that is PID 1 of a container | 14, Linux CI |

## 14. Harness mapping tables (FR-3.3, DD-27)

Resolved node-config field → harness option. The Claude Agent SDK and Codex app-server columns are the MVP adapters; the **ACP column is the design for the adapter that ships after MVP** (ADR-0004) and is kept here so the vocabulary stays harness-neutral. "runner" means enforced by the session runner from the SDK event stream regardless of harness; "warn" means the field cannot be honoured and `check_config` emits `IR008`.

| config field | Claude Agent SDK (`ClaudeAgentOptions`, `query` / `ClaudeSDKClient`) | Codex app-server JSON-RPC through kora's client (`thread/start`, `turn/start`) | ACP, after MVP (`initialize`, `session/new`, `session/prompt`) |
|---|---|---|---|
| `prompt` | `query(prompt=…)` | `turn/start` input | `session/prompt` |
| `model` | `model` | thread config `model` | agent-specific session config; warn when the agent exposes none |
| `system_prompt_append` | `system_prompt={type: preset, preset: claude_code, append: …}`; the preset is passed without `append` when the field is unset, because an unset `system_prompt` gives the session an empty system prompt | `model_instructions_file` override (replace semantics; adapter prepends the Codex defaults) | warn; adapter prepends to the first prompt |
| `skills` | materialised skill directories inside one per-attempt local plugin in the harness input directory, `plugins=[{type: local, path}]` with its `.claude-plugin/plugin.json`, listed as `<plugin>:<skill>`; not `add_dirs`, whose skills are not discovered under `setting_sources=[]` | `skills.config[]` config override (DD-20) | warn unless the agent reads skills from the workspace |
| `subagents` | `agents={…}` | warn | warn |
| `hooks` | warn (`IR008`): node hooks are not passed, because supervisor-side SDK callbacks would run author code with supervisor authority and candidate-side command hooks could act outside the namespace policy; an open implementation question (section 3) | `[hooks]` in the attempt's managed `requirements.toml`, next to kora's policy hook, so they run under `allow_managed_hooks_only = true` while repository hooks do not | warn |
| `mcp_servers` | `mcp_servers=` the path of a configuration file in the per-attempt harness input directory, so no server environment or header travels in argv (pinned digests verified by the runner) | `mcp_servers.<id>` config | `session/new mcpServers` |
| `tool_policy` (DD-29, current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md); coverage matrix in section 12) | the `PreToolUse` hook callback → policy protocol, the only decision point, for every call the harness does not reject itself, in every permission mode; no `allowed_tools` or `disallowed_tools` list is derived; a permission rule's denial before the callback is recorded from `ResultMessage.permission_denials` as `permission_denied` with `source: harness`, and a tool's own input-validation rejection as an ordinary failed `tool_result`; the adapter allows the synthetic `StructuredOutput` tool itself under the closed exceptions of section 3 | the `PreToolUse` command hook `kora-runner policy-hook`, declared as a managed hook in the attempt's `requirements.toml` with `allow_managed_hooks_only = true` → policy protocol; independent of `approval_policy`; `web_search = disabled` because hosted web search is outside the hook | `session/request_permission` → policy protocol for the kinds the agent declares; warn on the rest |
| `permission_mode` | `permission_mode` (`plan`, `acceptEdits`, `dontAsk`, `bypassPermissions`), always passed, `dontAsk` when no layer sets the field, because the CLI's own default depends on its configuration directory | `approval_policy` (`never` for `dont_ask`/`accept_edits`), `bypass` → `danger-full-access` | `session/set_mode` where the agent offers modes; else warn |
| `sandbox` | `sandbox=SandboxSettings(…)` | `sandbox_mode` (`read-only`, `workspace-write`, `danger-full-access`) | warn; runtime isolation only |
| `trust_repo_config: false` | `setting_sources=[]`, `strict_mcp_config=True`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, and a fresh per-attempt config directory and home outside the checkout | user config ignored, project untrusted, `project_doc_max_bytes=0` | hermetic workspace overlay; warn when the agent reads project config regardless |
| `trust_repo_config: true` | `setting_sources=[project]` without `strict_mcp_config`, so `.mcp.json` loads; `ENABLE_CLAUDEAI_MCP_SERVERS=false`, and a `claudeai` server after connect fails `config_error`; the repository's configuration, including project settings the agent writes during the session, is trusted code outside the policy (section 12) | project trusted | agent default |
| `network_policy` | runtime (DD-21); additionally `sandbox.network.allowed_domains` | runtime; additionally `sandbox_workspace_write.network_access` | runtime only |
| `output_schema` | `output_format={type: json_schema, schema}` with the bundled effective contract → `ResultMessage.structured_output`, validated by kora; when it is `null`, `ResultMessage.result` only if it is strict JSON that validates; else `artifact_invalid` | `turn/start outputSchema` → final message | warn; the runner validates the final text as JSON, else `artifact_invalid` |
| installed version (recorded only, DD-14) | `get_server_info()` at connect and the `init` message, checked against the pinned SDK's `__cli_version__` and the executed binary's digest | `initialize` response | `initialize` response `agentInfo` |
| `limits.stall_after` | runner, from message timestamps, extended during an open tool call | runner | runner |
| `limits.timeout` | API/scheduler guard authority; runtime/common executors use the saved earliest node/subgraph/run deadline through every managed phase, nominal interrupt at D and escalation at D+30s, without API connectivity (ADR-0022) | `turn/interrupt`, grace, kill | `session/cancel`, grace, kill |
| `limits.max_events` | runner, counting one `turn` per distinct message id, one `tool_call` per tool-use block and one `tool_result` per tool-result block; `ResultMessage.num_turns` is not used; `max_turns` is also set as an additional native limit, no lower than `max_events` | runner, counting `item/completed` notifications | runner, counting `session/update` tool calls and agent messages |
| `limits.budget_usd` | `max_budget_usd` at `budget_usd` plus `budget_headroom_pct` **and** runner from the per-call usage of streamed `message_delta` events priced with the attempt's price table; unpriced models leave `counters.usd` null | runner from normalized `thread/tokenUsage/updated` observations and the saved price table, when #140 activates enforcement | runner from usage in `session/update` when present |
| usage source | per API call, `message_start` and its last `message_delta` (`include_partial_messages=True`), for budget and `partial` usage; `ResultMessage.model_usage` per model with `costBasis` for final rows, `complete` when a result is parsed (ADR-0010), subagents included per the SDK documentation, not yet observed; `AssistantMessage.usage` is never summed | `thread/tokenUsage/updated` (`total` and `last` are overlapping observations, never summed); `turn/completed` has no usage; see the Codex usage contract below | agent-dependent; `unavailable` when absent |
| session handle (`resume`) | `resume=<session id>` | `thread/resume` | `session/load` when the agent declares `loadSession` |
| `env` / `secret_env` | the runtime launcher passed as `cli_path` starts the CLI with exactly the constructed environment, because the SDK passes the supervisor's environment and `env=` cannot remove inherited variables; names in `reserved_env` are rejected (`IR026`) | `shell_environment_policy` + constructed env | constructed env of the agent process |
| MCP server `secret_env` (ADR-0009) | the sidecar server's own environment; the harness gets an HTTP MCP entry for it | the same, through the `mcp_servers` HTTP entry of `config.toml` | the same where the agent accepts HTTP MCP servers; otherwise `isolates_tool_secrets` is false for it |
| harness credential (DD-30) | `ANTHROPIC_API_KEY` in the constructed env; base URL to the proxy post-MVP | `CODEX_API_KEY` in the constructed env | agent-specific variable |
| billing class | auth-method probe on the init message | auth file inspection; default `api_key` | `subscription` unless the adapter config declares otherwise |
| stop reason / error class | `ResultMessage.subtype` with `is_error` and `terminal_reason`: `success` with `is_error: false` → success, then output extraction; the provider-limit row below takes precedence over a result that does not map to success and when the runner interrupted for a provider limit, except over a confirmed policy bypass, while a `success` with `is_error: false` after limits the harness retried internally stays success; `success` with `is_error: true` → `config_error` when an `AssistantMessage.error` is `authentication_failed`, `billing_error`, `account_on_hold` or `credits_required`, else `harness_error`; `error_max_budget_usd` → `budget_exhausted`; `error_max_turns` or `terminal_reason: max_turns` → `events_exhausted`; `error_max_structured_output_retries` → `artifact_invalid`; `error_during_execution` after the runner's interrupt (`terminal_reason` `aborted_tools` or `aborted_streaming`) → the class of the interrupt's cause, which also wins over a `success` racing it, otherwise `harness_error`; a tool call no callback decided → `harness_error` (policy bypass), also over every limit cause, `provider_limit` included, because a bypass is a security event (issue #134); no `ResultMessage` → the cause's class after a runner kill, `harness_error` when the CLI exits by itself or exceeds the message limit, `infrastructure` when the launch fails or the launcher dies; a pre-launch check of the runner that fails, such as an output schema over the platform's argument limit (cause `launch_rejected`) → `config_error`, with nothing launched; `terminal_reason` and `api_error_status` go to the error details, and `errors` only as a content-free summary, its text kept as for structured-output diagnostics (section 3) | `turn/failed`, `error` → `harness_error` | `stopReason` (`end_turn`, `max_tokens`, `cancelled`, `refusal` → `harness_error`) |
| provider limit (ADR-0024; documented, not yet observed) | `rate_limit_event.rate_limit_info.status = "rejected"` with `resetsAt` and `rateLimitType`; assistant `error = "rate_limit"`; result `api_error_status = 429` → `provider_limited`; `billing_error`, `account_on_hold`, `credits_required` → `config_error`; `overloaded` → `harness_error`; `api_retry` and `tool_progress.subagent_retry` → `provider_retrying` (SDK 0.2.168 does not deliver `tool_progress`, SDK source) | `error` with `codexErrorInfo` `usageLimitExceeded` or `rateLimitExceeded` → `provider_limited`, reset from `account/rateLimits/updated` windows (`resetsAt`, `limitId`); `unauthorized` → `config_error`; `serverOverloaded` → `harness_error`; `willRetry: true` → `provider_retrying` | agent-dependent; without a documented signal a limit stays `harness_error` |

Fields with the same semantics on every harness are the kora vocabulary; fields that only one harness has are mapped on that harness and warned on the others, never silently dropped (FR-3.3).

### Codex usage contract (issue #142)

The app-server's `thread/tokenUsage/updated` notification carries thread/turn identity and `tokenUsage.total`/`last`. `turn/completed` carries a turn without usage. The adapter takes coherent cumulative observations/deltas with stable identity/order; it never sums snapshots, or adds `last` to its overlapping `total`. Thread totals from a resumed session require the previous-attempt baseline. A turn's completion does not imply all usage was received.

Native input includes cached input and cache writes: normalize uncached `input_tokens = inputTokens - cachedInputTokens - cacheWriteInputTokens`; keep cache reads and writes separately. Output includes reasoning, so `reasoningOutputTokens` is a subset of `outputTokens`. Unknown dimensions stay unknown, impossible subsets are refused, and no unequal cache-TTL split is invented. Token-usage notifications do not themselves establish per-model attribution; configured model alone is insufficient when rerouting or multiple models are possible. Keep an attribution gap unless source evidence establishes the model. Synthetic context-window-fill totals are not billable usage. [Dated primary-source evidence](../research/usage-pricing-sources-2026-10-23.md) separates these source facts from unobserved runtime behavior.

With usable evidence before a kill, usage is partial; without it, unavailable. A loss, reset, unexplained regression, reroute without attributable usage or unobserved final interval cannot become complete from status alone. Subscription billing keeps USD null. Replay proves normalization only: work item 15 must add `test_codex_usage_preserves_attribution_and_gaps` and deterministic cases through the real app-server interface, plus operator probes for model attribution, rerouting, killed turns and synthetic totals. The tests cover duplicate/reordered notifications, missing dimensions, unequal cache rates, no-report versus zero, resume baselines when supported, and the engine's basis rules; no real provider is called in CI. #140 separately owns budget activation/refusal.

## 15. Inherited-deadline conformance gates (ADR-0022)

The named future release contracts and all case variations are [data-model section 15.3](data-model.md#153-recovery-and-future-acceptance-contracts). No runtime or tests are implemented in this documentation change; actual documentation CI does not execute these cases.

Separate three evidence layers: configured fake runtime/common executors prove clock/authority decisions at injected phase barriers; PostgreSQL/SQLite integration proves scope/attempt persistence and atomic transitions; real trusted Linux/macOS local processes and Linux Docker demonstrate supported interruption/escalation and domain stop behavior. A metadata timeout/latch or fake receipt proves no OS behavior. Independent runtimes must demonstrate their declared local deadline placement and clock assumptions before shipping; no same-user hostile containment is required of local.

The suite covers node/run/nested-wrapper precedence and ties, no-limit binding, pre-submit/queued-expired work, both plan placements, preparation/input/credential/inspection/finalization phases, scope retry and rematerialization, exact expiry boundaries, late result rejection before timeout pass, and read-only replay/reconciliation after expiry. Delayed cancel and adoption during/after grace must preserve the original escalation threshold. Clock adjustment, changed installation configuration and sticky expiry cannot restore authority or extend installed timers. Control failure/suspension/registry loss retains ADR-0017 uncertainty and ADR-0020 reservations; the suite cannot infer closure from elapsed time. Use injected clocks/randomness, explicit synchronization and conforming protocol fakes, never patched transports, mocks or sleeps.
