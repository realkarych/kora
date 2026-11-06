# Confirmed Codex assistant message events

Measured on 2026-11-06 for [#863](https://github.com/realkarych/kora/issues/863). The executable inputs match revision `02961d4ecb729ab83843c8c1e9d9f50344617602`. The Codex source tree is `a89970c77cfbd52c5674e3cbe87e0ffa0a164cbf`. The fixture tree is `94f5fd3d0b37852b225a094d57c47cef9b2f5edb`. The two modified native inputs were checked against their recorded SHA-256 digests after the run.

## Native handoff

The existing fixture uses the official Codex 0.166.0 Linux amd64 musl archive, pinned to SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`, with CPython 3.11.17. The run used LinuxKit 7.0.14 and Docker Engine 29.9.2 on arm64, running Linux amd64 containers under emulation. The container has no external network. The fixture supplies deterministic local Responses API replies and uses the existing isolated environment. It checks the native version on build.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-messages
```

| Native case | Confirmed handoff | Final observation |
| --- | ---: | --- |
| Plain assistant message | 1 | Matching acknowledged turn, exact text and absent phase |
| Streamed assistant messages | 3 | Matching acknowledged turn and exact commentary, partial answer and final answer, in order |
| SIGKILL after a real assistant delta | 0 | No terminal or promoted partial message. App-server exited with SIGKILL and the local response stream closed. |

The command exited zero in 10.593 seconds, including three exact container removal receipts. The final probe inventory was empty. The existing 75-second per-container attach deadline and the independent removal path were unchanged. A development run before the `CompletedMessage` fixture import was added failed at Python startup and removed its container. The corrected run used the source hashes recorded above.

## Shared event boundary

The standalone native container exercises the confirmed callback. Full-package loopback tests pass the callback into the shared event validator and counter. They cover ordered earlier items, an empty item, an item arriving after acknowledgement, suppression of a delta and terminal summary, fixed refusal, retained prefix on disconnect, and a deadline reached during deferred delivery with an early native terminal. The optional staging admits exactly 1,048,576 UTF-8 identity and text bytes and refuses the next byte. The message count remains an independent bound.

The event payload contains an attempt-local ordinal and the masked text. The tests cover exact canonical size, multibyte and escaped text at overflow, and a registered value split across a masking piece or the clipping boundary. Each mask step receives at most 8 KiB of input plus the shared masker's held overlap. The projector retains masked pieces through the first canonical overflow, includes the marker in the final budget, and checks the inherited deadline before emission. Fixed failure reasons contain no native IDs or text.

This evidence covers event admission and counting by the shared test sink. The common production sink still owns final journal masking, durable replay, enforcement and shutdown under [#199](https://github.com/realkarych/kora/issues/199). Production registration remains part of [#772](https://github.com/realkarych/kora/issues/772). Native macOS behavior and required policy activation are outside these observations.
