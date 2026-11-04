# Managed Codex one-shot shell controls probe

Observed on 2026-11-04 (Europe/Moscow) for [#688](https://github.com/realkarych/kora/issues/688). The tested executable fixture tree at `tests/plugins/harness/codex` was `10866004779ea3f175e037d3a547c7ab34d9a248`, based on kora `c92adfc3d96af35062fe5a327a511f918badb50d`.

## Reproduce

Use the Docker Engine and Buildx prerequisites and pinned base image from the [managed hook probe](codex-managed-hook-probe-2026-11-04.md). The probe runs Codex CLI 0.166.0 from upstream commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and verifies the Linux amd64 musl archive with SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-shell-controls
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-write-stdin
sh tests/plugins/harness/codex/run_hook_probe.sh --check-provider-env-key
```

The shell-controls command runs each case in a separate unprivileged, loopback-only container with fresh home, Codex state and workspace. The launcher confirms each removal before continuing. The turn, cleanup and attach deadlines are 30, 5 and 75 seconds respectively. Strict user configuration requests `allow_login_shell = true`; root-owned requirements set it to false and preserve managed one-shot execution. The fixture UID has a root-owned Bash passwd entry. The container image checks that Bash and POSIX sh resolve to distinct executables.

The local Responses fixture supplies the same command text with three argument sets. The driver matches the scripted call's tool output and the app-server's ephemeral thread and terminal turn IDs. The catch-all managed hook records each entry before reading input, checks the actual nested input and permits the fixed command. The driver checks one entry and one exact Bash receipt with `tool_input` containing the command only. The marker records the resolved invocation path from `$0` and the Bash builtin login-state observation when applicable. Marker checks run after app-server reaping and listener closure. The launcher reports fixed status messages.

## Observations

All five commands exited zero under Docker Desktop 29.9.2 and Buildx 0.38.1. The Linux amd64 containers ran through emulation on an aarch64 LinuxKit 7.0.14 VM on Apple Silicon.

| Scripted arguments | Correlated result and marker |
| --- | --- |
| Omit `shell` and `login` | The Bash hook permitted the command. The native tool reported exit code zero. The marker reported the resolved Bash invocation path and Bash non-login mode. |
| Set `login: true` | The Bash hook permitted the same command before native dispatch returned ``login shell is disabled by config; omit `login` or set it to false.`` for that call. The turn completed and the command marker was absent. |
| Set `shell: "/bin/sh"` and omit `login` | The Bash hook permitted the same command with identical nested tool input. The native tool reported exit code zero. The marker reported the distinct resolved POSIX sh path and no Bash indicator. |
| Prior checks | Bash allow and deny, stalled-hook cleanup, interactive versus one-shot stdin, and the account and explicit provider-key cases all passed. |

Docker confirmed removal of every container. The final host probe inventory was empty.

## Scope

The results support managed refusal of a login request for the direct one-shot command and show that the model can still select a different installed interpreter in this pinned fixture. The managed precedence and hook order are explained by the pinned [config projection](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/config/src/config_requirements.rs#L1716-L1737), [native dispatch](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/registry.rs#L835-L882) and [shell resolution](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec.rs#L151-L177).

The shared test UID and fixed cooperative command limit the marker to invocation and interpreter behavior evidence. Native shell-snapshot startup, other shell selections and platforms, the production launch restriction and complete policy coverage retain their own evidence obligations. [Validation facts §10](../specs/validation-facts.md#10-native-codex-declarations) governs the availability of complete declarations.
