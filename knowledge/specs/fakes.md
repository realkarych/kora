# Fake harness, fake runtime and the testing composition

Status: normative design under [issue #136](https://github.com/realkarych/kora/issues/136); implementation plan approved by the project owner on 2026-10-21, with the owner's clarification of worker loss and runner death. Implementation and executable evidence are pending, in work items 7, 10, 14, 17 and 20 of the [implementation plan](../../docs/implementation-plan.md); the static import rule of section 2 is the exception and runs in CI.

The [testing rules](../../AGENTS.md#testing) forbid mocks, so almost every test runs on fakes: real implementations of kora protocols, selected by constructor injection and checked by the same conformance suites as production implementations (NFR-10.1, [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md)). This specification is their contract: where they live, how a serving process is kept free of them, what a test can script and what it cannot, how virtual time advances, and which conformance cases a fake can pass. The shapes it uses are those of [protocols.md](protocols.md) sections 2 and 3, the worker routes of [api-and-agent-surface.md](api-and-agent-surface.md#worker-route-bodies-issue-139), the scenarios of [dsl.md](dsl.md#4-testing-graphs-without-model-calls-nfr-101) and the recovery sequence of [scheduler.md section 3](scheduler.md#3-submission-execution-uncertainty-and-recovery).

## 1. Packages

Every kora fake is a module named `fake` in its plugin family and ships in the one distribution ([ADR-0013](../../docs/decisions/0013-implementation-stack.md)). The table is exhaustive; a new fake enters it in the change that adds it.

| fake | module |
|---|---|
| fake harness | `kora.plugins.harness.fake` |
| fake runtime | `kora.plugins.runtime.fake` |
| directory-backed VCS, and later the in-process code-host fake | `kora.plugins.vcs.fake` |
| scenario secrets provider | `kora.plugins.secrets.fake` |
| fake approval channel | `kora.plugins.approval.fake` |

- **`kora.testing`** holds the scenario engine, the testing runner of section 3, `VirtualClock` and `QuiescenceMonitor` (section 6).
- **Layer table.** The family rows and the `kora.testing` composition root of [ADR-0026](../../docs/decisions/0026-package-layers.md) already cover these modules, so the [package layer table](../architecture/detailed.md#package-layers) gains no row. Section 2 adds one import rule.
- **The directory-backed VCS is a fake.** It has no remote, no review and no code host, so it is not production-eligible and is excluded with the others. "The fake VCS" in the specifications names `kora.plugins.vcs.fake`.

## 2. Fakes never reach a serving process

A process that serves the API or runs a worker never selects a fake runtime, harness or VCS. Each rule below closes one path.

- **Never discovered.** No kora fake is a `kora.provider` entry point. `load_provider` refuses the reserved name `fake` in every protocol, an entry whose module lies within `kora.plugins.<family>.fake`, `kora.testing` or `kora.conformance`, and an implementation defined in that set, all with `provider_reserved` ([provider loading](protocols.md#provider-loading-issue-150)). A third-party distribution therefore cannot register a kora fake as a provider. Its own code can still import a fake or subclass one, because installed providers remain trusted cooperative code (TM-12 of the [threat model](../../docs/threat-model.md)).
- **The static import rule.** Enforced by `tests/architecture/test_import_layers.py` with a seeded self-test:
  - only `kora.testing`, `kora.conformance`, the fake modules themselves and `tests/` import a fake module;
  - `kora.standalone` imports none of `kora.testing` and `kora.conformance`, and `kora.__main__` imports them only inside its top-level command functions `test`, `conformance` and `mcp` (the stdio authoring server, for `kora_test`), never at module level; dry-run is deferred by [the first-slice contract](first-slice.md), and any future import exception requires its own owner-approved author-side testing implementation;
  - composition roots make no dynamic-import call (`importlib.import_module`, `importlib.util.spec_from_file_location`, `importlib.util.module_from_spec`, `__import__`, `pkgutil.resolve_name`, `runpy.run_module`, `runpy.run_path`), whatever its argument;
  - a fake module imports no fake module of another family, and shared code never lives in a fake module.

  The rule sees static imports and literal dynamic imports. A non-literal dynamic import outside provider loading and outside the roots, and an entry point's `load()` outside provider loading, are not detected by it, as [ADR-0026](../../docs/decisions/0026-package-layers.md) states for every dynamic import; review checks it, and `test_serving_process_refuses_fake_providers` proves at run time that serving processes hold no fake module.
- **Runtime footprint.** The API, worker, `kora standalone` and production runner entry points, started under `python -I`, hold no fake module, no `kora.testing` and no `kora.conformance` in `sys.modules`.
- **The production runner** loads its harness only through `load_provider`, so it can never host the fake. A workload, configuration or environment naming `fake` fails closed.
- **The runner launch command.** A runtime receives the command that starts its runner by constructor injection from its composition root ([local runtime](protocols.md#local-runtime-issue-123), "Spawn"). Production roots pass only the production `kora-runner`; `kora.testing` and `kora.conformance` pass the testing runner. No configuration key, CLI flag, environment variable, `kora.toml` entry or workload field selects it.
- **The `kora.standalone` seam.** Its provider injection is a Python constructor parameter only; no configuration key, CLI flag, environment variable or `kora.toml` entry maps to it.
- **`/plan` and the dry run.** `POST /namespaces/{ns}/plan` computes the materialised plan from the target runtime's declared capabilities without constructing any runtime, and executes nothing. The dry run is not an API operation: it runs author-side through `kora.testing`, and its placement and activation belong to the future author-side testing implementation under the first-slice deferral. Both are deferred from slice 1, so no implemented behaviour changes.
- **Capability flags and the evidence stamp.** Inside `kora.testing`, fake capability flags satisfy validation and admission checks, because tests must exercise acceptance paths; outside it no fake exists to declare them. The testing composition stamps `evidence: fake` on every attempt record, report and `kora test` result. The stamp is its own field, distinct from `CommandResult.resource_protection`, and comes from the composition, never from configuration, so nothing clears it. Worker start-up accepts a fake runtime's protection declaration only under the testing composition.
- **Every `kora.testing` composition** (scenarios, the dry run, integration tests):
  - publishes only to a local bare repository created in its temporary directory, with no remote;
  - runs `git` with a scrubbed environment and its own `HOME`;
  - builds its configuration explicitly, never from the environment or `kora.toml`;
  - has the scenario secrets provider as its only secrets provider, so it resolves no production secret;
  - uses a temporary SQLite standalone database and writes nothing durable to any other database;
  - causes no external effect beyond the commands a scenario runs, under the network rules of [dsl.md section 4.2](dsl.md#42-the-engine-isolation-and-time).
- **Fixture files** are loaded into the `ScriptBook` (section 4) from the author's checkout before execution. A fixture changed in a candidate workspace changes nothing, and the fake harness never reads outcomes from the candidate workspace, its files or its environment.

## 3. The testing runner and the scenario composition

The fake harness has to run inside a real runtime's runner: ADR-0005 and the end-to-end conformance case run the canonical graph on the fake harness against the runtime under test, and scenario commands run for real on `local` or `docker` (dsl.md section 4.2).

- **The testing runner** is `python -I -m kora.testing.runner`, a runner entry point composed by `kora.testing`. It is the real runner core with two differences:
  - it hosts the fake harness with the attempt's own `FakeScript`, never the whole `ScriptBook`; the script travels through the runtime's private transfer channel as a `TransferItem` of kind `script` ([protocols.md section 2](protocols.md#2-runtime-fr-81)), transferred once in `preparation`, materialised under the runner's control directory outside every candidate-visible root, recorded with its digest in the private checkpoint and re-hashed before the harness starts; it never comes through the candidate workspace or the environment, and the production runner refuses kind `script`;
  - it applies the command overrides of the same book.

  On `local`, which has no isolation, "the candidate cannot choose its outcome" holds only within same-user trust ([ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md)).
- **The fake runtime** runs the same testing-runner core in process (section 5).
- **Scenario composition.**
  - Agent workloads are rebound to the in-process fake runtime with the fake harness, so their runner timers live on the scenario's `VirtualClock`. That fake runtime mirrors the configured runtime's declared capabilities, with `plan_executors = {"runner"}`, so validation and the materialised plan match production; every result keeps the `evidence: fake` stamp. The rebinding happens in the `kora.testing` composition, as dsl.md section 4.2 rebinds secret aliases.
  - Command workloads run for real on the configured runtime (`docker` or `local`); a selector-matched `override` instead supplies `{exit_code, output}` through the fake runtime. This keeps dsl.md section 4.2's `network:` and `filesystem:` reporting and its watchdog.
- **The testing runner on a real runtime** serves the end-to-end conformance case. It runs on the real clock, so stall, deadline, `pause` and policy-timeout cases are fake-runtime cases only; it refuses a script containing a `pause` step, and a composition whose policy is built to time out, at submit, with `requires_virtual_time`.
- **Command deadlines.** A command that runs for real on a real runtime gets the remaining time of its inherited deadline, `D` minus the virtual `now` at submit, as a deadline on the real clock, so the thresholds of [scheduler.md section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022) fire after that much real time.
- **Harness identity.** The testing composition substitutes the fake for whatever harness the IR names, `claude` or `codex` in slice 1, on the worker side (`build_session_spec`, start-up version checks) and on the runner side. Validation still checks the graph against the named adapter's metadata, as compile does through `check_config`, so a graph the real harness would reject with `IR019` or `IR026` fails in a scenario too; the fake's own declared metadata governs only its own execution. `SessionResult` records the harness as `fake`, under the `evidence: fake` stamp. Fake evidence never stands in for a real adapter's readiness evidence.

## 4. The fake harness

**Replay belongs to the real adapter.** Replaying recorded SDK streams is the replay layer of each real adapter's conformance ([Claude adapter contracts](protocols.md#claude-adapter-contracts-issue-125)). The fake harness never parses an SDK stream. It reads one ordered script and never chooses an enforced outcome.

**`FakeScript`** is a closed model `{steps, end, on_interrupt}`: one ordered list of steps, how the session ends, and how it answers an interrupt.

| step | what it does |
|---|---|
| `event` | emits a `NormalisedEvent` of kind `turn`, `provider_limited`, `provider_retrying` or `error`, validated against the event rules of protocols.md section 3 |
| `tool_call {tool, input_shape, outcome?}` | first runs the adapter exception order of protocols.md section 3; once `max_events` is reached it denies the call and emits nothing counted. Otherwise it emits `tool_call` and asks the real `PolicyClient` (or takes the output channel), then emits `permission_denied`, or a `tool_result` from `outcome` (`is_error` and content, used only when allowed); an allowed call without `outcome` stays open, with no `tool_result` |
| `undecided_tool_result {is_error}` | a tool result for a `tool_use_id` the policy never decided; a non-error result is a bypass at once, and an error result is held until the `result` step and classified by the shared bypass rule |
| `harness_denial {tool}` | a `permission_denied` with `source: harness`, so the `denials` and `denials_complete` paths run |
| `usage` | a shared typed usage observation with identity/order, missing-field and attribution state; the common runner deduplicates and prices it with the saved map |
| `output` | a structured-output candidate through the `StructuredOutput` channel, or result text |
| `change` | files written into the workspace during execution, committed by the real VCS plan |
| `pause` | waits until an interrupt or the end of time (section 6) |
| `result {status: success \| error}` | a closed, harness-neutral ending with no native-limit subtypes, so a script cannot reach an enforced class through it. It emits the `result` event with `{subtype: success, is_error: false, terminal_reason: null}` or `{subtype: error, is_error: true, terminal_reason: null}`; `success` maps to success and then output validation, and `error` maps to `harness_error` |

**`end`** is closed to `completed`, `cli_exit_without_result` and `oversized_message`. Validation refuses, with `script_end_invalid`, an `end: completed` whose last step is not `result`, and any other `end` after a `result` step. Every other cause comes only from the code that produces it, so that code is what is tested:

| cause (class) | where it comes from |
|---|---|
| `max_events` (`events_exhausted`) | more limited events than `max_events`, through the adapter-side deny and the runner count |
| `stall` (`stalled`) | a `pause` after a completed call, held past `stall_after` |
| `deadline` (`timeout`) | the inherited deadline; a `pause` after an open `tool_call` extends the stall window to the deadline ([scheduler.md section 4](scheduler.md#4-heartbeats-stall-detection-and-timeouts)) |
| `budget` (`budget_exhausted`) | priced `usage` past `budget_usd`; pricing follows [data-model section 7](data-model.md#7-usage-records-and-completeness-fr-91), while enforcement/refusal and unknown-pricing policy remain pending [issue #140](https://github.com/realkarych/kora/issues/140) |
| `provider_limit` | a `provider_limited` event whose reset lies past `stall_after` |
| `cancel` | run cancellation through the runner's cancel path, answered by `on_interrupt` |
| `policy_bypass` | an `undecided_tool_result` step |
| `policy_error` (`config_error`) | a test-composed `PolicyClient` that raises |
| `policy_timeout` (`infrastructure`) | a test-composed `PolicyClient` that times out |
| `journal_unavailable` | a test-composed faulty sink |
| `launch_rejected`, `launch_failed`, `launcher_died`, `integrity_mismatch`, `version_mismatch`, `plugin_mismatch`, `config_check_failed` | never produced by the fake; the fake allocates no launch from its `LaunchContext` and runs no installed-version or configuration check, and these causes are covered by real adapters only |

Three classes are found after a `completed` session, by the code that checks the result: `artifact_invalid` from an invalid `output`, validated by the runner, and `protected_path_modified` and `secret_leak` from a `change` that touches a protected path or contains a declared dummy secret, found by inspection.

- **Interrupts.** The fake awaits `sink.termination()` beside its step loop, a held `pause` included, and its completion is the interrupt; between steps it also reads `sink.enforcement()`. An interrupt cancels a held `pause`. `on_interrupt: acknowledge` emits a `result` and ends, with `termination: interrupted`. `on_interrupt: ignore` keeps the session running; after grace the runner stops the step loop through the fake's own kill handle, and `run()` still returns through `finalise(summary, outcome)` with `termination: killed_after_grace`.
- **Usage completeness.** A `result` can establish `complete` only with usable complete observations; no usable usage remains attempt-level `unavailable`. Explicitly reported zero is distinct from no report. Interruptions and attribution, missing-field or observation gaps remain partial/unavailable after a result. The common runner consumes the shared typed observations and identity/order; duplicate/cumulative observations are not added unconditionally. Scripted cases cover the immutable map, missing/equal/unequal TTL dimensions, unknown models, replay and source-observation age under [data-model section 7](data-model.md#7-usage-records-and-completeness-fr-91). Work item 7 owns these conformance cases; `evidence: fake` never establishes real Claude/Codex compatibility.
- **Shared code lives in `kora.runner`, not in an adapter:** the harness-neutral `RunnerOutcome` cause-to-class step (each adapter keeps its own native stop-reason mapping, such as the Claude column of [protocols.md section 14](protocols.md#14-harness-mapping-tables-fr-33-dd-27)), usage completeness, output validation, bypass classification and the adapter exception order. Each adapter builds its own `EventSummary`; the fake fills the result, usage, decided-call, denial and output-channel fields from its steps, and `finalise(summary, outcome)` reads them. The fake has no native stop-reason column: its `result` step maps as the step table states.
- **Script source and key.** The `ScriptBook` key is the full selector path with an attempt on every segment, for example `candidates[1]#2/implement@1`. The runner derives it from the attempt's `node_path`, which embeds sub-graph attempts ([data-model.md](data-model.md)), and from the runtime attempt number in the saved request. A script binds to an attempt id when it is consumed, and the consumption is recorded in the private checkpoint, so an adopting worker never consumes it twice.
- **Caps.**
  - Only the addressed attempt's script crosses the transfer channel, streamed lazily. A `ScriptBook` holds at most 10,000 scripts and 1 GiB in total, and a fixture record line is at most 1 MiB. The book is file-backed: an attempt's script is loaded when it is consumed.
  - A script holds at most 100,001 steps, one past the runner's tracking ceiling of protocols.md section 3, delivered lazily.
  - A `change` is at most 1 MiB per file and 64 MiB per script.
  - Steps left over after enforcement stopped the session count as consumed by enforcement.

## 5. The fake runtime

The fake runtime implements the same [bounded grant-history and recovery DTOs](protocols.md#resumable-retained-dispatcher-history-issue-226), including receiver checkpoints, exact delivery dispositions and final frozen observations. Its conformance cases use injected clock/barriers for deadline, interruption and append traces. A fake passing does not establish local peer authentication, filesystem durability, process exit or resource release; those require the real-runtime cases. This capability remains pending with runtime and worker implementation.

- **What it runs.** It implements the `Runtime` protocol of protocols.md section 2 in process and hosts the testing-runner core with the injected harness. Its `plan_executors` is `{"runner"}`. Command workloads take a scripted `{exit_code, stdout, stderr, output_file}`; resources are still materialised and re-hashed (protocols.md section 2, "Command resources").
- **The same protocol shape as the local runtime** ([local runtime](protocols.md#local-runtime-issue-123)):
  - `submit` returns in phase `preparation` and starts no candidate; `transfer_in` is allowed only in `preparation`; `start` re-hashes every input and starts the candidate;
  - `quiescence(handle)` returns the `NormalCompletionQuiescence` record after the candidate exited and every `finalize` settled;
  - `recovery(attempt_id, grants)` takes every recorded grant, the claim's own included, and returns the recorded evidence, `normal_completion` included, also when no handle exists;
  - `record_dispatcher_quiescence(attempt_id, acknowledgement, grant)` checks the acknowledgement against the grant's bound process;
  - the registry states `unsubmitted`, `starting`, `running`, `completed`, `retiring` and `retired`, `released_at`, and the operations-by-state table, apart from its full-disk cells, since the fake runtime has no disk to fill: `retire` with no entry writes a `no_runner_started` tombstone that a delayed `submit` finds, and `release` keeps the tombstone.
- **The fake domain model.** A `FakeHost` owns a durable registry and a journal store, and workers attach to a host. `portable_handles` decides whether another host's worker may adopt. `lose_registry()` starts a new lineage, and the old one stays unreachable: a retryable `KoraProtocolError`, never `None`. `release` deletes only the bulky roots; registry, fences, receipts and acknowledgements remain. A recovering worker reaches state only through the `Runtime` protocol, never through a shared path.
- **Answers come from the registry.** `lookup` and `retire` answer from the registry's real state. Controls inject only `unreachable` or `unknown`, as retryable errors, never an affirmative answer. A receipt is issued only after the fence is installed and every managed fake operation has settled, and repeating `retire` returns the same receipt.
- **Named points:**
  - received before registry serialization, for `workspace_target`, `submit` and `finalize`;
  - submit accepted before the handle returns;
  - each phase entry of protocols.md section 2;
  - `finalize` dispatched and settled;
  - after `completed` is recorded.

**Worker loss and runner death are separate controls**, so the fake never recovers more than the real local runtime does.

- **`lose_worker(dispatcher, point)`** ends the worker's call at a named point, as a worker death or a lost connection, and cancels and awaits that worker's heartbeat and call tasks; the fake runner stays alive, and a runner-side operation the lost call started stays pending. Before the runner exists, at `workspace_target` or `submit` received before registry serialization, the next worker finds no entry or a `starting` entry: `lookup` returns `None` or is retryable, `recovery` returns the recorded evidence, and the worker either submits idempotently or retires with a `no_runner_started` tombstone. From submit accepted onward, a later worker adopts the live runner, and adoption stays observational until every earlier grant closes (scheduler.md section 3, step 6). Preparation resumes from its checkpointed result; before any preparation checkpoint, the runner repeats idempotent preparation against the recorded plan digests. From `sealing` onward the runner resumes from the sealed or prepared checkpoint without a new commit.
- **`lose_runner(point)`** kills the fake runner, at each phase entry, at `finalize` dispatched or after `completed` is recorded; it cancels and awaits the runner's tasks and removes its timers from the `VirtualClock` wake set at the death point. A publication operation already issued is owned by the `FakeHost` and the VCS plan, not by the runner, so it survives the death and is what read-only reconciliation reads. Nothing restarts the runner in any phase: no new runner starts under the attempt id, preparation is not rerun, the harness script is not replayed, and no useful execution or publication resumes. `status` reports `lost`, and the worker follows the retirement and `lost` path. Terminalization still needs the closure evidence of the terminal report: the `TerminationReceipt` with its scope, every dispatcher closure and the publication outcome; missing evidence leaves recovery unresolved. Retained logs, results and checkpoints stay readable, and an operation already issued, such as a `finalize` dispatched before the death, is reconciled read-only.
- **Normal exit.** The fake runner exits as the local one does, on the `VirtualClock`: once `completed` or `retired` is recorded, when its last follower disconnects or 60 s after its last request, and before that no idle timer applies. A runner that exits after recording `completed` stays completed, `lose_runner` at the point after `completed` included: `status` reports `exited` in phase `complete`, and `recovery` returns the normal-completion record.
- **Counts.** Both controls are one-shot, or counted with an explicit count, so no worker or runner is lost at the same point without end.

**Process identity.** The testing composition injects a per-incarnation source of `ProcessIdentity` `{host_id, boot_id, pid_namespace, pid, start_time, leads_group}`. `process_exit(dispatcher)` yields `runtime_observed_process_exit` only for an identity it injected with `leads_group: true`, with no live `spawn_effect(dispatcher)`, as a surviving effect subprocess keeps the grant open on `local`, and with no call of that dispatcher still pending at the fake runner: a call held at a barrier, such as a cut-off `finalize`, makes the evidence refuse retryably until it settles, as local condition 5 does. The API's evidence binding refuses the same items as it does for a real runtime.

**Evidence it produces,** in the shapes of protocols.md section 2 and the terminal report:

| evidence | how the fake produces it |
|---|---|
| `NormalCompletionQuiescence` | on normal completion, with `scope: fake_runtime` |
| `registry_acknowledgement` | returned by `recovery()` after `record_dispatcher_quiescence` |
| `runtime_observed_process_exit` | `process_exit(dispatcher)` for an injected identity with `leads_group: true`, no live `spawn_effect` and no pending call |
| `TerminationReceipt` | `retire` after the fence is installed and every managed operation settled, with `scope: fake_runtime`, also after `lose_runner`, where the fake answers from registry state as the local worker-side fallback does; an injected `unknown` leaves the retirement inconclusive, with no receipt and recovery unresolved |
| `TerminationReceipt` with `scope: no_runner_started` | `retire` with no entry, or in `unsubmitted` or `starting` |
| `PublicationOutcome` `not_applicable` | a workload without publication |
| `PublicationOutcome` `not_dispatched` | a retire before `finalize` is dispatched |
| `PublicationOutcome` `unknown` | a worker loss or runner death at `finalize` dispatched; the cut-off operation stays pending while a barrier holds it (with either `holds_time` value), and once released it settles to the VCS plan's actual result, which `recovery()` then reports as `completed` or `failed` |
| `PublicationOutcome` `completed` | `finalize` settled |
| `PublicationOutcome` `failed` | a VCS plan that fails |

The fakes end an attempt through the same `TerminalReport`, `ClosureEvidence`, `report_result` gating, write-once report and evidence binding as a real worker ([worker route bodies](api-and-agent-surface.md#worker-route-bodies-issue-139)).

**Lifecycle.** The `FakeRuntime` owns a temporary root under `tmp_path` and its tasks. `release` during a test deletes only the bulky roots of a retired or completed handle. `close()` first releases every held barrier and cancels every held `pause`, then cancels and awaits every task and removes every timer it registered, then removes the whole temporary root, every lineage included (old registries, journal stores, tombstones), whether or not a handle was retired.

## 6. Time, randomness and quiescence

- **In `kora.protocols`, with production implementations:** `Clock` (`now()` in UTC, `monotonic_ns()`, `sample() -> ClockSample` and an async `sleep_until(deadline_ns)` through which every timer is registered) and `ActivityReporter` (`span(name)`, an async context manager released on every exit path, including cancellation and a lost worker or runner; the production implementation does nothing). `RandomSource` lands with its first consumer, the jitter of [scheduler.md](scheduler.md) sections 6, 7 and 19; production draws come from `random.SystemRandom` or `secrets`, and a scenario takes its seed from an optional `seed` field of the scenario file or a fixed default, which the `kora test` result records. They feed `tick(clock, random)` and `step(clock, random)` of [scheduler.md section 12](scheduler.md#12-execution-backend-boundary-nfr-55).
- **`VirtualClock`** (in `kora.testing`) implements `Clock`; `now()` and `monotonic_ns()` move together. It offers `advance(ns)`, `adjust_wall(offset)` for the clock-fault cases of [scheduler.md section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022), and `advance_to_next_wake()`, used by the scenario engine. Its `now` also serves as the diagnostics clock.
- **Span ownership.** The runtime operation owns the attempt span, not the worker. A lost worker or connection ends only that worker's call spans. A lost runner ends its span, and no restart opens a new one. A span ends at the runner's recorded phase result (`complete` or `failed`), at retirement or at `close()`; the worker's later calls, such as its terminal report, hold their own call spans.
- **`QuiescenceMonitor`** (in `kora.testing`) implements `ActivityReporter`. Activity counts from submit to the runner's recorded phase result, not per call, so a real command's subprocess is visible to it. The runner releases an attempt's span while the session is parked in a `pause` or a `holds_time: false` barrier, while it has no work of its own and waits for a worker operation (`start`, `transfer_in`, `finalize`, `quiescence`), and during the grace after an interrupt the session ignores; it takes the span back when the session resumes, the operation arrives or the grace ends.

**Quiescent** means all of these at once: the span count is zero; no attempt is claimable; no run's `needs_scheduling` marker is set; no in-process wake-up event is pending; every remaining wait is a timer. The database facts are read through `kora.db` read functions. `settled` ([dsl.md section 4.1](dsl.md#41-scenarios)) is quiescent with no attempt claimed or running, no preparation, publication or result report in flight, and no open approval or wait, or the run is terminal.

**When quiescence is evaluated.** The engine evaluates it only when the span count changes, the wake-up event is set, or a barrier, an action, a worker call or a `tick` completes, and never loops on database reads.

- **Waits that hold no count:** a scripted `pause`, and a `barrier` declared `holds_time: false`. A barrier with `holds_time: true` freezes time until the test releases it.
- **The wake set:** durable `wake_at` values (waits, retry back-off, admission recheck), and the due rows of the observation dispatcher, notification delivery and retention once those components exist, until when the engine refuses graphs that need them; lease expiry; deadline thresholds; the runner's stall and deadline timers; the runner's termination-grace and kill-escalation timers; the fake runner's idle-exit timer. Idle polling (the scheduler's idle tick, claim intervals, sweeps) and heartbeats are not wakes; the in-process wake-up event or the engine's own `tick` call drives polling.
- **After each advance**, every active worker that is not held at a barrier heartbeats at the new time before lease expiry is evaluated. A lease-expiry test therefore holds its worker at a barrier; holding a worker at a barrier also stops its heartbeat task.
- **Wakes at one instant** follow one total order: deadline thresholds; the runner's deadline timers; the runner's stall timers; the runner's termination-grace and kill-escalation timers; lease expiry; retry back-off; admission rechecks; the fake runner's idle-exit timer; other waits by `wake_at`. Within each step, ties are ordered by the owning object's id. So a stall and a deadline due together end as `timeout`.
- **The engine loop.** At each quiescent point it fires the next armed action if its condition holds; otherwise it advances to the next wake; otherwise the scenario ends and reports its open waits.
- **Watchdogs.** `kora.testing` owns a real monotonic clock, separate from the injected one. It bounds every test-side wait, barrier hold and advance: 10 s by default, and 60 s per real command (dsl.md section 4.2), configurable per scenario up to 300 s; a larger value is refused at load with `watchdog_above_ceiling`.
- **One release path.** Every watchdog expiry, `scenario_real_time_limit` and `quiescent_without_wake` runs the same release: release every barrier and `pause`, terminate every process group, wait a grace of 5 s, kill what remains, cancel every task, and bound each of those awaits by the real watchdog clock. A holder still present after it is reported by name as a failure.
- **Failures with named errors:**
  - `quiescent_without_wake`: an integration test is quiescent, with no wake and no action, before its condition holds;
  - `virtual_time_step_cap`: 10,000 advances in one run;
  - `scenario_real_time_limit`: 120 s of real time per test or scenario, a provisional value that work item 7 replaces with the measured one from the virtual-time cost measurement, and never below the longest configured command watchdog plus 10 s; a command watchdog that expires first reports `watchdog_expired`;
  - a barrier still held at the end of a test is reported with its holder.
- **`wait_until(condition, deadline_ns)`** is async and event-driven: the condition notifies, and nothing polls.
- **The seed field.** The optional `seed` field enters [scenario.schema.json](scenario.schema.json) with `RandomSource`'s first consumer; until then a scenario has no `seed`.
- **Per-instance state.** Clock, monitor, book, registry and controls belong to their instance, with roots under `tmp_path`. There is no module-level state, so tests can run in parallel.

## 7. Scenario strictness and error classes

- **Unused entries.** The scenario engine, not the fake, checks for unused entries at run end; an unused entry is an execution error unless it is `optional`. Duplicate and ambiguous keys are execution errors.
- **Error classes.** Each scenario `errorClass` value translates as follows for an agent selector:

  | `errorClass` | translation |
  |---|---|
  | `events_exhausted`, `stalled`, `budget_exhausted`, `artifact_invalid`, `protected_path_modified`, `secret_leak` | the producing steps of section 4 |
  | `timeout` | the engine inserts a `tool_call` without `outcome` for a tool the addressed node's effective policy allows, which stays open, followed by a `pause`, so the deadline, not the stall window, ends the session; with no allowed tool the scenario fails as an execution error |
  | `harness_error` | `end: cli_exit_without_result` |
  | `infrastructure` | the engine inserts a `tool_call` step, and the composition's policy times out for the addressed attempt only (`policy_timeout`) |
  | `config_error` | the same, with a policy that raises (`policy_error`) |
  | `command_failed`, `attempt_failed` | refused for agent selectors |

  A class whose enforcing limit is not configured on the addressed node is an execution error, for example `timeout` without a deadline. Stall, timeout and pause cases run where agent workloads run on the fake runtime, which is always the case in scenarios (section 3).
- **Dry-run fixture records** carry a selector key. Dry-run behavior activates with the future author-side testing implementation.

## 8. Conformance applicability

Each case below has one of three values for the fakes: **passes**, protocol evidence; **not applicable**, with the reason; **never claimed**, an isolation or security property only a real implementation shows. A conformance run of a fake passes exactly the cases marked `passes`.

**Suite outlines** ([protocols.md section 13](protocols.md#13-conformance-suites-nfr-102)):

| case | fake runtime | fake harness |
|---|---|---|
| Runtime outline: idempotent submit, absence versus unreachable lookup, original-domain adoption and journal offset, portable recovery state, local deadlines and cancellation, one handle through publication, late `finalize`, preparation crash recovery as a worker loss | passes | — |
| Runtime outline: constructed candidate environment, no candidate-visible publication credential | never claimed | — |
| Every case or leg that needs a `worker_host` executor | not applicable: the fake runtime's `plan_executors` is `{"runner"}` | — |
| Harness outline: normalised events, usage completeness, structured output and its fallbacks, policy hook before every tool call | — | passes, from scripted steps instead of recorded SDK streams |
| Harness outline: `max_events` at the exact count | — | passes, through the adapter-side deny and the runner count |
| Harness outline: recorded SDK streams, hermetic behaviour, installed version against `tested_versions`, skill-directory and binary digests | — | not applicable: real-adapter evidence only |
| End to end: the canonical graph on the fake harness against the runtime and VCS under test | passes, with the testing runner on the runtime under test | passes |

**Execution-recovery implementation gates (ADR-0017):** every case passes on the fake runtime, with crashes injected as `lose_worker` and runner losses as `lose_runner`, except two. `test_partitioned_live_host_is_not_assumed_stopped` is not applicable: it needs a real local session. `test_runtime_stop_does_not_imply_publication_quiescence` passes for its runner legs, and its worker-host publisher leg is not applicable.

**Publication authority and recovery contracts:**

| case | value |
|---|---|
| `test_publication_destination_is_trusted`, `test_publication_records_uncommitted_changes_once`, `test_finalize_recovers_prepared_revision` (crashes as `lose_worker`), `test_publication_authorization_is_late` | passes |
| `test_non_git_inspection_stream_scans_intermediate_history` | passes, on the directory-backed fake VCS |
| `test_publication_preserves_candidate_commits`, `test_publication_sealing_rejects_mutable_aliases` | passes for the protocol legs with real Git; the local process legs are not applicable |
| `test_candidate_cannot_forge_runner_journal`, `test_candidate_git_configuration_cannot_execute_in_publisher`, `test_agent_descendants_cannot_access_publication_credentials` | never claimed |
| `test_required_execution_protection_rejects_local`, `test_harness_launcher_preserves_control_boundary` | not applicable: a `local` case and a real-harness case |

**Command-resource contracts (issue #45):**

| case | value |
|---|---|
| `test_resources_run_without_graph_package`, `test_graph_runs_on_repository_without_gates`, `test_resource_digest_mismatch_fails_attempt` | passes |
| `test_compile_exit_then_deploy`, `test_resource_set_digest_vector`, `test_changed_content_same_name_is_new_version`, `test_deploy_rejects_missing_or_mismatched_resource` | passes for their runtime legs; the compile and deploy legs do not depend on the runtime |
| `test_unprotected_runtime_reports_protection_none` | passes: the fake reports `resource_protection: none` |
| `test_candidate_cannot_alter_resources` | never claimed |

**Local runtime contracts (issue #123):** the fake runtime passes the cases whose behaviour it mirrors: `test_worker_restart_adopts_live_session` (through `lose_worker`), `test_live_old_worker_keeps_adoption_observational`, `test_unconnected_grant_closed_by_process_exit`, `test_worker_child_effect_keeps_dispatcher_open` (through `spawn_effect`), `test_stale_incarnation_claim_binds_its_own_process` and `test_completed_attempt_never_looks_lost`. It passes the protocol legs of `test_lost_runner_is_reported_not_restarted` (through `lose_runner`), `test_receipt_names_its_scope` and `test_registry_states_and_release`; their file, sweep and fallback legs (leftovers, cleared environments, daemons, unverified members, lock and temporary registry files) are not applicable. Every other case of that table tests a local mechanism (host and pid namespace, process groups, the control channel, the journal files, transfer files, the launcher, ownership checks, the service manager) and is not applicable.

## 9. Diagnostics

The fakes are providers: they use the injected `ProtocolContext.logger` under the runtime and harness rows of [observability.md section 7](observability.md#7-component-coverage-and-activation), activated in work item 7. Script content is test data and never reaches diagnostics, only the script key and counts. A watchdog failure reports its holders by name.

## 10. Contracts and measurements

These are future executable contracts; documentation CI runs only the static import rule. Every database leg runs on PostgreSQL and SQLite.

| contract | evidence required when implemented | work items |
|---|---|---|
| `test_serving_process_refuses_fake_providers` | `provider_reserved` for `fake`; a subprocess with a generated `*.dist-info` registering `harness.fake` to a kora fake module, and a third-party `runtime.fake`, is refused with the module absent from `sys.modules`; serving entry points under `python -I` hold no fake, `kora.testing` or `kora.conformance` module | 6, 7, 17, 20 |
| `test_fake_import_rule` | the static rule of section 2, roots included, with its seeded self-test, including the `mcp` and `async def` command functions; `test_source_tree_respects_package_layers` applies it to the source tree | active since issue #136; the fake modules of work item 7 fall under it |
| `test_production_runner_refuses_fake` | a production runner given a workload, configuration or environment naming `fake` fails closed | 14 |
| `test_testing_runner_hosts_fake_on_real_runtime` | the canonical graph runs on `local` through the testing runner with the fake harness and real commands | 7, 14 |
| `test_fake_harness_reads_only_its_script` | a script-shaped workspace file and a fixture changed in the candidate workspace change nothing | 7 |
| `test_runner_enforced_classes_come_from_enforcement` | every cause of section 4 arises from its producer, and each post-completion class from its check; `end` cannot name an enforced cause, and `script_end_invalid` refuses an `end` that contradicts the steps; `interrupted` and `killed_after_grace` are both reachable, and `run()` returns through `finalise` after a kill; the testing runner on a real runtime refuses `pause` and a timing-out policy with `requires_virtual_time` | 7, 14 |
| `test_fake_tool_calls_go_through_policy` | the exception order, then the `PolicyClient`; a deny becomes `permission_denied` | 7, 14 |
| `test_fake_runtime_control_points` | every named point and control is reachable and deterministic; a `lose_worker` before registry serialization leads to an idempotent `submit` or a `no_runner_started` retirement, and from submit accepted onward to adoption of the live runner; every `lose_runner` point before `completed` leads to `lost`, retirement and no restart; `lookup` and `retire` never contradict the registry; receipts are idempotent; exit evidence only for injected identities with `leads_group: true`, no live `spawn_effect` and no pending call, and a call held at a barrier refuses retryably; each evidence row of section 5 | 7 |
| `test_fake_runner_death_is_never_restarted` | after a runner death in each phase no second runner starts, no script step runs twice, no preparation reruns and no publication resumes; logs and checkpoints stay readable; a `finalize` already dispatched is reconciled read-only; the attempt stays unresolved until the closure evidence arrives; a runner that recorded `completed` before its exit stays completed | 7 |
| `test_fake_runtime_releases_resources` | after success, error, a worker loss at each point, a runner death in each phase, cancellation, `lose_registry`, a watchdog expiry and `close()` with an attempt in flight or a `holds_time: true` barrier held, no task, thread, timer registration, open span, directory or descriptor is left | 7 |
| `test_virtual_time_advances_only_when_quiescent` | time does not move while any quiescence condition fails, and quiescence is evaluated with a bounded number of database reads per advance; a held pause reaches `stalled` because the span is released while parked; an ignored interrupt reaches `killed_after_grace` through the grace timer; lease expiry during a `holds_time: false` barrier and while the runner waits for `start`; lease expiry and adoption after a worker loss at each point, and retirement after a runner death; the span count returns to zero after success, error, a worker loss, a runner death and cancellation; a stall and a deadline at one instant end as `timeout`; `settled` does not fire while an attempt is parked in a `pause` | 7, 10, 17 |
| `test_armed_action_fires_before_virtual_time_advances` | an armed `waiting` action fires before an approval timeout | 7, 20 |
| `test_unreleased_barrier_fails_within_watchdog` | a never-released barrier and a `quiescent_without_wake` state fail by name within the real-time watchdog, listing the holders, and leave no task, process group, directory or descriptor; a watchdog above 300 s is refused with `watchdog_above_ceiling` | 7 |
| `test_fake_results_carry_fake_evidence` | every record of a testing composition carries `evidence: fake`, including mixed runs; configuration cannot clear it | 7, 17 |
| `test_fakes_pass_their_conformance_rows` | the suites pass exactly the cases section 8 marks `passes` | 7 |
| `test_dry_run_resolves_no_secret` | with a production-like `KORA_DATABASE_URL` and a reachable remote set in the process-start environment, the dry run resolves no secret, touches neither and writes nothing durable; a scenario leg with the local Git backend does the same | the scenario leg in 7 and 20; the dry-run leg pending author-side testing implementation |

| measurement | work item |
|---|---|
| fake runtime and harness lifecycle: repeated cycles of success, error, a worker loss at each point, a runner death in each phase, cancellation, `lose_registry`, a watchdog expiry, a `scenario_real_time_limit` expiry, `close()` with an attempt in flight or a `holds_time: true` barrier held, and the testing runner on `local`, in one process; peak, post-quiescence and per-cycle growth of memory, and the counts of tasks, threads, timers, descriptors, subprocesses, process groups and temporary directories sampled after an explicit cleanup condition; workload, cycle counts and budgets declared before the run, separate process runs, and a base and head comparison | 7; the worker leg in 17 |
| virtual-time cost: advances and wall time per scenario and integration test, on both databases | 7, 10, 20 |
| the share of fake-backed suites in CI wall time against the 10-minute budget | 7, and each work item adding such suites |
