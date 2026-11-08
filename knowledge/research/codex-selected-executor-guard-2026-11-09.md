# Selected Linux Codex before-effect executor fixture

[#1004](https://github.com/realkarych/kora/issues/1004) tests a fixed Bash command and a fixed direct `apply_patch` against official Codex 0.166.0. The native app-server reaches the real exec-server through a fixture-owned WebSocket relay to private stdio. Each case uses a fresh app-server, exec-server, workspace, loopback Responses server and synthetic provider key. The app-server receives a separate attempt-local relay bearer. The fixtures send the same initial tool call within each tool's cases.

## Results

The native controls use the existing authenticated WebSocket exec-server. The guarded cases use the fixture relay. The tuple records the number of effect requests seen by the relay, forwarded to native stdio and refused by the relay, respectively.

| Tool and hook outcome | Native marker | Guarded marker | Guarded effect counts |
| --- | --- | --- | --- |
| Bash allow | Exact Bash executable and executor home | Same exact bytes | `1, 1, 0` |
| Bash explicit denial | Absent | Absent | `0, 0, 0` |
| Bash exit 2 without reason | Exact bytes | Outside guarded matrix | — |
| Bash exit 1 | Exact bytes | Absent | `1, 0, 1` |
| Bash five-second timeout | Exact bytes | Absent | `1, 0, 1` |
| Direct patch allow | Exact patch bytes | Same exact bytes | `1, 1, 0` |
| Direct patch explicit denial | Absent | Absent | `0, 0, 0` |
| Direct patch exit 1 | Exact patch bytes | Absent | `1, 0, 1` |
| Direct patch five-second timeout | Exact patch bytes | Absent | `1, 0, 1` |
| Direct patch staged permit followed by timeout | Covered by native patch timeout | Absent | `1, 0, 1` |

The app-server reports the matching managed hook source, call, result and exact status entry for the acknowledged thread and turn. The fixture checks the private input receipt separately. A positive result releases one exact request only when the staged input also matches that thread, turn, call, tool and input. In the staged direct patch case, the Unix peer credentials and pidfd prove that the exact hook process staged the permit while alive, then exited and closed its peer after the native timeout. Both native roles are still live at that observation. A successful same-turn hook completion is mandatory even when a permit exists.

The relay admits the fixed native startup and metadata shapes, one Bash `process/start` or direct patch `fs/writeFile`, and the matching allowed Bash process termination. Refusal sends the original request ID with error code `-32600` before any effect request reaches native stdio. The synthetic model sees the fixed Bash refusal or the native patch write failure. The relay settles its listener, connection and response pump before the fixture checks counters, hook identity and marker. The local Responses fixture sees exactly two requests per guarded case. Missing and incorrect bearer, authenticated ordinary HTTP, an unexpected WebSocket subprotocol, a malformed HTTP header, a second connection and reconnect after fencing are exercised. Host-side checks reject any output containing either canary. The malformed request produces a fixed diagnostic.

## Reproduction

Run from a checkout containing the executable fixture revision. The host needs GNU `timeout`, uv, CPython 3.11.17 and a local Docker socket with Buildx and Linux amd64 support. Image setup can fetch the pinned binary and hashed test wheels. The fixture itself runs as a nonroot user with `--network none`, no host mount, loopback-only model traffic and no real credentials. Its launch-to-terminal deadline is 30 seconds, its child and endpoint cleanup deadline is 5 seconds, and the container attach deadline is 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-executor-guard
```

The locked test group `codex-probe` uses `aiohttp==3.14.5` and existing transitive pins. The build verifies the [official release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, the archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978` and the executable version. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

The complete run passed on executable revision `e5109028a508ac55f7abce4b60cec4e86cc82b3b`, fixture tree `3fd9caf1bbae9ae97bad4f6c423a03d984117643`, Codex provider tree `705f614b4bd60a7cedcbffe2fbc0e21c10607cd4` and lock blob `881ac4770ed91a6b738b9cde54c42323191c2bdc`. It ran in Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on a macOS arm64 host. The outer command used `timeout -k 20s 520s`. It exited 0 after 171 seconds, reported nine guarded verdicts, nine fixed parser diagnostics, 18 native pair reaps and 18 exact container removals, and left both before and after container inventories empty. Neither canary appeared in the combined log. Log SHA-256 `7c2bcb6a23be462af7e63a9694d89cf53772d34d6c4eebb1692b8dd05bd374ef`.

The initial executable revision `31b0df71b1f70ba3d726a74e2a2691d2139aa911` passed the nine native controls and failed at relay startup. The pinned native executor sends explicit null parameters for environment status and information. The corrected fixture admits those exact shapes. The full run above also covers the later subprotocol refusal and unsigned watch identity checks.

The combined role-launch and executor-guard executable revision `3c7ff3d7cde65b3d2e50ea88d8e02859f2f688e3` has fixture tree `76b174b43590636f0ffe1875525d84871a8c8364` and Codex provider tree `1aca51f3751c3854ceaee51e14e4b1553dd73b36`. It uses the same lock blob and Linux environment. The complete guard matrix again exited 0 after 171 seconds, with 18 exact removals, empty before and after inventories and log SHA-256 `f8ec8e099c61b56f526cb2b2cb09da2adfe187409ad68fb50be7117263a38ac4`. On the same executable revision, `--check-selected-native-context` exercised the default conformance dependency export and the shared wrapper. The selected launch, both legacy allocation refusals and cancellation matched their expected results, and all four exact containers were removed. Its before and after inventories were empty and the log SHA-256 is `b9a68ec944e2743033684183502ad5771791233f97aafb0051691ab051861d1f`.

## Scope

The fixture checks two fixed effects in fresh one-turn sessions. Arbitrary patch batches, Bash-routed patch, alternate effect methods, persistent session reconnect and replay are outside this matrix. The authorization file belongs to the fixture. The accepted [activation boundary](../specs/codex-execution-profile.md#required-tool-policy-activation) requires a production decision bound to the actual covered effect. Runtime ownership and whole-domain closure remain with [#184](https://github.com/realkarych/kora/issues/184), authenticated Policy and first-cause handling with [#199](https://github.com/realkarych/kora/issues/199), and complete effect-free declarations with [#549](https://github.com/realkarych/kora/issues/549). [#1030](https://github.com/realkarych/kora/issues/1030) supplies separate evidence for Bash-routed patch.
