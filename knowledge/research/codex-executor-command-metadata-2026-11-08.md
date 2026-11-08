# Selected Codex executor command correlation labels — 2026-11-08

[#979](https://github.com/realkarych/kora/issues/979) establishes two exact native environment names for the selected Codex launch constructor. `CODEX_THREAD_ID` and `CODEX_TOOL_CALL_ID` belong to native command correlation. The private preflight refuses either name in workload environment or workload secret names with `codex_launch_environment_conflict`. Its two initial native role environments supply neither value. Adjacent names remain available under the existing ownership rules.

## Pinned ownership and observation

The selected [command preparation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/unified_exec/process_manager.rs#L1495-L1507) puts the native IDs in the environment sent to the executor. [Process metadata](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/unified_exec/process_manager.rs#L221-L246) carries the same thread and call. The [native executor](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/local_process.rs#L744-L784) assembles the child environment and applies the process metadata after inheritance and overlay. The [pinned POSIX label code](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/shell_environment.rs#L6-L40) uses the exact names.

The fixture places stale values under both names only in the disposable exec-server environment, alongside an unrelated positive control. The selected non-login Bash command writes a bounded marker. The marker contains the exact thread ID acknowledged by app-server, the scripted tool call ID and the unchanged control value. The two native values replace the stale values. The original selected Bash and executor-home marker also matches, and the shell confirms the app-server key is absent from its environment.

The managed receipt matches the fixed Bash call and decoded command. The selected turn produces one started and one completed `commandExecution` item, with completion exit 0. The model receives the correlated exit-0 result with an empty command output body. The fixture keeps the observed values in a disposable workspace and reports fixed outcome labels. The marker records the values that reach the command through the selected pair. The ordering of assignments within the executor comes from pinned source because the app-server overlay also carries the IDs.

These labels can be changed by shell startup or the command. Use them for correlation observations. Policy, executable and endpoint binding, path authority, release of the production domain and complete validation facts remain separate under [the selected profile](../specs/codex-execution-profile.md) and [validation facts section 10](../specs/validation-facts.md#10-native-codex-declarations).

## Reproduction and cleanup

Executable revision `81e1ea6a6abf1b8fc295b441fd1c24fbf85caa4a`, Codex provider tree `3a51e5425a203dcc335d20236c404570b4a5606f`, fixture tree `ec9b133c048853651691069ea54da883d247b91a`. The build uses the [official Codex 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and verifies Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured run used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The build can fetch pinned assets. Each nonroot case container has `--network none` and no host mounts. A synthetic API key and attempt-local bearer are the only fixture credentials, and the model exchange is loopback. The existing case, cleanup and attach deadlines are 30, 5 and 75 seconds.

All four cases passed. The ordinary allow and denial retained their matched effects. The missing-key case reached neither the model fixture nor the hook, and the selected command metadata case matched both markers. The probe reaped eight direct native children, closed its streams and loopback server, and confirmed four exact container removals. Probe-container inventory was empty before and after. The command exited 0 in 30.47 seconds including image builds. The combined stdout/stderr log SHA-256 was `f770287affa5b5dc02cf9a2ad2961b6ce8cac693ba3636d23989fa139f965454`.
