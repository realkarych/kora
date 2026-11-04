# Managed Codex shell hook probe

Observed on 2026-11-04 (Europe/Moscow) for [issue #652](https://github.com/realkarych/kora/issues/652). The tested fixture tree at `tests/plugins/harness/codex` was `158eb10eb38b8559729a84e10540269f50e942ca`, based on kora `b2573db4ade306fbb45afc8b5a5a9a1b56c0fdcd`.

## Reproduce

From the repository root, use Docker Engine with a local Unix socket and Buildx, a POSIX shell, `tar` and `timeout` with kill-after support. The probe runs a Linux amd64 image. The initial build can fetch the public base image and release archive. The launcher selects the local Docker socket and gives Docker a fresh configuration and environment for the build and container lifecycle. The running container has loopback networking, a private PID namespace, no host mounts and no credentials.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
```

The binary reports `codex-cli 0.166.0`. The [upstream release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) resolves to `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The build verifies `codex-x86_64-unknown-linux-musl.tar.gz` against SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The amd64 Python 3.11.17 Debian slim base manifest is pinned to `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

## Method

Each case starts a fresh app-server over stdio in a new workspace with separate home and Codex config directories. The app-server reads a managed `PreToolUse` command hook from the container's `/etc/codex/requirements.toml` with `allow_managed_hooks_only = true`. The matcher is `^Bash$`. The fixture runs with Codex approval policy `never` and sandbox mode `danger-full-access` inside the unprivileged, network-isolated container. This makes the allow run a positive control for the command and workspace.

The custom provider uses HTTP Responses at an ephemeral loopback port. The bundled `gpt-5.5` profile selects the Responses request shape with top-level tools in this Codex release. The model name serves as a local configuration selector. The fixture returns identical SSE bytes with a single `exec_command` call, `touch kora-probe.marker`, in both cases. It checks the correlated tool output in Codex's next request before returning a fixed closing message. The driver waits for the corresponding `turn/completed` with status `completed`, then checks the hook receipt and workspace. The hook exits 0 for allow and 2 with a fixed reason for deny. The [pinned client code](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/client.rs) and [upstream exit-2 test](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/tests/suite/hooks.rs#L5097-L5165) describe the respective wire shapes.

The cleanup case stalls the hook after recording readiness. The driver verifies a matching live hook at the 30-second monotonic deadline, before Codex's 60-second hook timeout. The driver has five seconds to close the fixture and wait for the app-server, with process-group escalation. The launcher verifies removal of the exact container. Its private PID namespace also contains hooks that Codex starts in separate sessions.

## Results

Both documented commands exited 0 in the Linux amd64 container under Docker Desktop 29.9.2 and Buildx 0.38.1. The VM used an aarch64 LinuxKit 7.0.14 kernel on an Apple Silicon host and ran the amd64 image through emulation.

| Case | Observation |
| --- | --- |
| Allow | Exactly one matching Bash hook receipt. Codex finished the turn and a regular marker file existed. |
| Deny | Exactly one matching Bash hook receipt for the same command and call ID. Codex returned `Command blocked by PreToolUse hook:` with the fixed hook reason for that call. The turn finished and the marker path was absent. |
| Stalled hook | One matching hook was live when the driver deadline expired. The marker was absent. The fixture closed, the driver waited for app-server, and Docker confirmed container removal. |

An additional host-side check launched the stalled case with a synthetic credential-bearing Docker proxy configuration. Once the hook receipt existed, container inspection showed no proxy variables, no mounts and network mode `none`. Sending SIGTERM to the wrapper produced exit status 143 and the exact container was absent afterward.

The observed policy result applies to one Bash call on one pinned binary and configuration. The remaining adapter conformance, integration with the shared runner and production sandbox behavior require their own evidence.
