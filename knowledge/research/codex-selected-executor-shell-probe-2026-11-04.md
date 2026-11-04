# Codex selected executor shell probe

Observed on 2026-11-04 (Europe/Moscow) for [#693](https://github.com/realkarych/kora/issues/693). The executable fixture tree at `tests/plugins/harness/codex` was `87450be01a079db3c09cd081c9aee8435bbc770c`, based on kora `6b9edf914e7097f10abb4efb713ebe7930124b86`.

## Reproduce

Use the Docker Engine and Buildx prerequisites and pinned base image from the [managed hook probe](codex-managed-hook-probe-2026-11-04.md). Both native processes use Codex CLI 0.166.0 from upstream commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and the verified Linux amd64 musl archive with SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-executor-shell
sh tests/plugins/harness/codex/run_hook_probe.sh --check-shell-controls
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-write-stdin
sh tests/plugins/harness/codex/run_hook_probe.sh --check-provider-env-key
```

The selected-executor command runs one case per disposable container. The app-server uses stdio and the separate exec-server listens on an ephemeral IPv4 loopback port. Each process has its own fresh home and Codex state. The fixture configures native capability-token authentication with a fixed synthetic bearer, passes only its digest on exec-server argv and supplies the bearer through the app-server registration. The local Responses fixture receives no authorization header. Runtime containers use loopback-only networking, no host mounts or published ports, and the existing unprivileged user. The probe emits fixed status diagnostics and discards native state.

The app-server's startup selector disables the built-in executor. The driver requires an unknown result for the `local` environment, verifies Bash and the expected path on the registered executor, and selects only that environment. The managed configuration uses one-shot mode and disables login shells. The app-server user config requests the opposite. The same command records the resolved invocation path and Bash login state, plus a fixed nonsecret origin witness supplied only in the executor's environment. Successful tool results require exit zero in Codex's response header.

The driver checks one managed Bash hook invocation and exact nested input for each case. For completed turns it correlates the tool result and terminal turn. For the stall it verifies the only scripted Responses request and the live matching hook before cleanup. The startup and turn, shared cleanup and host attach deadlines are 30, 5 and 75 seconds. The driver reaps both native children and closes the Responses listener. It checks final markers after native cleanup. The launcher confirms container removal before advancing.

## Observations

All six commands exited zero under Docker Desktop 29.9.2 and Buildx 0.38.1. The Linux amd64 containers ran through emulation in an aarch64 LinuxKit 7.0.14 VM on Apple Silicon.

| Case | Observed result |
| --- | --- |
| Omitted shell and login | The selected executor reported Bash. The tool exited zero and the marker showed non-login Bash and the executor-only origin witness. |
| Nonexistent absolute path ending in `/bash` | The tool exited zero and the marker still showed the executor's Bash invocation path, non-login state and origin witness. |
| Explicit `shell: "/bin/sh"` | The hook allowed the command. Codex reported that the selected environment supports only Bash. The marker was absent. |
| Explicit `login: true` | The hook allowed the command. Codex reported the native login-disabled error. The marker was absent. |
| Managed hook denial | Codex reported the hook's fixed refusal. The marker was absent. |
| Managed hook stall | The matching hook was live at the turn deadline and the marker was absent after both native processes were reaped. Docker confirmed removal. |
| Prior probes | Local shell controls, Bash allow and deny, stalled-hook cleanup, interactive and managed one-shot stdin, and the provider-key checks passed. |

All five non-stall executor turns completed. Docker confirmed all container removals and the final probe inventory was empty.

## Scope

The pinned [shell preparation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/preparation.rs#L216-L244) removes the model-supplied shell argument on the selected path and refuses a mismatched type before using the reported shell. The same-type path case records the effective Bash invocation. The distinct-type refusal and the executor-only origin witness distinguish this fixture from the [earlier local shell observations](codex-managed-shell-controls-probe-2026-11-04.md).

The observations cover a Bash-reporting executor and a cooperative fixed command under a shared fixture UID. Production endpoint reachability and authorization, missing executor shell metadata, shell-snapshot startup, other tools and platforms, and independent reaping of the hook descendant before Docker removal retain their own evidence obligations. Both complete native declarations remain unavailable under [validation facts §10](../specs/validation-facts.md#10-native-codex-declarations).
