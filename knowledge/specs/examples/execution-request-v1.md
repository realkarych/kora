# Execution request version-one examples

These proposed codec vectors accompany the [canonical execution request contract](../execution-request.md). Identifiers, model prices and selections are synthetic. They support byte checks and provide no trusted policy, installed provider facts, durable owner or execution authority. Future integration checks acquire authentic saved facts and use the actual producers.

## Complete bodies

Each canonical body starts at the opening brace and ends at the closing brace. The LF before a closing fence is Markdown framing. Hash the UTF-8 bytes with no BOM or final LF.

| Value | Byte count | SHA-256 |
| --- | ---: | --- |
| Agent canonical | 1925 | `sha256:b73cd56ceeadfd246a3d4bb2d62c8f5f4958da4f7536c2755f851f12d469db9e` |
| Agent config canonical | 233 | `sha256:9e14e22962d671df37901ebffec692f7a09a46195d28096f8004e249934b819b` |
| Agent escaped source with LF | 1946 | `sha256:20fe21875ee2f45aecef20da0a54ede2a6169b76b6e9c2629c54997a986d149b` |
| Command canonical | 1546 | `sha256:2219b3b05d98c78e53e7f699a7ee4639a7eeb2c687077c142214723668020400` |
| Command config canonical | 80 | `sha256:e612f39374e6350d65a8d09951b4efcf8e1ab43e25bc75bfbfa99202c3cfe315` |

### Agent

The saved per-run parameter `label` is the string `β`. The selected template invokes no provider or external source. Its checked output is the request's rendered prompt. An omitted `config.system_prompt_append` becomes explicit null in the different, required `rendered.system_prompt_append` slot. Both `1` and `1.0`, and both integer `0` and real `-0.0`, are present as distinct spellings in the by-value output schema.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","config":{"harness":"claude","output_schema":{"examples":[1,1.0,0,-0.0],"type":"number"},"prompt":"Inspect “café” with {{ params.label }}.\nRecord \"checked\" and backslash \\.","workspace":{"repo":"/workspace/project.git","rev":"main"}},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"agent","limits":{"max_events":128,"stall_after_us":900000000},"node_path":"agent","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":{"implementation_version":"1.0","name":"claude","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAY","selection_revision":1},"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"prompt":"Inspect “café” with β.\nRecord \"checked\" and backslash \\.","system_prompt_append":null},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"backoff_coefficient":1.0,"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

### Command

This is the fixed command shape without artifact inputs or materialized resources. The harness, retry source and publication slots are explicitly null. No checkout result or effect evidence is supplied.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FB2","config":{"cmd":["check","--mode","fixed"],"workspace":{"repo":"/workspace/project.git"}},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"command","limits":{"stall_after_us":900000000},"node_path":"gate","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":null,"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"cmd":["check","--mode","fixed"]},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

## Equivalent logical sources

The escaped agent source uses equivalent spellings for the accents, Greek letter and LF escapes in the prompt. In this one raw source vector, append **one literal LF** after the closing brace. Its raw source digest differs from the canonical request digest. Prepare the same canonical agent bytes under the original stage.

```json
{"attempt_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","config":{"harness":"claude","output_schema":{"examples":[1,1.0,0,-0.0],"type":"number"},"prompt":"Inspect “caf\u00e9” with {{ params.label }}.\u000aRecord \"checked\" and backslash \\.","workspace":{"repo":"/workspace/project.git","rev":"main"}},"execution_policy":{"namespace_revision":7,"semantics":"kora.execution.v1","values":{"namespace":{"require_protected_execution":false},"node":{"tool_policy":{}}}},"feedback":null,"graph":{"contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","semantics":"kora.execution.v1","version_id":"01ARZ3NDEKTSV4RRFFQ69G5FAX"},"input_bindings":[],"kind":"agent","limits":{"max_events":128,"stall_after_us":900000000},"node_path":"agent","price_table":{"effective_from":"2026-01-01T00:00:00Z","id":"01ARZ3NDEKTSV4RRFFQ69G5FB1","namespace_id":null,"normalization_version":1,"prices":{"synthetic-model":{"cache_creation_1h":null,"cache_creation_5m":null,"cached_input":null,"input":"1.25","output":"0","reasoning":null}},"version":1},"protected_paths":[".kora"],"providers":{"harness":{"implementation_version":"1.0","name":"claude","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAY","selection_revision":1},"runtime":{"implementation_version":"1.0","name":"local","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FAZ","selection_revision":1},"vcs":{"implementation_version":"1.0","name":"git","selection_id":"01ARZ3NDEKTSV4RRFFQ69G5FB0","selection_revision":1}},"publication":null,"render_profile":"kora.jinja2.v1","rendered":{"prompt":"Inspect “caf\u00e9” with \u03b2.\u000aRecord \"checked\" and backslash \\.","system_prompt_append":null},"resources":[],"retry_policy":{"infrastructure_retry":{"max_attempts":1},"interpretation":"kora.disabled-retry-policies.v1","provider_retry":{"max_wait":"0s"},"retry":{"backoff_coefficient":1.0,"max_attempts":1}},"retry_source":null,"run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAW","secret_aliases":{},"version":1}
```

With the same trusted attempt, policy and saved facts, these transports reach the same canonical and config entries in the table.

| Form | Request and expected owner transition |
| --- | --- |
| Inline | Use the agent body as the logical object in `seal_request.body_or_upload_ref` and policy revision `7`. The API makes its canonical bytes durable through the original storage lifecycle before the conditional owner write. |
| Upload then seal | Send the escaped agent source as the payload, with its raw byte count and digest. Request version and selected attempt stay fixed. The result of upload is the same original reference in ready state, with the canonical body retained under that stage. Seal that exact reference at policy revision `7`. |
| Matching authorized seal replay | In separate clean cases for each form, compare the request and config digests with the same canonical entries. After the first commit and a policy revision change, return the same original saved descriptor and policy. The upload replay uses the original completion binding if staging cleanup already finished. |

Compare the logical identities across transports. Compare the exact descriptor on each transport's original accepted replay.

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
