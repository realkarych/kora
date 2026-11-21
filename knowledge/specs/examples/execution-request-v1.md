# Execution request version-one examples

These proposed codec vectors accompany the [canonical execution request contract](../execution-request.md). Identifiers, model prices and selections are synthetic. They support byte checks and provide no trusted policy, installed provider facts, durable owner or execution authority. Future integration checks acquire authentic saved facts and use the actual producers.

## Complete bodies

Each canonical body starts at the opening brace and ends at the closing brace. The LF before a closing fence is Markdown framing. Hash the UTF-8 bytes with no BOM or final LF.

| Value | Byte count | SHA-256 |
| --- | ---: | --- |
| Agent canonical | 2212 | `sha256:2bc8058738971ff562eb5d84dd39bf24708321bd911ff12875300a035633aee2` |
| Agent config canonical | 254 | `sha256:37745668a9b66c94443353ec51e95d022318f04777331d1accc440d92e4bd96f` |
| Agent escaped source with LF | 2233 | `sha256:2d16e8240394c3fd36efbd5e30734b4d08e0e186a9b72d8a6cff0265df8019e9` |
| Command canonical | 1612 | `sha256:78fd5481f36074da5c472eb1de3d8e26ec145897ac1d75705d328bea48a69fbe` |
| Command config canonical | 80 | `sha256:e612f39374e6350d65a8d09951b4efcf8e1ab43e25bc75bfbfa99202c3cfe315` |
| Agent deadline one microsecond later | 2212 | `sha256:88be80a3326453c133134ac40cc3315f2b6a76425fc5d818ff53166e2f867611` |
| Agent deadline with smaller uncertainty | 2212 | `sha256:9e5d8ed9c815e9ba6ff69e14d5720013f40d89b60850f3c973e4dc19025007f3` |
| Agent deadline replaced by unlimited | 2066 | `sha256:96f5ebb86809ca5a0b151cbfd68d54be6b027571379295f0c4505fd67cd285df` |
| Agent with zero stall duration | 2204 | `sha256:6d5ab1a0d4535b4bdba3536f3f61e138cf29fee9beed858a20c3fe44d7208d82` |
| Agent with stall duration above signed-64 | 2222 | `sha256:43d960dd5c9d3bffa64f328dd29110564b93a4d8ae05776f079061107b2883b0` |
| Agent with changed workspace revision | 2213 | `sha256:814766860cef4b7c0aa8c7fbc334149ac46e60fd7b6c8851d6cce1a4fc8effc0` |

### Agent

The first executing claim started the attempt at `2026-01-01T00:00:00.123456Z`. The saved node timeout is `90s`, the determining scope is this physical attempt, and no parent supplies an earlier cutoff. The recorded uncertainty is exactly `250001` microseconds and the recorded grace is 30 seconds. The saved per-run parameters are `label = β` and `checkout_ref = refs/heads/main`. The configured runtime default supplies the exact `"900000000"` stall value. The selected workspace source is the authored `rev` template. Its independently rendered value is `refs/heads/main`. The selected template invokes no provider or external source. Its checked output is the request's rendered prompt. An omitted `config.system_prompt_append` becomes explicit null in the different, required `rendered.system_prompt_append` slot. Both `1` and `1.0`, and both integer `0` and real `-0.0`, are present as distinct spellings in the by-value output schema.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","config":{"harness":"claude","output_schema":{"examples":[1,1.0,0,-0.0],"type":"number"},"prompt":"Inspect “café” with {{ params.label }}.\nRecord \"checked\" and backslash \\.","workspace":{"repo":"/workspace/project.git","rev":"{{ params.checkout_ref }}"}},"deadline":{"value":{"at":"2026-01-01T00:01:30.123456Z","clock_uncertainty_us":"250001","grace_seconds":30,"scope_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","scope_kind":"attempt"},"version":1},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"agent","limits":{"max_events":128,"stall_after_us":"900000000","timeout":"90s"},"node_path":"agent","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":{"implementation_version":"1.0","name":"claude","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAY","selection_revision":1},"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"prompt":"Inspect “café” with β.\nRecord \"checked\" and backslash \\.","system_prompt_append":null,"workspace_revision":{"source":"rev","value":"refs/heads/main"}},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"backoff_coefficient":1.0,"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

### Command

This is the fixed command shape with an empty declared input inventory and no materialized inputs or resources. Its executing claim recorded an explicitly unlimited deadline as the versioned envelope with a null value. The harness, retry source and publication slots are explicitly null. Its omitted `workspace.rev` and `from_ref` yield an explicit null `rendered.workspace_revision`. Actual checkout evidence is recorded by later preparation.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FB2","config":{"cmd":["check","--mode","fixed"],"workspace":{"repo":"/workspace/project.git"}},"deadline":{"value":null,"version":1},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"command","limits":{"stall_after_us":"900000000"},"node_path":"gate","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":null,"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"cmd":["check","--mode","fixed"],"workspace_revision":null},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

## Equivalent logical sources

The escaped agent source uses equivalent spellings for the accents, Greek letter and LF escapes in the prompt. In this one raw source vector, append **one literal LF** after the closing brace. Its raw source digest differs from the canonical request digest. Prepare the same canonical agent bytes under the original stage.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","config":{"harness":"claude","output_schema":{"examples":[1,1.0,0,-0.0],"type":"number"},"prompt":"Inspect “caf\u00e9” with {{ params.label }}.\u000aRecord \"checked\" and backslash \\.","workspace":{"repo":"/workspace/project.git","rev":"{{ params.checkout_ref }}"}},"deadline":{"value":{"at":"2026-01-01T00:01:30.123456Z","clock_uncertainty_us":"250001","grace_seconds":30,"scope_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","scope_kind":"attempt"},"version":1},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"agent","limits":{"max_events":128,"stall_after_us":"900000000","timeout":"90s"},"node_path":"agent","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":{"implementation_version":"1.0","name":"claude","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAY","selection_revision":1},"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"prompt":"Inspect “caf\u00e9” with \u03b2.\u000aRecord \"checked\" and backslash \\.","system_prompt_append":null,"workspace_revision":{"source":"rev","value":"refs/heads/main"}},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"backoff_coefficient":1.0,"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

