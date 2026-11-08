# Model-visible selected Codex tools — 2026-11-08

[#996](https://github.com/realkarych/kora/issues/996) records the first model request from the selected Linux app-server and exec-server pair on official Codex 0.166.0. Both fresh cases use the fixture's selected `gpt-5.5`, one-shot shell controls, disabled web search, private native roles and managed hook. The loopback Responses endpoint verifies the configured model and checks the same complete inventory in each case.

## Observed identities

| Responses type | Name or nameless type identity | Classification input |
| --- | --- | --- |
| `custom` | `apply_patch` | File edit |
| `function` | `exec_command` | One-shot shell command |
| `function` | `create_goal`, `get_goal`, `update_goal` | Native goal operations. Residual candidates. |
| `function` | `request_user_input` | Native user-interaction control. Residual candidate. |
| `function` | `view_image` | Local image read. Residual candidate. |
| `tool_search` | Nameless `tool_search` | Discovery surface. Its reachable tools need separate classification. |

All eight entries are top-level. The inventory contains no namespace, hosted `web_search`, `write_stdin` or `update_plan`. The fixture retains types and names, with a qualified namespace representation for the parser. It caps the inventory at 64 entries, bounds each name at 64 permitted ASCII characters, rejects duplicate identities and compares the sorted inventory with the observed snapshot. Diagnostic output contains only validated identities and fixed outcome labels.

The pinned [tool serialization](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/tools/src/tool_spec.rs#L18-L64) distinguishes named tools from nameless search types. The pinned [configuration resolver](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/config/mod.rs#L2824-L2830) enables `update_plan` only on explicit configuration. Its [native handler](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/plan.rs#L87-L98) emits a plan update before a successful result. The [utility registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/spec_plan.rs#L1295-L1333), [goal extension](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/ext/goal/src/tool.rs) and [image registration](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/spec_plan.rs#L1440-L1450) identify the residual candidates above.

## Matched native dispatch

- The positive case submits the existing Bash call and verifies its exact managed hook invocation and receipt, model-visible success and executor-home marker.
- The adverse case submits a valid-shaped `update_plan` function call in the same ordinary selected mode. The second request contains exactly one correlated `unsupported call: update_plan` result. The observer sees no `turn/plan/updated` through matched turn completion. The invocation canary, hook receipt and workspace marker are absent. Both cases install the same managed allow decision, so an unexpected hook can record its invocation in either case.

The discovery revision `271eebc90df79459cbca7c97a042447aab80d5b3` observed the same eight identities and dispatch outcomes before the exact snapshot was added. This evidence covers the first request for the named configuration and one injected disabled tool. Other model/configuration branches, discovery results, the residual tools' own interception and complete coverage retain separate checks under [#549](https://github.com/realkarych/kora/issues/549) and [#199](https://github.com/realkarych/kora/issues/199). Production launch and domain closure remain with [#184](https://github.com/realkarych/kora/issues/184).

## Reproduction

Executable revision `54c1888774c05164e6779df5297d9deaa76dc92b`, provider tree `12129d3d6c6e2e426b21b351a9123034c7dfbb19`, fixture tree `c80c170294cb897f42c21fc934ce3255a56565e5`. The fixture verifies the [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at source `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. Build can fetch pinned inputs. The nonroot execution containers use `--network none`, no host mounts and loopback-only exchange with synthetic credentials. The case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-tool-inventory
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected tool inventory | 2 | 4 | 2 | 0 | 16.74 s |
| Selected launch regressions | 4 | 8 | 4 | 0 | 31.96 s |

Both modes closed their owned streams and listeners. Probe-container inventories were empty before and after. Combined stdout/stderr log SHA-256 values in table order were `5ae0e3bb2a735553f6bdfd6c8a2a7082259a21a06c039850cbcc11127ffaf20f` and `485426994eefa06d1388005dd5878c699abf64ecc89c98288141187cd3ef013e`.
