# Structured component diagnostics

Status: normative design under [issue #91](https://github.com/realkarych/kora/issues/91); direction and implementation plan approved by the project owner on 2026-10-18. Runtime implementation and executable evidence are pending. This specification applies [NFR-9.4](../../docs/requirements/non-functional.md#9-observability) and the existing [ADR-0013 stack](../../docs/decisions/0013-implementation-stack.md); it introduces no new provider protocol or service.

First-implementation amendment dated 2026-10-20: the shared bootstrap, the `kora` entry point, the configuration ranges and the process exit contracts landed under [issue #147](https://github.com/realkarych/kora/issues/147); decisions approved by the project owner on 2026-10-20.

Read this contract whenever implementing or changing a component's behavior, logging configuration, collection, context, redaction or output. The [research](../research/orchestrator-logging-patterns-2026-10-18.md) and [planning review](../reviews/structured-logging-plan-review-2026-10-18.md) explain its basis; they do not replace the contract or prove runtime behavior.

## 1. Streams and authority

| Stream | Purpose and owner | Durability and access |
|---|---|---|
| Component diagnostics | Explain operation, degradation and failure; process entry points own output configuration | Best-effort output to operator-controlled sinks, with bounded loss accounting; no execution authority or replay role |
| Domain/audit events and recovery journals | Existing transition owners commit accepted facts and recovery intent | Existing transactional durability, authorization, retention and replay contracts in the [data model](data-model.md) and [runtime protocols](protocols.md) |
| Session output | Existing normalized segments and opaque harness transcripts | Existing content redaction, upload, namespace `transcripts` permission and payload retention under [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md) |

General diagnostics never enter the durable event table. Diagnostic filtering, incident suppression, sink failure or overflow cannot change accepted domain events or recovery records. A diagnostic claiming a committed transition is emitted only after commit; precommit records describe an attempted action. A crash between commit and diagnostic output can lose that diagnostic. Neither diagnostics nor timestamps prove outcome ordering, physical termination or publication authority.

Candidate stdout/stderr, SDK messages and transcript fragments remain untrusted observations. They are never parsed into trusted component records because they resemble JSON logs. Operational stderr is not ingested as session output. The existing `GET …/attempts/{att}/logs` surface continues to serve session content, not operator diagnostics.

The opt-in [observability exporter](protocols.md#9-observability-exporter-fr-113-nfr-92) still consumes durable event rows. Its queue, replay and conformance obligations remain separate. This logging stage adds no collector, log database, public log-search endpoint, console log viewer, remote exporter or tracing dependency. Actual future tracing may contribute trace/span IDs; absent tracing never produces fabricated IDs.

## 2. Record and configuration

Python processes use one typed bootstrap built on the decided `structlog` stack with standard-library logging integration. Entry points initialize it; modules and providers do not install handlers, configure the root logger or use `print` for diagnostics. Reinitialization must not duplicate handlers. Libraries receive or obtain a bound logger without owning output configuration.

Python diagnostics use stderr, defaulting to one JSON object per line. CLI results, `--json` errors and MCP stdio protocol messages keep their existing stdout contract. Local text rendering is an explicit operator choice and uses the same sanitized record; it is not a bypass. A browser diagnostic boundary uses the same levels and field policy, rendering sanitized values through the browser console without a telemetry collection endpoint. Browser instance identity identifies the page lifetime, not an OS process.

### Record schema

| Field | Type and rule |
|---|---|
| `schema_version` | Integer `1`; a changed incompatible shape requires an explicit version |
| `timestamp` | RFC 3339 UTC string ending in `Z`; not a global ordering or authority source |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `event` | Stable name defined by kora or the installed provider's reviewed event catalog; never derived from user content |
| `message` | Bounded static explanatory text; variable values use reviewed typed fields |
| `component` | Stable logical component name, including when standalone shares one process |
| `logger` | Stable package/module logger name; never a per-run dynamic logger |
| `instance_id` | Fresh process/page-lifetime identity from an injected identity source; cannot authorize an operation |
| Available context | `namespace_id`, `graph_id`, `run_id`, `node_path`, `attempt_id`, `session_id`, `worker_id`, `request_id`: validated strings, present only when known and authorized |
| Execution context | Nonsecret `lease_epoch` integer and `execution_domain_id` string when relevant; never bearer tokens, lease secrets or recovery capabilities |
| Diagnostic details | Event-specific reviewed scalar fields and bounded collections: safe error code/reason, route template, outcome, count, duration in milliseconds, suppression count or truncation marker |
| Replay context | `replayed_request_id` when a saved error carries an original request identity, under section 4 |

Each event definition names its owner, level, allowed fields/types and sensitive exclusions. Required fields and canonical context cannot be overwritten by arbitrary extra fields or provider/candidate messages. Missing context is omitted, not filled with invented identifiers. Do not copy a URL, graph document, exception object or configuration dictionary into the record. Collectors ignore unknown additive fields and use `schema_version` for incompatible changes. Tests assert event/level/context and safe fields, not incidental prose or timestamps from the real clock.

### Configuration contract

| Setting | Default and validation |
|---|---|
| Global kora level | `INFO`; only the five named levels are valid |
| Component overrides | Empty; explicitly registered component names and valid levels only |
| Third-party capture | Explicit logger allowlist, default level `WARNING`; each admitted path uses the same content policy |
| Rendering | `json`; `text` is explicit local configuration |
| Maximum record bytes | 16 KiB of serialized UTF-8, including framing |
| Queue limits | Both 1,024 records and 4 MiB of serialized bytes per process apply; either limit can cause a drop |
| Shutdown log drain | At most 1 second; never extends an execution-stop deadline |

Configuration is validated at startup; invalid names, levels, nonfinite/unbounded limits and limits too small for required fields fail visibly. This contract fixes defaults, not a measured performance budget. The first implementation issue must specify finite supported configuration ranges and limits for string bytes, field/collection counts, nesting, frames, active secret registrations and incident keys, with measured cost and tests. It must not accept unlimited settings or arbitrary executable handler imports through untrusted graph configuration. There is no live level-control API in this stage. Runtime dependencies are added only with that first implemented consumer through `uv`, with their existing stack reason recorded.

**First implementation** ([issue #147](https://github.com/realkarych/kora/issues/147)). Every kora process reads its logging configuration from environment variables; `kora standalone` additionally maps a `[logging]` table of `kora.toml` onto the same settings. No CLI flag sets a logging setting.

| Variable | Default | Accepted |
|---|---|---|
| `KORA_LOG_LEVEL` | `INFO` | one of the five level names |
| `KORA_LOG_COMPONENT_LEVELS` | empty | `component=LEVEL` pairs separated by commas; the component must be registered in the process |
| `KORA_LOG_THIRD_PARTY` | empty | `logger=LEVEL` pairs separated by commas; a name without a level is captured at `WARNING`; `py.warnings` is always captured at `WARNING` |
| `KORA_LOG_FORMAT` | `json` | `json`, `text` |
| `KORA_LOG_MAX_RECORD_BYTES` | 16384 | 4096 to 1048576, and at most a quarter of `KORA_LOG_QUEUE_BYTES` |
| `KORA_LOG_QUEUE_RECORDS` | 1024 | 16 to 65536 |
| `KORA_LOG_QUEUE_BYTES` | 4194304 | 65536 to 268435456 |
| `KORA_LOG_DRAIN_SECONDS` | 1 | 0 to 1 |

Fixed bounds of the first implementation, not configurable: 1024 bytes per string value, 32 fields per record, 64 items per collection, nesting depth 4, 256 values per record, 32 frames per exception and 8 exceptions per chain, 256 registered secrets of at least 8 bytes each and 65536 bytes of registered values per process. The walk over a record keeps a cumulative budget of values and string bytes and rejects the record with a counted reason as soon as the budget is exceeded, before redaction runs. Incident keys are tuples of a registered component, event and reason, so their number is bounded by the event catalog.

Invalid logging configuration ends the process with exit code 78 and exactly one line on stderr, written before any sink exists: `kora: invalid logging configuration: <VARIABLE>: <static reason>`, where the static reason names the accepted form and never the value.

For browser diagnostics, the same schema/content/volume rules apply where meaningful; the page owns its bounded incident state. It does not imitate a process pipe, introduce a browser-to-server telemetry route or claim to control the browser's own console storage. Its first diagnostic-producing feature specifies and tests finite client-side work and volume limits.

## 3. Levels and emission ownership

| Level | Use | Avoid |
|---|---|---|
| DEBUG | Bounded scheduling decisions, routine polls/heartbeats and retry detail | Content dumps, per-token/tool payloads or security bypasses |
| INFO | Process ready/stopped, run acceptance, attempt start/finish, committed transitions, scheduled retries, completed cleanup | Every unchanged poll, heartbeat, query or loop iteration |
| WARNING | Unusual recoverable degradation: unavailable provider, stale lease rejection, observation gap, first overload and recovery summary | Ordinary validation failure or every expected retry |
| ERROR | Failed operation with an actionable outcome: exhausted retries, unrecoverable submission/storage failure or unexpected request failure | Treating every failed user command as a control-plane outage |
| CRITICAL | Component cannot continue safely: startup or invariant failure | An ordinary failed run |

Short-lived client processes, the CLI and the stdio MCP server, emit their process lifecycle events at DEBUG, because an INFO line on every invocation would be noise on the user's terminal; long-running processes emit them at INFO (decided under [issue #147](https://github.com/realkarych/kora/issues/147), 2026-10-20).

The boundary that handles a failure owns its diagnostic and safe stack context. Lower layers propagate typed errors; logging and re-raising the same failure through every layer creates duplicate diagnostics. Distinct server and client boundary events may share correlation without each printing the same stack. Expected user errors return their structured result without a traceback. Unexpected errors include a safe exception type and bounded frames after the content policy. Business errors still propagate or produce their specified structured failure; logging never fabricates success.

Rate-limit repeated incidents using bounded keys. First occurrence and recovery summaries carry suppressed counts; poll frequency does not determine INFO volume. ERROR/CRITICAL are not intentionally sampled, but overload or a broken sink can lose records at any level. A component's durable state remains the outcome authority. Suppression, queue overflow and output failure are distinct counters/reasons.

## 4. Context and request correlation

| Boundary | Bind and propagate | Reset and trust rule |
|---|---|---|
| API ingress | Generate current `request_id`; bind authenticated namespace/object scope after authorization | Ignore caller IDs as canonical identity; denied claims remain content-free classifications |
| Async task / sync bridge | Copy the intended bounded immutable context explicitly where propagation is not guaranteed | Reset at operation exit, including exception/cancellation; no shared mutable global scope |
| Scheduler transition | Load scope from the current authorized records and decision owner | Commit determines outcome; a local diagnostic cannot bless stale evidence |
| API to worker | Reconstruct scope from the authorized claim/saved execution metadata and current nonsecret epoch | Each HTTP exchange has its own request ID; run/attempt keys join exchanges |
| Runtime launch / trusted child | Launcher supplies validated context and owns the child channel identity | Child creation and exit delimit scope; candidate fields cannot overwrite it |
| Recovery/adoption | Use saved execution identity plus the current authorized recovery context | Current aliases and old request-local variables cannot redefine original scope |
| Provider | Inject already bound `ProtocolContext.logger` | Provider does not reconfigure root output or fetch secrets for logging |
| Clients / browser | Preserve API transport and error IDs in safe diagnostic metadata | Do not modify the returned API JSON body or promote local correlation to authority |

Concurrent operations must not leak context, including standalone coroutines and sync/async framework paths. Bind/reset context at actual boundaries; using context variables alone does not prove propagation across threads, HTTP or processes. Time and identity generation remain injectable for deterministic tests.

Every API-handled HTTP exchange receives a fresh server-generated transport request ID returned as `X-Request-ID`, including error responses and stream setup. A newly generated structured error uses the same value in `error.request_id`. This is a response correlation contract, not a new operation or authorization mechanism. Errors occurring outside the API process before a request reaches it cannot carry its generated ID.

Idempotency replay preserves the saved body exactly, including any original error request ID. The current response header and transport diagnostic use the fresh ID; replay diagnostics link the original as `replayed_request_id` when present. A successful accepted-outcome replay likewise has a fresh transport ID and a content-free outcome reference, without repeating the mutation. Python and generated TypeScript clients retain the current header and distinct saved error ID for diagnostics. CLI JSON and MCP results preserve the API result body verbatim. Stream failures after headers retain the current exchange's context and existing streaming error semantics.

## 5. Safe content and trust boundaries

Use event-specific allowlisted fields. Never emit request/response bodies, prompts/feedback, transcripts, tool arguments/results, artifact payloads, environment/configuration dumps, SQL parameters, authorization/cookies, full URLs/query strings, arbitrary `repr` or exception locals. Content does not become safe because it lacks a registered secret. Route templates, safe codes, bounded identifiers and counts explain the outcome without duplicating payload retention.

Exception messages, chained exceptions, source text, framework access/error logs, third-party handlers, startup and unhandled errors use the same policy. Keep only safe type/frame metadata and reviewed static explanations; omit unsafe source/message text. DEBUG uses exactly the same protection. Unknown fields or formatting failures produce a fixed content-free rejection reason/counter, never a fallback representation of the input. An exception that reaches a process's composition root is recorded as one CRITICAL `process.failed` record through this policy (exception type, module and frames, no message) and the process exits with code 70; an interrupt exits with code 130 and no failure record; a thread that ends on an unhandled error is recorded as one ERROR `thread.failed` record through the same policy while the process continues; standard output failures never print a traceback: a write that fails inside a command with a broken pipe ends the process with code 1 and no failure record, a write that fails inside a command for any other reason is the ordinary unhandled error (one `process.failed` record, code 70), a failure of the final flush at exit is silenced and ends the process with code 1 unless a nonzero code is already set, and a standard output closed before the interpreter started is nothing to write to, so the exit code is the command's own; a `SystemExit` raised by a command keeps its code, reduced to the eight bits the operating system reports; an unusable standard error loses the usage or configuration line but never changes the exit code. A standard-library record admitted through the third-party allowlist becomes the event `thirdparty.record` with its logger name, its level and the safe exception chain; its message text and arguments are never formatted or kept ([issue #147](https://github.com/realkarych/kora/issues/147)).

Known resolved values are redacted before serialization/enqueue in JSON and text. Do not transmit a secret to another component just to improve its logs. Escape embedded CR/LF, control characters and terminal escapes; truncate approved values with an explicit marker while retaining valid records and required fields. If a valid safe bounded record cannot be formed, reject it with bounded loss accounting. Name-based masking alone is insufficient, and arbitrary transformed secret removal is not guaranteed.

Redaction ownership includes outstanding captured output. Bound retained secret count/bytes, registration/rotation work and per-record matching: matching scans the prefix of a string that can survive truncation plus the longest registered value, window by window until the masked output fills the retained size, so its work is bounded by the retained output and the registry, not by the input. Release a registration only after dependent output has been sanitized; never evict a live mask silently. If mandatory coverage cannot be established, fail secret admission before exposing the value, with a content-free error and independently visible degradation. This is the existing [SecretValue protection obligation](protocols.md#6-secrets-fr-101), distinct from an ordinary diagnostic-sink outage, which does not stop scheduling.

The runtime launcher separates candidate stdout/stderr and candidate-writable descriptors from trusted supervisor diagnostics. A protected runtime prevents candidate and descendant writes to the trusted sink; trust comes from runtime/channel ownership, never a JSON field naming `supervisor`. Real conformance must prove that boundary. Local retains [ADR-0016's trusted same-user limitation](../../docs/decisions/0016-trusted-execution-and-publication.md): tests prove intended plumbing, not hostile process containment. Installed Python providers are trusted cooperative code; logging does not sandbox a malicious plugin.

Operator collectors may aggregate namespaces. Deployment owners control access, forwarding and retention, document those choices and prevent user/session endpoints from exposing the operator stream. Metadata such as IDs still needs operator access control. Browser diagnostics exclude cookies/tokens and retained payloads. The existing transcript capability and payload expiry cannot be bypassed by copying that content into longer-lived diagnostics. These requirements add no managed log-retention service.

## 6. Bounded output and failure behavior

Check level eligibility before expensive record construction. Bound pre-encoding work as well as final byte size, including collections, nesting, frames, matcher state and stdlib conversion. Never run arbitrary object formatting while holding scheduler locks. No synchronous sink, file or network I/O occurs on the event loop or inside DB transactions/run locks.

Producers use nonblocking enqueue; a separate writer owns sink I/O. Reaching either queue limit drops the incoming diagnostic and increments a fixed-cardinality loss counter. Do not block core progress, recursively report loss to the broken sink, retry without a bound or write synchronously through an emergency fallback. In-memory loss/queue/sink state is available independently of the sink; processes with health/metrics surfaces expose it there. The first consumer must define how operators inspect degradation for its process shape, including CLI/MCP processes without a server. Inability to deliver diagnostics is not proof that durable work failed or succeeded.

New diagnostic queue/loss/sink metrics use fixed component/level/reason dimensions. Run/request/attempt/session IDs never become labels. Preserve existing bounded namespace/runtime/resource/scope dimensions required by the [scheduler contract](scheduler.md#11-metrics-and-health-nfr-73); this is not a global ban on its existing metrics. Bound logger caches and incident-key state too.

Each output stream has one writer. Kora-launched trusted children use separate diagnostic channels, with an owning supervisor/collector merging framed records through bounded buffers. Multiple process queues must not write independently to the same inherited stderr pipe. Deployment collection preserves per-process framing; a 16 KiB record cannot be assumed atomic under POSIX pipe-write limits. Candidate/session channels remain separate from trusted merging.

The sole writer completes partial writes. An unrecoverable mid-record error abandons that stream until a fresh framing boundary is established; readers discard an incomplete tail and account for loss rather than attaching the next record to it. A crash can lose the final partial record. There is no durable or exactly-once diagnostic delivery guarantee. A merger is subject to the same limits and cannot become an unbounded intermediate queue or a reason producers block.

Shutdown drains diagnostics for at most the configured deadline, including a permanently blocked writer and interpreter shutdown hooks. It never delays deadline escalation under [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md), interrupts required durable closure to satisfy a log deadline, or leaves a writer preventing process exit. Test actual process exit rather than assuming a queue helper's stop method is bounded. Account for process count, OS pipes, merger buffers, matcher state and in-flight serialization when measuring total deployment memory.

## 7. Component coverage and activation

All rows inherit sections 1–6: safe fields, single failure owner, stderr or browser-console output, bounded noise and the shared test obligations below. Libraries use their caller's configured logger. Event names below describe required lifecycle categories; the implementing issue fixes the reviewed catalog and typed fields.

[Issue #91](https://github.com/realkarych/kora/issues/91) owns this pending activation inventory until a named implementation issue assumes each row. The design issue closes for the delivered contract, not runtime completion. Before code starts, the component issue records its row, event catalog, exact commands and evidence; the same PR ships its logging. No row may disappear into an unowned future TODO. The first real backend consumer also owns the shared bootstrap, dependency adoption, bounded sink and common tests. Existing executable obligations remain active independently.

| Component / owner | Lifecycle and failure evidence | Scope and sensitive exclusions | Activation and named coverage |
|---|---|---|---|
| API and webhooks | Request completion, accepted mutation, rejection/replay, unexpected failure | Current request and authorized namespace/object; route template, no body/header values | First API operation: schema, context, request-ID, content and durable-event tests |
| Scheduler/admission/retries | Accepted/pending/promoted run, committed transition, retry scheduled, recovery/degradation | Run/node/attempt and safe reason; no poll-level INFO or stored configuration | First scheduler: context, volume, stalled-sink and durable-event tests, NFR-3 measurement |
| Sensors and observation dispatcher | Observation failure/recovery, expired/stale result, delivery uncertainty | Authorized invocation scope, safe provider/purpose; no responses or secret-bearing endpoints | First dispatcher/provider call: context, volume and stalled-sink tests |
| Worker | Ready/claim accepted, attempt submission/report failure, adoption and shutdown | Worker/incarnation, run/attempt, epoch/domain; no claim payload or credentials | First worker: context, framing, output-failure and API-ID tests |
| Runtime supervisor/common executor | Preparation, launch, seal, inspection, finalization, stop/recovery result | Launcher-bound attempt/domain/phase; no command argv, workspace content or credentials | First runtime/common executor: context, candidate isolation, framing and shutdown tests; real protected evidence where claimed |
| Session runner/harness bridge | Harness start/end, limit/usage-observation gap, adapter failure | Attempt/session and safe codes; never token/tool/transcript output as diagnostics | First runner/adapter: context, safe-content and channel tests; preserve incremental session relay |
| Metadata/migrations/retention | Migration start/result, cleanup batch result, failed persistence/recovery | Safe operation/revision/counts and authorized scope; no SQL parameters or payload copies | Active for migrations and transaction failures since [issue #153](https://github.com/realkarych/kora/issues/153): the `db` component emits `db.migration.started`, `db.migration.finished` (after the commit), `db.migration.failed`, `db.transaction.failed` and `db.close.timed_out` with the dialect, revisions, counts, durations and the safe exception chain; after `db.close.timed_out`, a counted unit that had begun and reaches its commit rolls back instead and is recorded as `db.transaction.failed` with the `DatabaseClosedError` chain; a body that fails on its own keeps its own chain, a cancelled unit records nothing, a commit already under way completes, and units still waiting for the writer or a pooled connection begin nothing and record nothing; a close that ends within its deadline, or that is cancelled before its deadline as at event-loop shutdown, records no `db.close.timed_out`, and a unit that reaches its commit after such a cancelled close is recorded as `db.transaction.failed` with no `db.close.timed_out` ([issue #158](https://github.com/realkarych/kora/issues/158)); `tests/db/test_db_upgrade_process.py` (the SQLite process; the PostgreSQL process is covered by the filtering test), `test_sql_parameters_and_statements_never_appear_in_diagnostics` (the `db` leg of `test_logging_redacts_content_and_safe_exception_chains`), `tests/observability/test_db_diagnostics_follow_the_commit.py` (`db.migration.finished` only after the commit, a refused commit recorded as a failure, body faults recorded once, cancellations never, a close past its deadline recorded once before the rollback of the unit it cut short, the queued unit recording nothing, a body's own failure and a cancelled unit keeping their outcome, an idle close with a zero deadline recording nothing, a close cancelled by event-loop shutdown recording nothing), the close tests of `tests/db/test_transactions.py` (`test_close_after_the_deadline_fails_every_unit_in_flight_and_writes_nothing`, `test_close_after_the_deadline_fails_a_read_transaction_waiting_for_the_pool`, `test_close_while_a_commit_is_under_way_lets_the_commit_finish`, `test_close_during_the_checkin_of_a_committed_unit_fails_the_unit_behind_it`, `test_close_is_shared_by_every_caller_and_survives_their_cancellation`, `test_a_unit_started_after_close_was_called_is_refused_while_closing`, `test_close_cancelled_by_loop_shutdown_still_releases_every_connection`) and `tests/db/test_log_filtering_does_not_change_durable_events.py` run on both metadata databases. The cleanup batch result stays with the retention task of the scheduler work item |
| CEL binding and evaluator (`cel`) | `cel.binding.loaded` at INFO when the binding loads, once per process; the evaluator's events land with [issue #166](https://github.com/realkarych/kora/issues/166) | The version only; never expression text, bound values, binding messages or exception chains ([IR section 7.7](ir.md#77-evaluation-errors)). The binding's own `cel` logger forwards expression text and values, so the loader disables it and attaches a filter that drops every record; a host's `logging.config.dictConfig(..., disable_existing_loggers=False)` re-enables the logger but not past the filter. The binding's Rust panic hook writes a few unframed lines to file descriptor 2, outside every sink and not as diagnostic records: the panic location in the crate's source, its message and a note on `RUST_BACKTRACE`, and a full backtrace when that variable is set; none carries expression text | Active for the loader since [issue #165](https://github.com/realkarych/kora/issues/165): `test_cel_binding_loaded_event_is_safe` and `test_cel_binding_logger_never_carries_content`. Refusal reporting belongs to the first entry point that loads the binding, and the rule that CLI and stdio MCP processes emit these events at DEBUG activates with the first such consumer |
| DSL/compiler/IR | Compile/validation result and unexpected internal failure | Author-side operation ID, safe code/location classification; no source/prompt dumps | First compiler/validator: schema/content and CLI stdout tests; caller owns rendering |
| Shared clients/CLI/MCP | Operation completion/failure, transport recovery | Current request and saved error ID; no HTTP bodies/tokens; stdout reserved | First client/public surface: request-ID and real CLI/MCP framing tests |
| Browser | Safe client failure/recovery diagnostic | Page instance and authorized API IDs; no cookies, bodies or transcript state | First diagnostic-producing feature: browser diagnostics/content checks and applicable performance; current shell checks remain active |
| Startup/doctor/deployment | Configuration validation, ready/stopped, unavailable dependency | Component/instance and safe failure code; never configuration/environment dumps | First entry point: schema/configuration/content tests and blocked-writer process exit. The `kora` entry point ([issue #147](https://github.com/realkarych/kora/issues/147)) delivers the configuration, content and blocked-writer exit tests for the CLI process; `kora standalone` keeps the row for its own entry point |
| Logging infrastructure | Rejection, loss, sink failure/recovery and bounded drain | Fixed component/level/reason, independent counters; no recursive failure output | Active since [issue #147](https://github.com/realkarych/kora/issues/147): `tests/observability/` carries the bootstrap legs of the four shared tests below plus `test_entry_point_exit_paths` and `test_logging_loss_is_visible_without_the_sink`; `tests/measurements/test_logging_overhead.py` carries the measured overhead and resource lifetime |
| Product harness instructions | Correlation and error/recovery guidance | User-session instructions, not a runtime logging process | First relevant instruction/example change: existing operation/example and generation-drift checks |

### All eleven provider protocols

Every provider inherits the injected `ProtocolContext.logger` and the common conformance rules. Its first implementation issue assumes the row below; shared conformance covers bound context and failure ownership using installed implementations, with real core integration and protocol fakes where appropriate.

| Protocol | Required diagnostic categories | Additional exclusions and evidence |
|---|---|---|
| Runtime | Submit/adopt/lookup/finalize/stop outcome and uncertainty | No capabilities that authorize execution; real candidate/sink separation and lifecycle evidence |
| Harness adapter | Launch/normalization/connection failure and session end | No raw SDK stream/tool arguments; recorded-stream normalization is not live compatibility evidence |
| VCS | Preparation/inspection/publication/reconciliation outcome | No credentialed URLs, repository contents or unrestricted subprocess output; safe phase/revision references |
| Approval channel | Delivery/reconciliation/closure outcome | No form/ReviewBundle payload or callback credentials; no claim of exactly-once remote delivery |
| Secrets | Authorized resolution/admission failure and redaction health | Never values, secret-derived hashes or environment dumps; rotation/lifetime/exhaustion tests |
| Artifact storage | Upload/download/retention outcome | No payloads, signed URLs or response bodies; safe sizes/digests where permitted |
| Artifact type | Validation/summary outcome | No invalid payload or content-bearing projection; safe schema/code/counts |
| Observability exporter | Export degradation/recovery and counted drops | No recursive export of its diagnostics; existing slow/down event-export conformance remains separate |
| Authentication | Authentication/authorization outcome | No bearer/cookie/login-code values; denied claimed scope does not become canonical context |
| Sensor | Poll degradation/recovery, timeout/stale observation | No full responses/URLs; unchanged successful polls remain DEBUG |
| Policy | Decision/outcome and unsupported interception | No tool arguments/secret paths; diagnostic output never replaces durable denial/audit evidence |

## 8. Future executable evidence

These are release obligations for the owning implementation issues, not existing tests or results. Consolidate overlapping scenarios into real integration/conformance suites; do not write unit tests mirroring glue. Use constructor/configuration-selected protocol fakes, real handlers/pipes/processes and local services. No mocks, monkeypatch, patched HTTP transport or synchronization sleeps. Inject clock/identity sources; wait on explicit conditions with deadlines. Process-start environments may be explicit without runtime environment patching. Metadata cases use one parametrized SQLite/PostgreSQL fixture; container cases run on Linux, supported local paths on Linux/macOS.

| Named obligation | Required assertion and activation owner |
|---|---|
| `test_logging_schema_levels_and_component_overrides` | First bootstrap: real JSON/text output, five levels, defaults/overrides, invalid startup settings, stdlib bridge and no duplicate handlers. Bootstrap leg active since [issue #147](https://github.com/realkarych/kora/issues/147), through the real `kora` process |
| `test_logging_context_isolated_and_propagated` | Each relevant boundary: concurrent run/request isolation, sync/async bridge, subprocess and recovery identity, cancellation/reset; no fabricated or leaked context. In-process leg (tasks, thread bridge, cancellation, context override) active since [issue #147](https://github.com/realkarych/kora/issues/147); subprocess and recovery legs stay with their components |
| `test_api_request_id_matches_response_error_and_logs` | First API/client: spoofed inbound IDs ignored; fresh error/header/log agreement; cached error replay through another API instance retains original body and links fresh transport ID; success replay does not repeat mutation; clients preserve both IDs |
| `test_cli_and_mcp_stdout_remains_machine_readable` | First CLI/MCP: real process stdout remains exact JSON results/MCP frames while diagnostics go to stderr |
| `test_logging_redacts_content_and_safe_exception_chains` | First bootstrap and every capture path: registered secrets plus distinct unregistered nonsecret markers in forbidden bodies, prompts, transcripts, tools/artifacts, SQL parameters, exception chains/locals/source and third-party output; real error/startup/framework paths; JSON/text at DEBUG/INFO omit all forbidden markers and retain expected safe fields. Bootstrap leg (fields, collections, exception chains, third-party records, warnings) active since [issue #147](https://github.com/realkarych/kora/issues/147), with the unhandled-error path of the entry point in `tests/observability/test_entry_point_exit_paths.py`; the body, prompt, transcript, artifact, SQL and framework legs stay with their components |
| `test_candidate_output_cannot_forge_component_diagnostics` | First runtime/harness: real candidate and descendant emit forged JSON/context and control sequences; no candidate record reaches trusted diagnostics, valid supervisor context remains, operational stderr is not session content; protected runtimes prove descriptor/control-channel isolation, local proves intended plumbing |
| `test_logging_redaction_state_is_bounded_across_rotation` | First secrets/redactor: concurrent scopes and rotation keep count/bytes/work bounded; outstanding output retains masks; exhausted admission fails before exposure without silent eviction or input disclosure; measure steady-state CPU/retained memory |
| `test_logging_multiprocess_framing_with_slow_reader` | First trusted child/merger: near-limit concurrent records parse with intact identities; partial writes, incomplete-tail loss, recovery, bounded buffers and stopped-reader process exit on Linux/macOS |
| `test_logging_sink_failure_does_not_stall_work` | First sink and each core integration: real blocked/broken pipe, continuing core progress, finite record/byte queues, independent loss state, no recursive fallback and actual bounded process exit. Sink leg active since [issue #147](https://github.com/realkarych/kora/issues/147), including single-stream framing above `PIPE_BUF` and a nonblocking inherited descriptor; the core-integration legs stay with their components |
| `test_logging_volume_tracks_transitions_not_poll_frequency` | First repeated loop: injected clock/barriers; more unchanged polls/heartbeats produce no INFO growth; bounded incident keys and first/recovery suppression counts |
| `test_log_filtering_does_not_change_durable_events` | First persistence/transition path: INFO/ERROR/CRITICAL and overflow preserve exact domain/audit sequence; rollback never emits a committed-state claim |
| Provider logging conformance | Each provider: bound logger, typed context, safe failure fields and no root reconfiguration, including a locally installed third-party fixture |
| Browser diagnostic scenarios | First diagnostic-producing feature: existing real browser smoke captures exact expected negative diagnostics, no unexpected messages or prohibited content, finite incident work and applicable loading/interaction measurements |

Performance evidence declares workload, datasets, samples, environment and regression criteria before execution, retaining all samples on an uncontended runner. Cover disabled levels, INFO/DEBUG, maximum and oversized input, redaction churn, queue saturation and stopped readers. Measure producer cost, total transient/retained memory, event-loop/core progress and actual shutdown. The scheduler retains the under-five-second MVP target and incremental 1,000-node behavior; session relay remains incremental. The existing ten-minute whole-CI budget is unchanged. A design review, bounded queue setting or clean log sample is not a measured pass.
