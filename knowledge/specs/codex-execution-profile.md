# First Codex execution profile

The project owner selected this profile on 2026-11-05 under [#769](https://github.com/realkarych/kora/issues/769#issuecomment-6666327281), following the [reviewed proposal](https://github.com/realkarych/kora/issues/769#issuecomment-6665911224). It specializes the existing [harness/runtime contracts](protocols.md) and [same-call validation facts](validation-facts.md#10-native-codex-declarations), governed by ADR-0005/0009/0016/0022/0026. This is an accepted design, not evidence of profile implementation, complete declarations or execution admission.

## Scope and fixed inputs

The first working authentication profile uses an explicit native Responses provider with `env_key = "CODEX_API_KEY"`. Trusted configured installation inputs select the provider, destination, model, execution platform and native controls. The admitted credential owner supplies the key; authored graph values and ambient host/account/repository configuration do not select credentials or endpoints. Missing or unsupported account authentication cannot fall back to another method. ChatGPT account/OAuth support is deferred under [#770](https://github.com/realkarych/kora/issues/770) until the API-key adapter is integrated and qualified through the common runner.

The selected execution arrangement uses the official pinned Codex 0.166.0 binary for two roles per attempt: one app-server and one exec-server. The selected-executor interface is experimental. Its acceptance is restricted to the exact supported binary/control profile; a changed pin or unverified branch requires new matching implementation and evidence, not compatibility inferred from a version-like field.

Server composition under #549 retains selected-instance binding and endpoint/default resolution. This profile creates no server-wide default, provider factory, public executor selector, persisted profile format or new provider ABI member. Runtime construction, native declarations and host integration remain with their implementing issues.

## Execution and path binding

Both native roles, command/helper/hook descendants and their per-attempt homes/configuration remain inside the candidate execution domain. The Python adapter and policy client remain on the supervisor side. Existing runtime launch authority creates and controls native processes; the adapter does not replace it with direct subprocess authority. The second native role receives no supervisor API credential or publication capability. The arrangement adds no egress permission or network-enforcement claim.

The built-in executor is disabled. Exactly the admitted native execution environment is selected before a turn. Managed one-shot execution and disabled login shells remain effective. The owner must reject missing/inconsistent selected Bash metadata, unknown environments and drift before issuing work; native fallback when metadata is missing is not accepted. A model-supplied shell path cannot select another executor or interpreter.

Both native roles use the same runtime-bound candidate filesystem view and admitted checkout. The supervisor policy resolver uses the corresponding authorized view of that exact candidate path namespace and workspace root. Candidate/native cwd claims cannot replace this binding. The selected native endpoint exposes process, filesystem and outbound HTTP RPCs and streams, and native selected command hooks omit remote cwd. Path checks use that shared view. Existing runtime protections and gate-time checks govern later filesystem changes.

A live reconnect must remain bound to the same admitted native process, endpoint, profile and workspace or fail the attempt. A reused environment ID, cached shell metadata or native `providerId` does not establish continued identity. `providerId` is compatibility metadata, not executable-byte identity. The profile supplies no replacement-native-process or post-stop resurrection semantics.

## Native connection capability

The exec-server uses an owner-admitted host-local loopback listener with native capability-token authentication. Unauthenticated WebSocket execution access and nonlocal selection are forbidden; a fixed local readiness response is not execution authorization. The implementing launch owner establishes the exact listener/process/endpoint binding and authorization; the existing shared-UID fixture is not production authentication or containment evidence.

Each attempt has a fresh unpredictable executor bearer. The raw bearer is supplied only through admitted dynamic `environment/add` registration, with only its digest in native argv. The profile does not configure a token file or static `environments.toml` bearer. The capability authorizes the native RPC surface exposed by that endpoint, including process, filesystem and outbound HTTP requests and streams. Both roles and all native requests remain within the candidate runtime's existing network and credential controls. The bearer is not a Policy approval credential or authority over supervisor/control/publication resources.

Native authentication is connection-time, retains the token for reconnect and supplies no assumed TTL or automatic refresh. Owner-bounded lifetime means fencing admission, closing the listener and every accepted connection/session, ending reconnect rights and proving actual resource closure. Token deletion, a disconnected WebSocket, native `process/exited` or a logical terminal does not establish revocation or whole-domain release.

Pinned dynamic-registration source stores the sensitive Authorization header in memory for the environment/client and does not write `environments.toml` on that path. This is a source-path observation, not a complete non-disclosure guarantee. Native capture, files, logs, errors and diagnostic channels need actual negative evidence before activation.

## Environment and secret owners

Provider credentials, runtime controls, adapter assignments, workload values and admitted workload secrets retain their existing distinct owners. No supervisor environment is copied. Reservations are not inferred from forwarded values. Actual construction definitions and forbidden-name/duplicate-owner refusal must agree with declarations and refuse before secret revelation or native launch, including forbidden controls the adapter leaves unset.

The provider key never enters declaration values, argv, diagnostics or durable deployment/metadata configuration. Any permitted native per-attempt sensitive material retains an explicit private secret/file owner, bounded retention and cleanup. This profile adds no provider-key secrecy guarantee from candidate tools beyond DD-30 and the cooperative-local residual. The separate provider-key and selected-executor probes do not establish their combined process/start environment behavior.

## Private launch construction

Under [#781](https://github.com/realkarych/kora/issues/781), the private Codex constructor builds both role inputs from already selected provider settings, admitted lexical placements, a shared checkout, the executable and Bash paths, and the executor capability digest. It emits native arguments, environment assignments and TOML without allocating a runtime or inspecting ambient state. The app-server recipe names the provider credential and disables the built-in executor. The exec-server recipe contains the capability digest, not a raw bearer or provider credential. The caller retains both secret values and their actual delivery.

The constructor refuses overlapping lexical placements and collisions on the exact names it assigns or reserves for the provider-key owner. These are partial launch definitions, not complete reservations. Physical path identity, binary and effective managed-control checks, secret admission, materialization, final environment assembly and process launch remain with the runtime owner. The [combined Linux fixture](../research/codex-launch-construction-2026-11-05.md) consumes these exact definitions and checks the selected command environment and effects with synthetic credentials. It does not activate the production launch or declaration contracts.

## Selected startup and checkout trust

Under [#787](https://github.com/realkarych/kora/issues/787), the private app-server recipe marks the admitted checkout explicitly untrusted and disables project instructions, as required by ADR-0009. This precedes native startup. The pinned app-server can otherwise mark an explicitly requested writable checkout trusted during thread creation, before returning its acknowledgement. The runtime still owns protected materialization and the physical checkout binding.

The startup helper borrows the fresh attempt's exclusive app-server RPC and the owner's expected native metadata. It checks startup identity and refuses an available built-in executor before delivering the bearer. It registers the selected loopback executor, checks its reported Bash spelling, checkout and ready status, then prepares one ephemeral thread with the exact selected workspace and environment. Inconsistent, missing or premature replies refuse before it hands a thread id to the turn controller. The helper does not initiate a turn or retry. Native registration/info may connect or recover internally, and the metadata is not authority over the peer or path.

The [selected startup probe](../research/codex-selected-startup-2026-11-06.md) exercises the real Linux checkout controls, expected reply fields and separate bearer/Bash refusal stages with synthetic credentials. Its direct process and container observations do not replace the runtime's independent endpoint identity, reconnect fencing or whole-domain closure. Complete facts, runtime activation and native macOS qualification remain pending.

## Required tool policy activation

Under [#810](https://github.com/realkarych/kora/issues/810), both native roles retain the same approved official Codex release. The [selected-profile fault probe](../research/codex-managed-hook-failures-2026-11-06.md) observed official 0.166.0 report a failed managed hook and still execute the Bash command after exit 1, exit 2 without the blocking reason, and timeout. An explicit managed denial blocked the identical command. These are Linux Bash observations.

Before activating this profile for required tool policy, prove that the authenticated managed `PreToolUse` decision controls every claimed covered call before it reaches the tool. A denial, absent or invalid decision, Policy failure or timeout cannot release that call. A late decision, replay, input rewrite without matching authorization, or mismatched role, attempt, selected workspace or call cannot authorize effects. Qualification binds the actual covered action to the selected official version, platform and controls, not to candidate-supplied metadata or a reported success. Preserve the denied-call behavior in [section 13](protocols.md#13-conformance-suites-nfr-102) and the existing `test_required_policy_is_enforced_or_rejected` and `test_policy_failure_denies` obligations.

An official native blocking facility or a separately qualified runtime guard may supply the required control. Any guard supplements the same managed hook decision and the runtime's existing authority. Neither path is qualified by this design. `hook/completed`, an executor identifier, a native tool result and a later interrupt do not supply blocking authorization. The [source and evidence limits](../research/codex-official-policy-gate-2026-11-06.md) distinguish the paths and remaining checks.

Until qualification, complete `policy_coverage` remains unavailable for the affected selected profile under the [selected validation facts](validation-facts.md#10-native-codex-declarations). A missing guarantee is not a known-empty declaration. Existing validation and refusal semantics, independent reservation facts, and the runtime owner's deadline, first cause and actual closure remain unchanged.

## Resource owner and stopping

One invocation's existing runtime/runner owner retains both native roles and every acquired stream, reader/writer/control task, listener, active connection/session, process/output/filesystem handle, native filesystem helper, HTTP request/body stream, descendant and per-attempt tree. Internal state requires explicit finite admission/retention and actual settlement; two top-level processes do not bound this state by themselves.

That owner handles partial acquisition, either start failure, registration/refusal, initialization deadline, transport loss, a blocked hook/read/native request, unexpected native exit, cancellation and shutdown. Late completion must join the same cleanup and cannot reopen admission. Unknown ownership remains retained and charged until the existing recovery authority establishes closure. Failure while cleaning one native role cannot abandon the other.

The inherited deadline and independent interruption/escalation retain existing D and D+30 authority. A hung native call or teardown cannot move that threshold. Reconnect or adoption cannot reset the original deadline. Native turn/item completion, zero observed open tools, disconnect and reaping direct parents are not substitutes for actual domain closure.

Exact owned counts, identities, admission and actual closure remain blocking. Footprint/startup/whole-workflow timings remain diagnostic unless an existing approved method classifies a particular bound as blocking; existing safety deadlines and numerical gates are unchanged. This profile introduces no new benchmark campaign, numerical method or weakened acceptance check.

## Declaration and platform activation

The selected provider configuration must be validated, immutable and effect-free to acquire/read under validation-facts section 10: no execution host, running loop, version subprocess, process-environment inspection, files/sockets, secret resolution or runtime resources. It describes intended execution, while launch independently verifies actual binary identity and effective controls for both roles.

Complete reservations and policy coverage remain independently unavailable until their real production sources, shared launch definitions, exhaustive applicable ownership/classification and consumer integration qualify. No empty declaration, placeholder accessor, fake-only inventory or partial Linux observation supplies completeness. The exact internal receiver/signature belongs to the implementing plan.

Supported operating systems remain Linux and macOS. Complete native evidence must match the selected version, execution platform and full allowed control profile. Existing Linux amd64 fixture evidence does not establish native macOS behavior. First-slice refusal of authored hooks, skills, subagents, MCP servers and repository trust remains. Disabled native tools do not grant coverage, and conservative whole-policy/subagent requirements remain unchanged.

Native HTTP may resolve header values from executor-local environment names, and an absent native request timeout supplies no deadline. HTTP credential/redirect cases, requests, responses and retained body streams therefore retain phase-owner, finite admission and independent stop/settlement checks under the existing egress contract. No native HTTP timeout can replace inherited runtime authority.

Named implementation evidence includes `test_harness_launcher_preserves_control_boundary`, `test_codex_session_receives_only_constructed_environment`, `test_codex_binary_digest_mismatch_prevents_session_start`, `test_required_policy_is_enforced_or_rejected`, `test_codex_stream_volume_is_bounded` and `test_codex_session_releases_resources`. Implementing plans enumerate real success/refusal/failure/cancellation/shutdown cases, deterministic acquisition barriers, wrong/missing/stale/cross-attempt capability cases, saturated admission and exact independent closure. Existing supported platforms, metadata coverage, safety bounds and owner stop conditions remain.

[Runner host #199](https://github.com/realkarych/kora/issues/199) owns host/Policy/EventSink/enforcement/finalisation and runtime launch consumption; #549 owns trusted configured selection/projection; #593 owns saved execution contracts and canonical admission. This profile neither activates those consumers nor supplies complete HarnessAdapter/installed conformance.

## Threat and evidence limits

Existing TM-03 covers interception/alternate native tool paths; TM-05/TM-11 preserve candidate versus supervisor/control/publication authority; TM-06/TM-07 cover keys, bearer, environments and safe channels; TM-09/TM-10 cover both native roles, sessions/handles/helpers and actual release; TM-12 covers pin integrity and faithful qualification. The concrete native arrangement introduces no hostile-local isolation guarantee. Independent security design review established no mandatory threat-model text change for this bounded decision.

The [dated research record](../research/codex-execution-profile-2026-11-05.md) identifies original pinned-source/probe observations, accepted choices and pending production guarantees. Owner selection of this design, independent design review, later source review and actual runtime qualification remain distinct evidence states.
