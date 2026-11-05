# Codex selected startup and untrusted checkout evidence

[#787](https://github.com/realkarych/kora/issues/787) composes checked startup
over the existing app-server client and makes the ADR-0009 checkout controls
explicit in the launch recipe. The helper returns a checked thread id to the
existing turn controller. It creates no process, transport, filesystem view,
policy session or independent retry/lifetime owner.

## Pinned contract and reproduction

Codex CLI 0.166.0, upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 is `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`;
the Python 3.11.17 amd64 image digest is
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Use the [original native fixture prerequisites](codex-managed-hook-probe-2026-11-04.md)
and [launch construction](codex-launch-construction-2026-11-05.md). Uncached
builds fetch these pinned assets. Runtime containers are unprivileged with
no host mounts, external model calls or operator credentials.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-startup
```

The app recipe quotes one exact checkout path and sets its trust to
`untrusted`, with `project_doc_max_bytes = 0`. Pinned
[thread startup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/thread_processor.rs#L1448-L1523)
can otherwise mark a writable explicit cwd trusted before acknowledgement.
The pinned loader may read or parse disabled project files; the probe checks
that the planted configuration and instructions have no effect.

The startup helper checks pinned initialize metadata and unknown built-in
executor before sending the raw bearer in one dynamic registration. It
verifies the add acknowledgement, exact lexical Bash spelling, decoded
checkout URI and ready status, then checks one ephemeral thread's ordered
selection, cwd, runtime workspace roots, model, provider and approval state.
The experimental
[thread fields](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)
are not all in the published non-experimental JSON schema. The returned
id has the pinned UUIDv7 shape and cannot be a bearer. Empty turns is a
shape check; actual wire tests confirm startup never sends a turn. All
calls and final handoff use the original absolute deadline. Native add/info
can connect or recover internally; no same-peer inference follows.

## Linux observations

Observed on 2026-11-06 (Europe/Moscow), Linux amd64 with Python 3.11.17
under Docker Desktop 29.9.2 / Buildx 0.38.1 in an aarch64 LinuxKit 7.0.14 VM.
The case, cleanup and host attach limits remain 30, 5 and 75 seconds.

| Case | Observed behavior |
| --- | --- |
| Selected startup and turn | The native replies corroborated the selected Bash, provider/model and one workspace with a space, colon and at sign in its path. The checkout has a Git root marker and conflicting `.codex/config.toml` and `AGENTS.md`. The two local Responses requests used the trusted model and omitted the instruction canary. The driver supplied no model override to thread startup. A managed Bash hook saw the fixed command once; the allowed command wrote the expected selected role marker with the key unset. The app trust entry and zero instruction budget were unchanged after cleanup. |
| Wrong executor bearer | Only the submitted bearer differed from the digest-bound token. Initialization and add acknowledgement passed, and `environment/info` returned a refusal mapped to the fixed selected-startup reason. No accepted Responses connection, hook receipt or marker was observed. |
| Mismatched expected Bash | The real executor reported Bash with a different path from the deliberate wrong expectation. The helper returned the distinct fixed Bash refusal, before any accepted Responses connection, hook receipt or marker. |

Each case reaped both direct native processes before closing the Responses
listener and confirmed the outer container removal. The test verifies
specific fixture resources, not independent exec listener/session or
descendant closure. The probe diagnostics report fixed case and refusal reasons.
No native-wide log/file nondisclosure claim is made.

The final executable is `6bd7f51bef812077657b13662cbde515d4540a86`,
provider tree `9a737bff06d64223e955ae33fbb7f52de99f73ca` and fixture tree
`de72b3eb0ea93d2bb14f919ac7ab0aff5852023a`. The final selected-startup
command exited zero in 13.487s. The fixed diagnostic log SHA-256
`8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22`
matches the earlier log's content; these are separate executions on changed
source. A final Docker inventory was empty.

The final executable also passed the previous three-case selected-launch
and app-server startup modes, each with three removals (22.198s and 6.596s).
Their fixed logs have SHA-256
`ce30b94206d009096942e0d3adc152f356c7a59d290a45fa6500986d8d87a9aa` and
`a245bc9da04e05f0e22849c2558cb7f8a2df62efed888d334a7c5e9783569cf4`.
The original six-case selected-executor shell mode exited zero at
`0c6ac6e` with all six removals, including the stalled-hook case, in 69.22s.
Log SHA-256 `b2dc9c3d7cf27839e627acdf919270f602d3611baa1f8f3435239eb919a0f2f4`.
The repair leaves that legacy path's initializer default unchanged. The
earlier results retain their original revision and scope.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-startup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-executor-shell
```

## Focused checks

At executable `0c6ac6e3f6a3ab623aa1a55ccee547e245e4d67b`, the initial
selected-startup mode passed three cases and three removals (18.683s).
The initial focused Codex and architecture suite passed 1,551 tests
(621 Codex and 930 architecture), zero failure/error/skip, 41.65s wall.
JUnit SHA-256:
`cd967d2352942d4e73de69b71d9691142e136c80af80cad3175ed644004b9209`.
Native diagnostic SHA-256:
`8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22`.

The final executable revision `6bd7f51bef812077657b13662cbde515d4540a86`
adds the same premature-completion guard at initialize and rejects omitted
ancestry fields, with new wire regressions for those edges and inconsistent
checkout/selection. The focused suite passed 107 tests, zero
failure/error/skip in 0.45s wall. JUnit SHA-256:
`ded259abb2fc41012186d204196b86361e3b99a8105a847f72d0d928a946b418`.
Both commands exited zero under Python 3.11.17 on macOS 27.2 arm64, with the
locked uv 0.13.1 environment. The startup wire tests use actual socketpairs
and synthetic credentials. No tests call external model providers. Strict Pyright, scoped Ruff and shell syntax
checks passed. Artifact/source association is coordinator-recorded.

```sh
uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  tests/plugins/harness/codex tests/architecture --junitxml=/tmp/kora-787-focused.xml
uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  tests/plugins/harness/codex/test_selected_startup.py \
  tests/plugins/harness/codex/test_app_server_startup.py \
  tests/plugins/harness/codex/test_launch_configuration.py \
  --junitxml=/tmp/kora-787-final-focused.xml
```

## Limits and ownership

The valid native credential observation and the negative
[info-stage refusal](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/cli/tests/exec_server_websocket_auth.rs#L320-L345)
do not attest to actual process identity, post-start reconnect, stale or
cross-attempt authority, secret isolation, sandbox or physical filesystem
binding. The owner must fence the real endpoint and retire partial failed
startup, even if the RPC remains usable. [#199](https://github.com/realkarych/kora/issues/199)
owns those guarantees and full resource release. [#549](https://github.com/realkarych/kora/issues/549)
owns configured selection, fact projection and consuming validation.
Complete native facts remain unavailable.
Native macOS qualification and operational adapter integration remain pending.