With the same trusted attempt, policy and saved facts, these transports reach the same canonical and config entries in the table.

| Form | Request and expected owner transition |
| --- | --- |
| Inline | Use the agent body as the logical object in `seal_request.body_or_upload_ref` and policy revision `7`. The API makes its canonical bytes durable through the original storage lifecycle before the conditional owner write. |
| Upload then seal | Send the escaped agent source as the payload, with its raw byte count and digest. Request version and selected attempt stay fixed. The result of upload is the same original reference in ready state, with the canonical body retained under that stage. Seal that exact reference at policy revision `7`. |
| Matching authorized seal replay | In separate clean cases for each form, compare the request and config digests with the same canonical entries. After the first commit and a policy revision change, return the same original saved descriptor and policy. The upload replay uses the original completion binding if staging cleanup already finished. |

Compare the logical identities across transports. Compare the exact descriptor on each transport's original accepted replay.

## Stall and workspace variations

The stall rows replace only `limits.stall_after_us` in the agent example to isolate the codec comparison. Authored `0ms` corresponds to `"0"`. Authored `9223372036854776ms` corresponds to `"9223372036854776000"`, above the signed-64 integer range. A real verification case supplies the corresponding pinned graph and exact Claim. Reject a changed value for the original Claim while retaining the exact admitted logical value for an applicable Claim. Test selected-runtime range handling separately.

The changed-workspace row replaces only `rendered.workspace_revision.value` with `refs/heads/other`. The config digest remains the same. With the original relevant saved facts and template, the expected revision is still `refs/heads/main`. Refuse the changed body before `ready` or first seal. After a successful seal of the original body, recover `{"source":"rev","value":"refs/heads/main"}` from its retained bytes. An exact authorized replay uses the original descriptor and performs neither Jinja rendering nor a mutable branch read. A replay of the changed revision conflicts.

## Refused bodies

These mutations apply to the complete agent body above, before canonicalization. A decoder must preserve the original member pairs until it rejects duplicates. A generic object parser that silently overwrites earlier members cannot validate the first case.

| Source change | Proposed result |
| --- | --- |
| Replace the final `"version":1` with `"ver\u0073ion":1,"version":1` | Refuse duplicate decoded name even though the values agree. |
| Replace the final `"version":1` with `"version":2` or `"version":1.0` | Refuse unsupported version or integer-kind violation. |
| Add `"worker_approved":true` to the root | Refuse unknown member. |
| Remove `"feedback":null` | Refuse absent required slot. |
| Insert `"system_prompt_append":null` under `config` | Refuse explicit null for the optional, nonnullable source field. |
| Replace the integer `1` in `config.output_schema.examples` with `1.0` | Valid encoding with different canonical bytes. Refuse a mismatch to the exact pinned config. |
| Replace `-0.0` in the same array with `0.0` | Valid encoding with different canonical bytes. Refuse a mismatch to the exact pinned config. |
| Change `"input":"1.25"` in the price table to `"input":1.25` | Refuse a numeric money value. |
| Insert an unpaired escaped surrogate such as `\ud800` into the prompt, or replace a real with `1e999` | Refuse invalid Unicode scalar or nonfinite value. |
| Change a rendered value while preserving all saved template sources and the original grant | Refuse a detached derivation mismatch. A Worker statement cannot make the changed content valid. |
| Remove `deadline`, set it to bare `null` or replace it with the UTC scalar | Refuse missing complete original deadline evidence. The command's stored `{version: 1, value: null}` is a different, explicit state. |
| Replace `deadline.value.at` with `2026-01-01T00:01:30.123457Z` | Valid complete deadline shape with the changed-cutoff digest in the table. Refuse a mismatch to the original deadline before first ready or seal. |
| Replace `clock_uncertainty_us` with `"250000"`, or change the determining kind or its owner | Refuse changed timing or scope evidence. The uncertainty-only change has the different digest in the table. |
| Replace the whole finite `deadline` with `{"value":null,"version":1}` | Valid recorded unlimited shape with a different digest. Refuse the substitution when the saved original is finite. |
| Give `clock_uncertainty_us` as integer `250001`, change grace to `31` or use a noncanonical UTC spelling | Refuse the malformed original-deadline envelope. |
| For a command, supply a declared optional input with a saved `absent` selection, or add a materialized input member to the body | Refuse under the proposed v1 command coverage. An empty submitted inventory cannot hide an actual saved declaration. |
| Replace `"900000000"` with a JSON number, `"0900000000"` or a signed string | Refuse a malformed stall value. A canonical zero or wider string compares by its exact value and must agree with Claim. |
| Remove `rendered.workspace_revision` or copy the literal Jinja template into its `value` | Refuse missing or incorrect selection evidence. When no revision is selected, the command vector has an explicit null. |
| For a supported `from_ref` selection with a present verified `VcsRef`, submit a fallback revision. For an allowed absent ref, submit a guessed commit | Refuse the wrong source. A present artifact supplies its recorded commit. An allowed absence follows the authored `rev` fallback or yields explicit null when `rev` is omitted. |
