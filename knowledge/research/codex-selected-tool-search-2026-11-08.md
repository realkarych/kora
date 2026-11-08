# Deferred tools in selected Codex search — 2026-11-08

[#1001](https://github.com/realkarych/kora/issues/1001) measures one native search on the selected Linux profile. Both fresh official Codex 0.166.0 app-server and exec-server pairs use `gpt-5.5`, private native homes, the same synthetic API key and a managed `PreToolUse` matcher that catches every tool name. Both install the fixture's known Bash allow decision and verify the same eight first-request identities from the [initial inventory](codex-selected-tool-inventory-2026-11-08.md).

| Case | Matched model result | Managed hook | Workspace effect |
| --- | --- | --- | --- |
| Bash control | One correlated successful command output | Exactly one invocation and the exact shell receipt | Exact marker with Bash and the executor home |
| Client search for `spawn agent` with limit 8 | One correlated `tool_search_output` with `execution: client` and `status: completed` | No `hook/completed` in the observed turn, invocation canary or receipt | Marker absent after turn completion |

The search returns one namespace, `multi_agent_v1`, with five functions: `close_agent`, `resume_agent`, `send_input`, `spawn_agent` and `wait_agent`. None of those identities or the namespace appears in either first-request inventory. Each case completes after exactly two model requests. After observing the search result the scripted endpoint sends only the final response.

The fixture projects bounded type and name identities with namespace qualification and rejects duplicates. It compares the first-list and result inventories separately. It records validated identities and fixed outcome labels. The receipt is checked separately from the invocation canary, which the hook writes before decoding.

The pinned [tool registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/spec_plan.rs#L393-L400) requires a searchable deferred entry for ordinary discovery exposure. The [multi-agent registration](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/spec_plan.rs#L1555-L1583) defers the five functions for a model with search support. The [router](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/router.rs#L275-L293) gives the client search its own payload. The [handler](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/tool_search.rs#L209-L280) handles that payload with the [default hook conversion](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/registry.rs#L173-L182), which accepts only function payloads. These source paths explain the observed search result and absence of the hook.

This is one ranked search for one model/configuration on Linux. The fixture submits no discovered tool call. Complete deferred-tool enumeration, classification, subagent behavior and enforcement on any discovered call remain separate obligations under [#549](https://github.com/realkarych/kora/issues/549) and [#199](https://github.com/realkarych/kora/issues/199). The production launcher and domain closure remain with [#184](https://github.com/realkarych/kora/issues/184).

## Reproduction

Discovery revision `79ca6275c552ccb3832c3e0713487d8d0dd68536` observed the same identities and absent invocation before the exact inventory and native notification assertions were added. The final executable revision is `0461dad057cf658c985513f746de5fb9f314a592`, fixture tree `85b37e39afbdfa9f1bfa07eadc6ac5749fbf5907` and provider tree `12129d3d6c6e2e426b21b351a9123034c7dfbb19`. The fixture verifies the [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at source `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. These runs used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. Build can fetch pinned inputs. The nonroot execution containers have `--network none`, no host mounts and a loopback-only model endpoint with synthetic credentials. The case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-tool-search
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-tool-inventory
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected tool search | 2 | 4 | 2 | 0 | 15.69 s |
| Selected tool inventory | 2 | 4 | 2 | 0 | 15.45 s |

Both modes closed the owned streams and listeners. The probe-container inventory was empty before and after. Combined stdout/stderr log SHA-256 values in table order were `4164a8286f406a8019048418226771e4b59d50cc8b19ca4a520adf69397755c9` and `47fa69c89cfe2bea5da18918a9ab44fc367e423f3426766f244f568c1c30dfa3`.
