# Codex first execution profile decision and evidence

Checked on 2026-11-05 against kora
`fc4f93ddab0702f6662f898bde1efadb3ba1540d` for
[#769](https://github.com/realkarych/kora/issues/769). The project owner selected
API-key authentication first and then the app-server plus dedicated native
exec-server arrangement. The [accepted decision](https://github.com/realkarych/kora/issues/769#issuecomment-6666327281)
binds the [exact reviewed proposal](https://github.com/realkarych/kora/issues/769#issuecomment-6665911224),
SHA-256 `30b4904fc87e6fcc6bab1ed0eea69f4aad084438426fe5428cb18e4f45895956`.
The lasting contract is [the first execution profile](../specs/codex-execution-profile.md).
This change contains three knowledge files and zero production lines; no new
native experiment, declaration or operational activation is supplied.

## Scope selected

The first authentication branch uses the explicit native Responses-provider
`env_key = "CODEX_API_KEY"` path. Its trusted configured inputs do not come
from operator/account/repository state. The credential value keeps its admitted
insertion owner. ChatGPT account/OAuth support remains accepted deferred work
under [#770](https://github.com/realkarych/kora/issues/770), with a concrete
revisit trigger after the API-key adapter is integrated and qualified through
the common runner. This decision defines neither the future account-storage
format nor an account fallback.

Both native roles remain candidate-side under one existing runtime/runner
lifecycle authority. Native local-executor fallback is disabled; the selected
Bash metadata and managed one-shot/login controls must qualify before turns.
The full exposed endpoint, including process/filesystem and outbound HTTP,
exact workspace/path binding, live
reconnect identity and owner-bounded stopping are explicit. The Python adapter,
Policy, independent enforcement and publication keep their existing owners.
No public executor selector, provider ABI, persisted profile, new dependency,
server default, platform or acceptance reduction is introduced.

The unselected alternative retained one native process and deferred production
profile/declarations until a proven equivalent built-in shell restriction.
It did not establish that a current flag supplies that restriction. A fork,
changed native pin or broader policy interpretation was not approved.

## Exact native pin and observed evidence

Codex CLI **0.166.0**, upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, is the selected native baseline.
The Linux amd64 musl archive SHA-256 is
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The selected-executor interface is experimental and gains no compatibility
promise for a changed pin. Native macOS artifacts/behavior and production
launch integrity remain implementation qualification, not supplied here.

| Input | Actual basis | Limit |
|---|---|---|
| API-key provider | [Credential probe #681](codex-provider-env-key-probe-2026-11-04.md): explicit loopback env_key accepted the synthetic key and refused its absence before connection; the built-in account did not implicitly load it. | Synthetic Linux handoff, not real-provider authentication or account support. |
| Built-in shell | [Shell-controls probe #688](codex-managed-shell-controls-probe-2026-11-04.md): the model selected distinct POSIX sh while the managed Bash hook saw identical command-only input. | Fixed cooperative commands, one native pin and Linux fixture. |
| Selected execution | [Executor probe #693](codex-selected-executor-shell-probe-2026-11-04.md): selected Bash stayed effective for another same-type path, a different type was refused, managed login/hook denial held, and both direct children were reaped on stall. | Shared UID, synthetic bearer, no provider key in this executor case, Docker final cleanup; independent descendant closure, production authorization and native macOS remain unqualified. |
| Hook decoding and lifecycle | [Hook input](codex-pre-tool-use-2026-11-05.md) and [tool observations](codex-app-server-tools-2026-11-05.md) correlate the fixed native calls and observed item prefixes. | Those native cases do not establish complete selected-executor classification, end-to-end Policy delivery or physical stopped receipts. |

Original probe revisions, fixture trees, environments, commands and results stay
in the linked records. Their results are not relabeled as new profile runs.
The #681 credential and #693 executor probes do not establish their combined
process/start environment forwarding. DD-30 and the cooperative-local residual
are unchanged; the design grants no new provider-key secrecy guarantee from
candidate tools.

## Pinned primary-source observations

The [selected-executor preparation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/preparation.rs#L206-L234)
removes the model-supplied path on the selected branch and compares shell type.
Without reported metadata it can fall back to the session shell. Rejecting
missing/inconsistent metadata before a turn is therefore required kora behavior,
not an already observed native fail-closed guarantee.

The [native hook preparation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L331-L346)
omits remote cwd. Selected environments also own filesystem operations.
Candidate placement alone does not prove path agreement: both native roles and
the supervisor's authorized policy view must refer to the same runtime-admitted
candidate workspace/path namespace.

The [exec-server README](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/README.md)
at this pin has SHA-256
`d41090e5f935049a7532fba18e8c24f5557b39c11cc8916c534dd8a76397db96`.
It defines process and filesystem RPCs, connection-time capability auth,
reconnect token reuse, managed output and filesystem handles/helpers. Two
processes alone do not bound native retained state. Native compatibility
providerId is not executable-byte identity.

The [native client](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/client.rs#L596-L626)
and [transport recovery](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/client_transport.rs#L237-L272)
can reconnect and cache metadata. Continued use must retain the exact admitted
native process/endpoint/profile/workspace or fail the attempt. Cached metadata,
an environment ID, token deletion or a disconnect supplies no replacement,
revocation or closure authority. The owner fences admission and closes the
listener, all accepted sessions/connections and reconnect rights on stop.
No native TTL or automatic refresh is assumed.

The [dynamic registration processor](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/environment_processor.rs#L15-L47)
passes the supplied bearer as Authorization to the
[in-memory environment manager](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L483-L554).
The [connection options](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment/connect_options.rs#L67-L103)
mark the header sensitive. This path does not write environments.toml; the
separate startup reader accepts a static bearer, which this profile excludes.
Registration-path inspection is not a proof that every native file, capture,
log or error excludes secrets. Actual negative non-disclosure evidence remains
required before activation.

## Resource, threat and activation boundaries

The pinned [WebSocket processor registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/server/registry.rs#L77-L81)
also exposes `http/request` through the initialized authenticated connection.
The [native HTTP handler](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/server/handler.rs#L277-L327)
owns outbound requests and streamed bodies. The
[wire schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server-protocol/src/protocol.rs#L1140-L1197)
permits executor-environment header lookup and requests without a native timeout.
The capability scope is therefore not restricted to command/filesystem methods.
Native HTTP retains existing candidate network/credential controls and adds no
egress permission or enforcement guarantee. Header/redirect and body-stream
ownership, admission and independent stopping need implementation evidence.
The fixed local `/readyz` response is availability data, not authorized RPC
access; authentication claims concern the WebSocket execution connection.

The existing runtime owner covers partial startup, registration/initialization,
transport loss, a blocked hook/read/request, cancellation, unexpected native
exit, reconnect and shutdown. Both direct roles, all handles/helpers/descendants, HTTP requests/body streams,
streams/tasks/connections and per-attempt trees retain finite admission and
actual closure obligations. Failure cleaning one role cannot abandon the other.
Independent inherited D/D+30 thresholds and retained unknown ownership are
unchanged. Exact owned counts/identity/admission/closure are blocking;
footprint/startup timing is diagnostic unless an existing approved method
classifies its bound otherwise. No new numerical method/campaign or gate change.

Existing TM-03 maps managed controls and complete interception; TM-05/TM-11
retain candidate versus supervisor/control/publication separation; TM-06/TM-07
cover keys, bearer, environments, native HTTP and channels; TM-09/TM-10 cover owned native
roles/connections/handles/helpers; TM-12 covers pin integrity and truthful
qualification. Independent security review established no mandatory threat-model
text edit for this design scope; the cooperative-local residual remains.

[Validation facts section 10](../specs/validation-facts.md#10-native-codex-declarations)
continues to require real production definitions/builders, complete independent
reservations/coverage, configured binding and native evidence matching the
selected platform/profile. All complete Codex facts remain unavailable now.
No placeholder accessor, registered adapter or consumer activation lands here.
[Runner host #199](https://github.com/realkarych/kora/issues/199) retains host/Policy/EventSink/finalisation, #549 configured selection and
projection, #593 saved contracts and admission. Native Linux/macOS and actual
resource/authorization evidence remain pending; actual complete HarnessAdapter
and installed conformance are not established by this decision.

## Review and checks

Three fresh independent read-only profiles reviewed the exact design proposal:
correctness, security and performance passed for owner selection. Each
reassessed the final clarified text. No reviewer executed candidate code,
installed, edited, published or delegated. Actual-diff review and final-head CI
remain separate pending obligations, recorded with provenance in #769 and its
implementing PR. This record asserts no newly executed runtime measurement.

Latest applicable master-audit availability was checked: successful
41297342266 at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` is partial pricing/CEL
with 113 executed entries; later 42013930066 at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before mutation execution.
There is no applicable Codex-profile finding, added mutation campaign or
missing-report hold.
