# Selected Codex managed hook identity, 2026-11-08

[#1012](https://github.com/realkarych/kora/issues/1012) retains the existing managed hook's decoded session and turn fields. The private decoder already validates these as Unicode strings within the byte-bound envelope. It now preserves them exactly. The Policy request uses the caller's trusted workspace and node, with no session or turn claim from the native input. Decoder representations, refusal diagnostics and fixture output omit the observed identities.

## Native correlation

In eight selected official Codex 0.166.0 Linux cases, the decoded hook session matched the independently selected root thread, and the decoded hook turn matched the acknowledged turn. The app-server reported one managed completion for the same thread and turn with the exact run ID `pre-tool-use:0:/opt/kora-probe:kora-hook-probe-call`. The fixture also checked the exact hook receipt, including the fixed call and action, and the existing native result and workspace checks.

| Tool | Hook result | Native completion | Marker |
| --- | --- | --- | --- |
| Bash | Explicit denial | Blocked | Absent |
| Bash | Exit 2 without a reason, exit 1, timeout | Failed in each case | Exact marker present in each case |
| Direct patch | Allow | Completed | Exact marker present |
| Direct patch | Explicit denial | Blocked | Absent |
| Direct patch | Exit 1, timeout | Failed in each case | Exact marker present in each case |

The timeout cases retain the existing live Unix peer and receipt PID match, pidfd observation of the hook's exit while both native roles remained live, and independent turn and container cleanup.

The pinned [native hook dispatch](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/hook_runtime.rs#L196-L223) takes the hook session and turn from the session and turn context. The [handler identity](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/engine/mod.rs#L211-L223) and [tool suffix](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/events/common.rs#L115-L127) form the exact run ID. The [app-server projection](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1154-L1163) reports that completion alongside the native thread and turn.

This evidence covers correlation in fresh selected root sessions. Required Policy still depends on the runner's trusted attempt binding and proven prevention before effects. See [#199](https://github.com/realkarych/kora/issues/199) and the [selected activation requirements](../specs/codex-execution-profile.md#required-tool-policy-activation).

## Reproduction and cleanup

Executable revision `2226b8cb5c2d2ef049bd5934907166a7d72e161f`, fixture tree `5ea79910cf7d45adf963b8f3be99bbfe7bf6d7c6`, provider tree `f3b065c77a111d76a1305a261af84df99fed8bde`. The fixture verifies the [official archive](https://github.com/openai/codex/releases/tag/rust-v0.166.0) from source `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. Python 3.11.17 image manifest `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout`, a local Docker socket and Buildx. The measured execution used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. Build can fetch pinned inputs. The nonroot execution containers use `--network none`, no host mounts and loopback-only model responses. Selected cases use a synthetic API key. Case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-pre-tool-use
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-startup
```

| Mode | Cases | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: |
| Selected Bash hook failures | 4 | 4 | 0 | 38.47 s |
| Selected patch hook cases | 4 | 4 | 0 | 35.36 s |
| Direct hook regression | 5 | 4 | 0 | 20.64 s |
| Selected startup regression | 3 | 3 | 0 | 13.03 s |

The eleven selected cases reaped both native children, including the refused startup cases. The fixture closed its owned streams and listeners. Probe-container inventories were empty before and after. Combined stdout and stderr SHA-256 values in table order were `88b2eb3613fd6bcc05a61a6980b843c2e198ecee77e9275dbde4523be9066d2e`, `6ad27a86aa03be9bec69d003150765eac3cada8fc4888e9553bd3f312d0c7088`, `691ba8980370c30cb278116e7bc81af5d8bd59c410bd508517cd7048d46c10e7` and `948e3fde4c02b4d46499f81860b257d3373312ec1f85fd3232525f7f491e4e77`.
