# Codex selected direct patch hook failure evidence — 2026-11-06

[#838](https://github.com/realkarych/kora/issues/838) compares an explicit managed
denial with exit 1 for the same direct `apply_patch` call in the selected Codex
API-key profile. The earlier [Bash failure probe](codex-managed-hook-failures-2026-11-06.md)
covered the command path. The [built-in executor probe](codex-pre-tool-use-2026-11-05.md)
covered direct patch allow and deny.

## Observations

| Managed hook result | Correlated native observations | Output sent back to the scripted model | Marker after the completed turn |
| --- | --- | --- | --- |
| Exit 2 with the fixed reason on stderr | Hook `blocked`. Zero file-change or command lifecycle observations. | Exact PreToolUse refusal for the patch. | Absent, including a dangling link. |
| Exit 1 | Hook `failed` with `hook exited with code 1`. One `fileChange` item started and completed. Zero command lifecycle observations. | Correlated custom-tool result with exit code 0 and the added marker path. | Regular file with the exact marker bytes. |

Both runs used the same patch and call ID. Each acknowledged turn selected the
capability-authenticated exec-server with the built-in executor disabled. The
local Responses fixture verified two requests with the synthetic app-server key
per case and checked Codex's direct custom-tool advertisement. The system-managed handler
ran once and wrote one bounded decoded receipt for the exact patch and call ID.
The probe tied the single hook completion to the acknowledged thread and turn.
It also tied any tool lifecycle item to that thread, turn and call ID. The model
output and workspace marker were checked separately from those notifications.

The [pinned patch runtime](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/runtimes/apply_patch.rs#L172-L201)
uses the selected environment filesystem. The [pinned remote environment](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L810-L838)
supplies a remote filesystem client separate from the process backend.
The run establishes a direct-patch filesystem effect under the selected
profile. The source identifies the route. The shared-workspace fixture did
not independently trace the filesystem RPC or attribute the write to a
kernel-observed process. A guard for required direct-edit policy must cover
the native filesystem path under the
[official policy qualification contract](../specs/codex-execution-profile.md#required-tool-policy-activation).
The observations cover this patch, pin, Linux environment and exit 1.

## Reproduce on Linux

The wrapper verifies official `codex-cli 0.166.0` from upstream
[`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`](https://github.com/openai/codex/tree/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856).
The Linux amd64 musl archive SHA-256 is
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 image manifest digest is
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Executable revision `903e22ca64c88a8cb10ef2b0423c6621d2c7d5ee` on
`4700f79f07c78011dcdc4749ccd42ed1753f2166` uses fixture tree
`572724581925b47c76d0d6c388dbb3ad2e73e607` and provider tree
`ff04b89185da78ce6d824abb2f46124915d5a938`. The executable patch SHA-256
from `git diff --binary` over the fixture path is
`48441c3e6024a143db150401740c3888621512ef4d40f75ebf99eb351969c635`.
Run from the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
```

The runs used non-root, network-isolated amd64 containers in the aarch64
LinuxKit 7.0.14 VM on Docker Engine 29.9.2, Docker Desktop 4.98.0 and
Buildx 0.38.1. The containers had no host mounts or operator credentials.
The fixture used a synthetic provider key and a fresh executor bearer, and
all model traffic stayed on loopback. The build may fetch the pinned
release asset. The case, cleanup and container attach limits remained
30, 5 and 75 seconds, respectively.

| Command mode | Verified cases | Exact container removals | Exit | Wall time (diagnostic) |
| --- | ---: | ---: | ---: | ---: |
| `--check-selected-patch-hook-failures` | 2 | 2 | 0 | 19.59s |
| `--check-pre-tool-use` | 5 | 4 | 0 | 21.10s |
| `--check-selected-hook-failures` | 4 | 4 | 0 | 35.56s |

The two selected modes reaped both direct children in each of six cases,
drained their output and closed the Responses listener and connections.
The shell wrapper confirmed exact container removal and the final Docker
inventory found no remaining probe container. The built-in Bash allow and
deny cases share a container. The probe does not inventory all native
executor handles and descendants before removal.

Log SHA-256 values in table order are:

- `3d837ffcfb6fde0c6c2a37b00a333e6b42922b6b429e04fb1b20ff82d8e823a7`
- `be26e67a2990476a061ba562b3514dc8f5169595aaa3cea326b7355312e87311`
- `6eadcf484347d9d878bf826f533d426ee2283cd59ba804d4a923511c854ff4c5`

The logs identify cases and outcomes. The clean executable revision above
identifies their source inputs. Native macOS, additional patch failure modes,
production launch and mandatory Policy qualification remain with the implementing
owners.
