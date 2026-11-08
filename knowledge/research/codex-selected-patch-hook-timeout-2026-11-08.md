# Selected Codex direct patch hook timeout — 2026-11-08

[#973](https://github.com/realkarych/kora/issues/973) tests the direct file-edit path through the selected official Codex app-server and exec-server on Linux. It adds the successful managed-hook control and native timeout case to the prior [explicit denial and exit 1 evidence](codex-selected-patch-hook-failures-2026-11-06.md). All four patch cases use the same catch-all hook registration and five-second native hook deadline.

| Managed hook behavior | Matched hook completion | Model-visible result | Workspace marker after the turn |
| --- | --- | --- | --- |
| Exit 0 with empty output | `completed`, no entries | Patch exit 0 | Exact marker bytes |
| Exit 2 with the blocking reason | `blocked`, exact feedback | Exact PreToolUse refusal | Absent |
| Exit 1 | `failed`, exact exit error | Patch exit 0 | Exact marker bytes |
| Still waiting at the native deadline | `failed`, exact five-second timeout error | Patch exit 0 | Exact marker bytes |

Each case matches one hook invocation and its decoded patch receipt to the scripted custom tool call. It matches the native hook completion to the acknowledged thread and turn. Successful patch responses accompany one started and one completed `fileChange` item. Explicit denial produces zero tool lifecycle items. All four cases observe zero command lifecycle items. The marker check independently confirms absence or a regular file with the exact bytes. The experiment observes the terminal filesystem effect. It does not timestamp the write or trace the underlying filesystem RPC.

The timeout hook opens a private Unix socket connection after writing its receipt. The fixture matches the socket peer PID to the receipt, observes the live helper through its pidfd, then verifies helper exit and socket EOF with both native role pidfds still live. The same checks pass for the Bash timeout regression. This confirms the native timeout case rather than a fixture-triggered stop. Each case then reaps the two direct native children, closes the loopback Responses server and owned streams, and removes its exact container. Container inventory was empty before and after. These checks establish direct child and fixture cleanup. Full runtime domain settlement and required Policy enforcement remain owned by [#184](https://github.com/realkarych/kora/issues/184) and [#199](https://github.com/realkarych/kora/issues/199).

## Reproduce

The tested kora revision was `ec57937f18f390c7042bcc4b5eb5a455ea6eed6f`, with Codex provider tree `0a66c20b29f66a13ec88dabaee9f17c1c5f3d193` and fixture tree `972290f4bbe165c03374bbdd7c2ef02f131110b3`. The [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) resolves to `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Docker build verifies the Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978` and checks the binary version. The Python 3.11.17 base image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the repository checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment was a macOS arm64 host with Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14, executing Linux amd64 containers. The build can fetch pinned assets. Each case runs in a nonroot container with `--network none` and no host mounts, using a synthetic `CODEX_API_KEY`, an attempt-local executor bearer and loopback Responses. The existing case, cleanup and Docker attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
```

| Mode | Cases | Native children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected direct patch | 4 | 8 | 4 | 0 | 37.93 s |
| Selected Bash | 4 | 8 | 4 | 0 | 34.93 s |

The Bash regression retained the [prior results](codex-managed-hook-failures-2026-11-06.md). Explicit denial left the marker absent. Missing blocking reason, exit 1 and timeout each produced the selected Bash marker. Both timed helpers were independently observed exiting. The combined stdout/stderr log SHA-256 values in table order were `ad0d8f1a6131113ea14554a20e64ca49d3606c38cce3250070d2b3f0b8edabb4` and `47b0d684a92815249a2bf835ea99e051e037b88946c3947cb12ff11fc03efa50`.

The selected Linux profile still permits the tested Bash and direct patch effects when the managed hook times out. Required Policy activation still depends on a qualified decision boundary that blocks both tool paths before the effect. These cases establish no complete `policy_coverage` declaration. [Required tool policy activation](../specs/codex-execution-profile.md#required-tool-policy-activation)
