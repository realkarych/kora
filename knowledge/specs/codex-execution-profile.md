# First Codex execution profile

The project owner selected this profile on 2026-11-05 under [#769](https://github.com/realkarych/kora/issues/769#issuecomment-6666327281), following the [reviewed proposal](https://github.com/realkarych/kora/issues/769#issuecomment-6665911224). It specializes the existing [harness/runtime contracts](protocols.md) and [same-call validation facts](validation-facts.md#10-native-codex-declarations), governed by ADR-0005/0009/0016/0022/0026. This is an accepted design, not evidence of profile implementation, complete declarations or execution admission.

## Scope and fixed inputs

The first working authentication profile uses an explicit native Responses provider with `env_key = "CODEX_API_KEY"`. Trusted configured installation inputs select the provider, destination, model, execution platform and native controls. The admitted credential owner supplies the key; authored graph values and ambient host/account/repository configuration do not select credentials or endpoints. Missing or unsupported account authentication cannot fall back to another method. ChatGPT account/OAuth support is deferred under [#770](https://github.com/realkarych/kora/issues/770) until the API-key adapter is integrated and qualified through the common runner.

The selected execution arrangement uses the official pinned Codex 0.166.0 binary for two roles per attempt: one app-server and one exec-server. The selected-executor interface is experimental. Its acceptance is restricted to the exact supported binary/control profile; a changed pin or unverified branch requires new matching implementation and evidence, not compatibility inferred from a version-like field.

On 2026-11-09 the project owner selected the first Linux production direction under [#772](https://github.com/realkarych/kora/issues/772#issuecomment-6735570737). The runtime admits one root turn per attempt and owns an authenticated relay to the native executor. [#1220](https://github.com/realkarych/kora/issues/1220) specifies that boundary. The profile stays unavailable until its native controls, Policy enforcement and configured facts qualify.

Server composition under #549 retains selected-instance binding and endpoint/default resolution. The optional in-process launcher extension approved under [#826](https://github.com/realkarych/kora/issues/826#issuecomment-6681105608) is the launch interface for this profile. Configured selection and declaration reads follow the existing effect-free contract.

## Execution and path binding

Both native roles, command/helper/hook descendants and their per-attempt homes/configuration remain inside the candidate execution domain. The Python adapter and policy client remain on the supervisor side. Existing runtime launch authority creates and controls native processes; the adapter does not replace it with direct subprocess authority. The second native role receives no supervisor API credential or publication capability. The arrangement adds no egress permission or network-enforcement claim.

The built-in executor is disabled. Exactly the admitted native execution environment is selected before a turn. Managed one-shot execution and disabled login shells remain effective. The owner must reject missing/inconsistent selected Bash metadata, unknown environments and drift before issuing work; native fallback when metadata is missing is not accepted. A model-supplied shell path cannot select another executor or interpreter.

The runtime consumes the attempt's single root-turn admission before handing its `turn/start` request to the native writer. Loss of acknowledgement, reconnect, adoption and interruption cannot issue a second root turn. The private app-server control surface admits only the selected startup and lifecycle operations. Thread replacement, additional turns and alternate effect-bearing control requests must be unavailable or refused before effects. The first profile requires that native subagent execution is impossible under the protected effective controls.

Both native roles use the same runtime-bound candidate filesystem view and admitted checkout. The supervisor policy resolver uses the corresponding authorized view of that exact candidate path namespace and workspace root. Candidate/native cwd claims cannot replace this binding. The native executor protocol includes process, filesystem and outbound HTTP RPCs and streams. The relay admits only qualified routes within the selected profile. Native selected command hooks omit remote cwd. Path checks use that shared view. Existing runtime protections and gate-time checks govern later filesystem changes.

A live reconnect must establish the same admitted app-server peer, executor, endpoint, profile and workspace and settle prior work before releasing anything further. Otherwise the owner fails the attempt. A reused environment ID, cached shell metadata or native `providerId` does not establish continued identity. `providerId` is compatibility metadata, not executable-byte identity. The profile supplies no replacement-native-process or post-stop resurrection semantics.

## Native connection capability

The runtime owns the selected host-local loopback relay. It admits the exact live app-server peer established by the runtime's role binding. The runtime starts the native exec-server with private stdio and gives the relay exclusive use of that stream. The native executor exposes only that stream. The adapter has no executor writer. An endpoint, readiness line, role name or bearer alone cannot attest the live peer.

Each attempt has a fresh unpredictable registration bearer, delivered only through the admitted dynamic `environment/add` registration. It authenticates the app-server connection in conjunction with the runtime's peer binding. It confers no Policy authority, and raw credentials stay out of argv, logs, durable data and candidate-visible configuration. Setup and lifecycle operations use their own bounded authority. The relay keeps requests within the candidate runtime's existing network and credential controls.

The runtime fences registration, reconnect and new forwarding under the original deadline and stop authority. It closes the listener, every accepted connection and session and the private native stream, and confirms actual resource closure. Token deletion, native disconnect and a logical terminal are observations. They do not establish revocation or domain release.

The earlier direct executor WebSocket profile used native bearer authentication, with the bearer digest in native argv. Its [launch fixture](../research/codex-launch-construction-2026-11-05.md) and [bound-role fixture](../research/codex-native-launch-context-2026-11-08.md) retain their evidence for that topology. The pinned dynamic registration path stores the Authorization header in memory. Activation requires negative evidence across native files, capture, errors and diagnostics.

## Environment and secret owners

Provider credentials, runtime controls, adapter assignments, workload values and admitted workload secrets retain their existing distinct owners. No supervisor environment is copied. Reservations are not inferred from forwarded values. Actual construction definitions and forbidden-name/duplicate-owner refusal must agree with declarations and refuse before secret revelation or native launch, including forbidden controls the adapter leaves unset.

The provider key never enters declaration values, argv, diagnostics or durable deployment/metadata configuration. Any permitted native per-attempt sensitive material retains an explicit private secret/file owner, bounded retention and cleanup. This profile adds no provider-key secrecy guarantee from candidate tools beyond DD-30 and the cooperative-local residual. The separate provider-key and selected-executor probes do not establish their combined process/start environment behavior.

## Private launch construction

Under [#781](https://github.com/realkarych/kora/issues/781), the existing private Codex constructor builds the direct-fixture role inputs from already selected provider settings, admitted lexical placements, a shared checkout, the executable and Bash paths, and the executor capability digest. It emits native arguments, environment assignments and TOML without allocating a runtime or inspecting ambient state. The app-server recipe names the provider credential and disables the built-in executor. The exec-server recipe contains the capability digest, not a raw bearer or provider credential. The caller retains both secret values and their actual delivery. Under [#1228](https://github.com/realkarych/kora/issues/1228), the same private constructor explicitly selects the relay's strict executor-stdio recipe. Both recipes reuse the role-owned environments, path binding, reserved names and native configuration. The stdio recipe selects only private stdio and uses neither a bearer nor a digest. An unsupported choice or mismatched digest refuses before materialization. The runtime retains actual process launch and private-stream custody under #184.

The constructor refuses overlapping lexical placements and collisions on the exact names it assigns or reserves. These include the provider-key name, the unset app-server state-path control `CODEX_SQLITE_HOME`, and the executor-owned command correlation labels `CODEX_THREAD_ID` and `CODEX_TOOL_CALL_ID`. The native executor supplies the correlation values. The constructor reserves the unset Bash startup control `BASH_ENV` because [non-login Bash still sources it](../research/codex-bash-startup-environment-2026-11-08.md). It also reserves the unset Bash trace controls `SHELLOPTS` and `PS4`. The [selected Linux evidence](../research/codex-bash-trace-environment-2026-11-08.md) records inherited trace output and a separate effect from expanding the trace prefix. It also reserves the unset Linux dynamic-loader controls `LD_PRELOAD` and `LD_AUDIT`. The [selected Linux loader evidence](../research/codex-dynamic-loader-environment-2026-11-09.md) records a separate loader effect before the approved Bash text for both controls. It reserves the unset model TLS controls `CODEX_CA_CERTIFICATE`, `SSL_CERT_FILE`, and `SSL_CERT_DIR` based on their [observed effect on selected Linux HTTPS trust](../research/codex-model-tls-roots-2026-11-08.md). The constructor also reserves the four unset alternate registry controls `CODEX_EXEC_SERVER_NOISE_REGISTRY_URL`, `CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID`, `CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN`, and `CODEX_EXEC_SERVER_NOISE_CHATGPT_ACCOUNT_ID`. These are partial launch definitions. Complete reservations still require the selected declaration evidence. Physical path identity, binary and effective managed-control checks, secret admission, materialization, final environment assembly and process launch remain with the runtime owner. The [combined Linux fixture](../research/codex-launch-construction-2026-11-05.md) consumes these exact definitions and checks the selected command environment and effects with synthetic credentials. It does not activate the production launch or declaration contracts.

The private constructor also reserves the exact uppercase and lowercase
spellings of `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`. Both
authored name collections must omit these controls, and the two recipes leave
them unset. The [selected Linux proxy evidence](../research/codex-model-proxy-2026-11-08.md)
distinguishes delivery of the provider key to a cleartext proxy from
routing the TLS connection through a CONNECT tunnel. Runtime ownership of
admitted routing and egress follows the existing environment contract.

## Bound native launch

The selected Codex profile uses [bound native start](protocols.md#bound-native-start). Part D of #184 binds both production roles to the same attempt and candidate checkout, with separate private role placements. Both roles use the same pinned official executable. The binding fixes each complete argument vector and native configuration. The provider borrows the admitted app-server stdio. The runtime consumes the executor role under the same launch owner and retains its stream for the relay. Under [#1230](https://github.com/realkarych/kora/issues/1230), the local bound context takes an explicit provider-visible role set. The selected fixture exposes only `app-server`. An adapter request for `exec-server` refuses before claim, acquisition or credential disclosure. The local launch owner uses the same bound-role lifecycle to acquire and retain executor stdio. The historical direct fixture exposes both roles. The [selected stream evidence](../research/codex-private-executor-stream-2026-11-09.md) records native initialization and actual fixture closure. The production registration handoff belongs to its implementing plan.

| Slot | Bound native command | Harness-credential grant |
| --- | --- | --- |
| app-server | `app-server` with the selected stdio and strict-config arguments | Exactly the admitted `CODEX_API_KEY` |
| exec-server | `exec-server` with selected private stdio and strict-config arguments | Empty |

Before either start or any value disclosure, the runtime refuses missing required or unexpected harness credentials, duplicate owners within either role's environment, and forbidden names across the selected profile. It verifies the pinned executable, admitted Bash, bound checkout and placement view, and the configuration and environment for the actual launch. The verified configuration fixes the Responses destination and untrusted checkout. App-server's bound environment sets `CODEX_EXEC_SERVER_URL=none` to disable the built-in executor. App-server's stdio, the attested relay peer and the private executor stream stay under the runtime owner. The live checks follow the startup order below.

The harness grant supplies the key only to app-server's initial environment. Exec-server and its Bash child's initial environments must omit that key. This delivery guarantee retains DD-30 and the cooperative-local same-user residual. Independently authorized workload secrets keep their own grant. The raw relay registration bearer remains confined to the admitted registration channel.

Native conformance under part D and #199 exercises both starts with an official binary and a synthetic key on each platform before its profile activates. Linux is the first activation target. macOS has a separate native qualification, and repository verification covers both platforms. It verifies the app-server grant and key absence in exec-server and its Bash child, and observes refusal before disclosure and activation with a valid synthetic grant for legacy allocation, wrong-role or argument substitution, and mismatched executable, configuration or environment. It exercises missing and unexpected grants, forbidden and duplicate names, unknown, stale, reused, cross-attempt and concurrently requested slots, and expiry or stop during acquisition, gated launch and delivery. Partial start tests include one running role when the second fails and verify actual closure of roles, descendants, streams, endpoints and private trees. Diagnostics and durable data follow the existing negative secret-evidence obligations.

The SDK launch path remains available to SDK-selected profiles. Codex activation requires the real owner-bound launcher and host, selected-profile conformance, complete applicable declarations under #549, and the separate required Policy gate below.

## Selected startup and checkout trust

Under [#787](https://github.com/realkarych/kora/issues/787), the private app-server recipe marks the admitted checkout explicitly untrusted and disables project instructions, as required by ADR-0009. This precedes native startup. The pinned app-server can otherwise mark an explicitly requested writable checkout trusted during thread creation, before returning its acknowledgement. The runtime still owns protected materialization and the physical checkout binding.

The existing startup helper borrows the fresh attempt's exclusive app-server RPC and the owner's expected native metadata. It checks startup identity and refuses an available built-in executor before delivering the bearer. It registers the selected loopback executor, checks its reported Bash spelling, checkout and ready status, then prepares one ephemeral thread with the exact selected workspace and environment. Inconsistent, missing or premature replies refuse before it hands a thread id to the turn controller. The helper does not initiate a turn or retry. Native registration/info may connect or recover internally. Production uses the owner-bound relay for that registration and verifies protected effective controls through actual use, including overrides and reloads. Native metadata supplies no peer or path authority.

Under [#1042](https://github.com/realkarych/kora/issues/1042), the helper also lists the acknowledged thread's hooks before handing off the thread. The bound expectation describes the selected app-side system `PreToolUse` declaration. The startup check requires the exact workspace and one reported enabled, managed synchronous catch-all command with the expected source, timeout and normalized declaration fingerprint. Warnings, errors and mismatched replies refuse the handoff. The reply is native configuration evidence. The runtime owner still verifies the materialized configuration, executable and actual filesystem binding, and required Policy activation still needs before-effect enforcement. [Selected inventory evidence](../research/codex-managed-hook-inventory-2026-11-08.md)

The [selected startup probe](../research/codex-selected-startup-2026-11-06.md) exercises the real Linux checkout controls, expected reply fields and separate bearer/Bash refusal stages with synthetic credentials. Its direct process and container observations do not replace the runtime's independent endpoint identity, reconnect fencing or whole-domain closure. Complete facts, runtime activation and native macOS qualification remain pending.

## Required tool policy activation

Under [#810](https://github.com/realkarych/kora/issues/810), both native roles retain the same approved official Codex release. The [selected-profile fault probe](../research/codex-managed-hook-failures-2026-11-06.md) observed official 0.166.0 report a failed managed hook and still execute the Bash command after exit 1, exit 2 without the blocking reason, and timeout. An explicit managed denial blocked the identical command. These are Linux Bash observations.

The [selected shell-routed patch probe](../research/codex-shell-routed-patch-policy-2026-11-08.md) exercises the existing `allow_deny` guard against a Bash tool call that routes to a native file edit. The hook input and actual edit route are separate parts of the enforcement qualification.

Before activating this profile for required tool policy, prove that the authenticated managed `PreToolUse` decision controls every claimed covered call before it reaches the tool. A denial, absent or invalid decision, Policy failure or timeout cannot release that call. A late decision, replay, input rewrite without matching authorization, or mismatched role, attempt, selected workspace or call cannot authorize effects. Qualification binds the actual covered action to the selected official version, platform and controls, not to candidate-supplied metadata or a reported success. Preserve the denied-call behavior in [section 13](protocols.md#13-conformance-suites-nfr-102) and the existing `test_required_policy_is_enforced_or_rejected` and `test_policy_failure_denies` obligations.

The managed `PreToolUse` Policy decision supplies the authority, with the runtime relay enforcing it at the native write. The authenticated handoff binds the attempt, native role, thread, turn, call, physical workspace and exact effect. A pending, timed-out or invalidated decision supplies no release. The owner serializes one-use admission and the native write with the inherited cutoff and stop fence. Native hook completion, transport IDs, payload similarity and reported results cannot supply that authority.

An unattributed effect requires proof that only the authorized producer can issue it and earlier work and handles have settled. This holds across sampling steps, native retries and interruption within the same root turn. Unresolved ambiguity refuses before the native write. Qualification must cover alternate app RPC and native review, guardian, compaction and other producer routes, and establish their unreachability or effect refusal. Bind tool-attributable reads and metadata to their authorized operation separately from trusted startup and lifecycle requests.

Qualify Bash, direct patch and shell-routed patch through the actual supported route and covered effect. Bind the full admitted multistep patch effect sequence before the first mutation or refuse the shape. Observe zero covered effects on pre-admission refusals and zero new covered effects after the stop fence. This includes later mutations after an authorized patch prefix, even if the native request has already been forwarded. Refuse any effect shape that cannot meet the after-stop requirement. The runtime retains termination and cleanup authority for released work, the original D and D+30 thresholds, and the first cause. The existing runtime and Policy rules govern the scope of an authorized Bash process.

The [fixed-effect relay fixture](../research/codex-selected-executor-guard-2026-11-09.md) demonstrates two scripted effects on Linux and the fixture's own cleanup. Production peer custody, arbitrary or shell-routed patch qualification, and live reconnect require their own evidence.

Until qualification, complete `policy_coverage` remains unavailable for the affected selected profile under the [selected validation facts](validation-facts.md#10-native-codex-declarations). Its availability is separate from reservation facts and from the proposed exact-profile evidence of impossible native subagent execution.

## Private gated app-server stdio

Under [#1052](https://github.com/realkarych/kora/issues/1052), the runner's
private gated process path can attach one owned duplex socket to native
app-server stdin and stdout. The existing gate prepares the transport before
activation. After acknowledgement, the owner permits one synchronous borrow
before the inherited cutoff and stop fence. The owner aborts the transport on
stop and confirms descriptor closure before consuming the direct child's
exit. Preparation failure and cancellation retain the same owner through
cleanup. The default gate and installed lifetime wire schema retain their
existing stdin, stdout and capture behavior.

The [selected Linux fixture](../research/codex-gated-stdio-2026-11-08.md)
exercises this path with the pinned official binary and the existing selected
exec-server. The private stream prerequisite leaves bound slot admission,
production grant delivery, the harness host and Policy activation with
their existing owners.

## Local bound role environments

Under [#1058](https://github.com/realkarych/kora/issues/1058), the selected
Linux fixture gives the provider constructor the exact names from one
immutable authored snapshot. The local runtime binder uses that snapshot
and both returned recipes. The binder validates both roles before revealing
any secret. The app-server receives the sole provider key grant. The
exec-server and the Bash command run without that key.

The [native evidence](../research/codex-bound-environment-2026-11-08.md)
checks the complete initial environments of both pinned roles, their own
gate streams, managed hook and marker, and observed cleanup. The preflight
cases cover a late role collision, an attempted reassignment of the key
and an unset provider reservation. Production launch authority, physical
identity and the required before-effect Policy gate remain separate.

## Resource owner and stopping

One invocation's existing runtime/runner owner retains both native roles and every acquired stream, reader/writer/control task, listener, active connection/session, process/output/filesystem handle, native filesystem helper, HTTP request/body stream, descendant and per-attempt tree. Internal state requires explicit finite admission/retention and actual settlement; two top-level processes do not bound this state by themselves.

That owner handles partial acquisition, either start failure, registration/refusal, initialization deadline, transport loss, a blocked hook/read/native request, unexpected native exit, cancellation and shutdown. Late completion must join the same cleanup and cannot reopen admission. Unknown ownership remains retained and charged until the existing recovery authority establishes closure. Failure while cleaning one native role cannot abandon the other.

Under #961 the private selected composition checks the caller's stop event immediately before handing each setup frame or the `turn/start` request to the app-server writer. A latched stop at that boundary refuses the frame with `turn_stop_before_start`, without publishing shared events. An already written setup exchange remains bounded by the inherited deadline. Once a turn request is handed to the writer, the existing matching acknowledgement, interruption and terminal flow applies. The caller retains transport retirement, first cause and native closure.

The inherited deadline and independent interruption/escalation retain existing D and D+30 authority. A hung native call or teardown cannot move that threshold. Reconnect or adoption cannot reset the original deadline. Native turn/item completion, zero observed open tools, disconnect and reaping direct parents are not substitutes for actual domain closure.

Exact owned counts, identities, admission and actual closure remain blocking. Footprint/startup/whole-workflow timings remain diagnostic unless an existing approved method classifies a particular bound as blocking; existing safety deadlines and numerical gates are unchanged. This profile introduces no new benchmark campaign, numerical method or weakened acceptance check.

## Declaration and platform activation

The selected provider configuration must be validated, immutable and effect-free to acquire/read under validation-facts section 10: no execution host, running loop, version subprocess, process-environment inspection, files/sockets, secret resolution or runtime resources. It describes intended execution, while launch independently verifies actual binary identity and effective controls for both roles.

Complete reservations and policy coverage remain independently unavailable until their real production sources, shared launch definitions, exhaustive applicable ownership/classification and consumer integration qualify. No empty declaration, placeholder accessor, fake-only inventory or partial Linux observation supplies completeness. The exact internal receiver/signature belongs to the implementing plan.

Supported operating systems remain Linux and macOS. Complete native evidence must match the selected version, execution platform and full allowed control profile. Existing Linux amd64 fixture evidence does not establish native macOS behavior. First-slice refusal of authored hooks, skills, subagents, MCP servers and repository trust remains. Every effective nonempty namespace and node policy layer for the first profile, including trusted defaults, must be deny-only, with its complete requirements confined to Bash and file edits. [The current validation rules](validation-facts.md#9-complete-policy-coverage-requirements) continue to apply. #549 owns a separate amendment for discharging only the subagent context requirement from complete, effect-free evidence of its impossibility on the exact qualified configured profile. The future fact is separate from `policy_coverage` and requires protected controls and real native proof. Primary-kind requirements retain their existing derivation.

Native HTTP may resolve header values from executor-local environment names, and an absent native request timeout supplies no deadline. HTTP credential/redirect cases, requests, responses and retained body streams therefore retain phase-owner, finite admission and independent stop/settlement checks under the existing egress contract. No native HTTP timeout can replace inherited runtime authority.

Named implementation evidence includes `test_harness_launcher_preserves_control_boundary`, `test_codex_session_receives_only_constructed_environment`, `test_codex_binary_digest_mismatch_prevents_session_start`, `test_required_policy_is_enforced_or_rejected`, `test_codex_stream_volume_is_bounded` and `test_codex_session_releases_resources`. Implementing plans enumerate real success/refusal/failure/cancellation/shutdown cases, deterministic acquisition barriers, wrong/missing/stale/cross-attempt capability cases, saturated admission and exact independent closure. Existing supported platforms, metadata coverage, safety bounds and owner stop conditions remain.

[Runner host #199](https://github.com/realkarych/kora/issues/199) owns host/Policy/EventSink/enforcement/finalisation and runtime launch consumption; #549 owns trusted configured selection/projection; #593 owns saved execution contracts and canonical admission. This profile neither activates those consumers nor supplies complete HarnessAdapter/installed conformance.

## Threat and evidence limits

Existing TM-03 covers interception/alternate native tool paths; TM-05/TM-11 preserve candidate versus supervisor/control/publication authority; TM-06/TM-07 cover keys, bearer, environments and safe channels; TM-09/TM-10 cover both native roles, sessions/handles/helpers and actual release; TM-12 covers pin integrity and faithful qualification. The concrete native arrangement introduces no hostile-local isolation guarantee. Independent security design review established no mandatory threat-model text change for this bounded decision.

The [dated research record](../research/codex-execution-profile-2026-11-05.md) identifies original pinned-source/probe observations, accepted choices and pending production guarantees. Owner selection of this design, independent design review, later source review and actual runtime qualification remain distinct evidence states.
