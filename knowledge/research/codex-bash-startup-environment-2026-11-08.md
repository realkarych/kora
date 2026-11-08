# Selected Codex Bash startup environment

[#1016](https://github.com/realkarych/kora/issues/1016) verifies Bash startup in the selected Linux Codex profile. The private launch constructor refuses the exact `BASH_ENV` name in both authored name collections with `codex_launch_environment_conflict`. Both role recipes leave the variable unset. The conflict check uses names only and preserves acceptance of adjacent names.

## Native observation

Non-interactive Bash reads the startup file named by `BASH_ENV` even without login mode. The [Bash startup contract](https://www.gnu.org/s/bash/manual/html_node/Bash-Startup-Files.html#Invoked-non_002dinteractively) describes this behavior. The fixture plants the same fixed script in each fresh checkout. Only the adverse fixture adds `BASH_ENV` to the materialized exec-server environment, after the pure recipe has been constructed. The script refuses to write its marker if the approved command marker already exists, including as a dangling symlink. Each probe verifies that the expected markers are regular files with exact contents where present.

| Selected exec-server environment | Exact managed hook and approved command marker | Separate Bash startup marker |
| --- | --- | --- |
| Default constructed inputs | Same approved command received by the hook, exact marker and model-visible exit 0 | Absent |
| Same inputs with fixture-only `BASH_ENV` | Same approved command received by the hook, exact marker and model-visible exit 0 | Exact contents, written before the approved command marker |

Both command markers identify the selected Bash and executor home. The command also verifies non-login mode and absence of the app-server credential. This demonstrates an extra effect controlled by the native startup environment even when the hook received only the approved command. Runtime assembly and actual environment admission remain with [#184](https://github.com/realkarych/kora/issues/184). Complete declarations and fail-closed Policy enforcement remain with [#549](https://github.com/realkarych/kora/issues/549) and [#199](https://github.com/realkarych/kora/issues/199).

## Reproduction

Executable revision `68b2fb0ac5ecef192a2c8b344cd6c1b054625985`, Codex provider tree `a39b9c0d582973354376dc55731d3bb24bd69d8a`, fixture tree `a0edc7fc45e5d5154401d1c48a8df97417bb5ed4`. The build verifies the [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The build can fetch pinned inputs. Execution uses nonroot containers with `--network none`, without host mounts, with a synthetic API key and attempt-local executor bearer. Model traffic stays on loopback. The existing case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bash-startup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected Bash startup | 2 | 4 | 2 | 0 | 19.00 s |
| Selected launch regressions | 4 | 8 | 4 | 0 | 30.51 s |

Both modes closed their owned streams and local model server. Probe-container inventory was empty before and after each mode. The combined stdout/stderr log SHA-256 values in table order were `fbfa2d188140a813f9037c2a37ece250daac997d671719034d4f42714e4e427a` and `3ac61308ac595d7a7b0eb09fe33727c6b32f6c30b8dbd9ca47dc02e545605e62`.
