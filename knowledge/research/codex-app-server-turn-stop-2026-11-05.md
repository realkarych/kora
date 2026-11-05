# Controlled interruption of a native Codex turn

Checked on 2026-11-05 for [issue #746](https://github.com/realkarych/kora/issues/746).
This implements private transport and turn observation, without registering a
provider or activating the runner host.

## Source and ownership

Executable source was `e17ed31dff57b53db1d1bec503014815c63f06c3`, based on master
`6e670a55c484b48021d919ac74c36ac995157992`. The provider tree at
`src/kora/plugins/harness/codex` was `23b437fce8a43b917ae3b227c9f769513d1bf61b`;
the fixture tree at `tests/plugins/harness/codex` was
`2882209c55be349c721393c7c8189d16ddb83a8d`.
Production scope is 100 added plus nine deleted lines across the existing RPC
and turn modules, excluding tests and this record.

The exclusive controlled receive owns at most one read task, one stop waiter
and one control request. It keeps a partially read frame in that same read task
while writing the request. A ready read is processed before a simultaneous stop;
an already observed terminal suppresses interruption. This does not claim to
inspect unread kernel data or suppress every request racing natural completion.
Existing frame, depth, message, request-ID and absolute-deadline bounds apply.

The trusted caller supplies an Event; peer notifications cannot request control.
`turn/interrupt` uses the selected thread and acknowledged turn ID. A stop before
the start acknowledgement waits for that identity, subject to the same deadline.
The internal start callback exposes only that checked ID, allowing the native
fixture to preserve its usage-to-turn check while observation runs concurrently.

After sending interruption, success requires both its matching successful
empty-object reply and the matching native terminal, in either order. A reply
alone does not end observation. The actual terminal status remains unchanged:
natural completion or failure racing stop is not rewritten to interrupted.
Wrong IDs, malformed/refused/duplicate replies, duplicate terminals and observer
refusal fail closed. Errors expose fixed reason codes, without native content.

Owned read and Event-wait tasks are cancelled and awaited before leaving the
operation, including cancellation, timeout and failure. The caller's Event is
neither cleared nor cancelled. No background reader or queue survives return.
A trailing notification requires the next owner's explicit read. A failed or
cancelled operation keeps the existing unusable-connection behavior.

Neither acknowledgement nor native terminal establishes runtime domain closure,
runner first cause or usage completeness. The caller retains pipes, process,
stop escalation and finite operation deadlines. Runner timers and the independent
kill path must not wait for a control acknowledgement before escalation.

Primary pinned sources are the [interrupt parameters](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnInterruptParams.json)
and the [app-server protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/README.md).
The [earlier native interruption record](codex-app-server-interrupt-2026-11-05.md)
describes the model/child barriers and distinguishes process evidence from intent.

## Linux execution

The binary was real Codex CLI 0.166.0 at upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`; Linux amd64 musl archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Python 3.11.17 retained amd64 image manifest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran Linux amd64 under emulation on
LinuxKit 7.0.14 arm64, from a macOS arm64 host. Each container used its existing
unprivileged user, private PID namespace, no host mount, `--network none`, fresh
test-owned homes and an allowlisted credential-free environment. The scripted
Responses server was loopback-only; no external model call occurred.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-interrupt
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-rpc
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-errors
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-usage
```

| Mode | Actual result | Wall seconds, diagnostic |
| --- | --- | ---: |
| Interrupt | Exit 0: model, command and exact app-server kill | 17.95 |
| RPC | Exit 0: completed turn and exact process exit | 5.63 |
| Errors | Exit 0: 401, 429 and 503 native failed turns | 11.36 |
| Usage | Exit 0: cumulative native snapshots and decoded replay | 4.38 |

All three interruption cases used the turn helper while one observation task
drained stdout. Model interruption waited for the held stream and then verified
its closure. Command interruption waited for the live child and nonzero primer
usage, kept the control socket open and verified child pidfd exit before generic
app-server cleanup. Both obtained the native interrupted terminal with the
app-server still live. The kill case killed and reaped the exact app-server,
observed RPC EOF without a terminal, and retained its known usage. Markers stayed
absent. Existing hook receipts and request-count checks passed.

All nine exact containers were confirmed removed, and final probe inventory was
empty. Logs contained only fixed cases/phases or closed native error codes, with
no test-content canary. Case 30-second, cleanup five-second and attach 75-second
deadlines were unchanged. Container teardown still covers descendants outside
the app-server group; this is not production protected-runtime evidence.

## Focused verification and remaining integration

On macOS arm64 / Python 3.11.17, 1,213 Codex and architecture tests passed in
37.72 seconds, including 34 new controlled-turn cases. They cover both reply and
terminal orders, actual terminal statuses, early stop, simultaneous ready
completion, a split UTF-8 frame, competing access, safe malformed/refused/mismatched
observations, finite transport budgets, deadline, cancellation, EOF/truncation,
trailing-message preservation and exact task settlement. Scoped Ruff format/lint,
strict Pyright, shell syntax and diff whitespace checks passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

Native macOS interruption, an unresponsive interrupt followed by production
escalation, authenticated policy hooks, public event/final-result mapping,
provider registration and installed-adapter conformance remain pending with
work item 15 and [runner hosting #199](https://github.com/realkarych/kora/issues/199).
The helper's finite transport deadline is not an implementation of D or D+30s.
[Selected facts #549](https://github.com/realkarych/kora/issues/549) and
[server execution #593](https://github.com/realkarych/kora/issues/593) receive no
new selection, execution authority or public contract. Existing TM-03,
TM-06/TM-07, TM-10 and TM-12 cover this private boundary without a threat-model
amendment. No dependency, platform, policy gate or public/persisted format changed.

The latest successful master mutation audit was partial pricing/CEL evidence at
`6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`. The later run at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before executing mutations.
Their summaries and reports supply no applicable Codex finding or runtime pass.
