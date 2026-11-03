# Managed Codex one-shot shell probe

Observed on 2026-11-04 (Europe/Moscow) for [issue #670](https://github.com/realkarych/kora/issues/670). The tested executable fixture tree at `tests/plugins/harness/codex` was `c964547ff47e1d3a11d365945b7536f44cda6594`, based on kora `6171f46`.

## Reproduce

Use the same local Docker Engine and Buildx prerequisites and pinned Linux amd64 Codex 0.166.0 archive and base image as the [managed shell hook probe](codex-managed-hook-probe-2026-11-04.md). From the repository root, run the comparison and the original regressions.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-write-stdin
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
```

The comparison runs the interactive and one-shot variants in separate unprivileged containers. Each has a private PID namespace, fresh app-server configuration and workspace, and a loopback-only Responses fixture. The launcher confirms container removal before starting the next variant. The probe uses the same per-session and cleanup deadlines as #652. Codex receives neither credentials nor externally generated model responses.

## Comparison

The interactive variant sets a managed catch-all `PreToolUse` matcher. Its hook records an invocation before parsing and filtering input. The fixture asks `exec_command` to start a PTY command that prints a readiness line and waits for a fixed token. On the next Responses request, it checks the correlated readiness line and running session ID before issuing nonempty `write_stdin`. The command writes fixed bytes to a workspace marker only after receiving that token. The fixture checks the correlated write result, the driver waits for the matching completed turn, and the probe checks both the marker bytes and the exact initial Bash receipt.

The managed variant sets `[features] unified_exec = false` in the root-owned `/etc/codex/requirements.toml` while the isolated user configuration requests `unified_exec = true`. The model request advertises `exec_command` with `timeout_ms` and omits the interactive `tty` and `yield_time_ms` parameters. The fixture deliberately supplies a `write_stdin` call although the model request omits the tool. It requires Codex's correlated `unsupported call: write_stdin` result before continuing to the ordinary marker command. The Bash allow and deny cases retain their original managed matcher and check the correlated result and marker after turn completion. [Pinned tool registry](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/spec_plan.rs#L1243-L1275), [one-shot schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L280-L310)

## Observations

The comparison and original regressions exited zero in a Linux amd64 container under Docker Desktop 29.9.2 and Buildx 0.38.1. The VM used an aarch64 LinuxKit 7.0.14 kernel on Apple Silicon and ran the amd64 containers through emulation.

| Case | Observed result |
| --- | --- |
| Interactive | Codex advertised both native tools. The write to the live session produced the expected marker bytes. Across the completed turn, the managed catch-all hook ran once and its receipt matched the initial Bash command. |
| Managed one-shot, allow | Codex advertised one-shot `exec_command` and omitted `write_stdin`. The attempted write returned `unsupported call: write_stdin` for the scripted call ID. The ordinary Bash hook ran once, the turn completed and the allowed marker appeared. |
| Managed one-shot, deny | The ordinary Bash hook ran once and Codex reported its fixed refusal reason. The turn completed and the marker was absent. |
| Original probes | Direct Bash allow and deny, and the stalled-hook deadline and removal check, passed. Docker confirmed removal of every probe container. |

A separate host-side check sent SIGTERM to the launcher on Docker's destroy event for the interactive container. The launcher exited 143 before the one-shot cases ran, and a Docker lookup confirmed the reserved container name was absent.

This evidence covers direct native tool dispatch on the pinned binary and the synthetic fixture. Hook receipts and the test workspace share the container UID. Production isolation, repository configuration, other tool kinds and adapter or runner integration require separate evidence.
