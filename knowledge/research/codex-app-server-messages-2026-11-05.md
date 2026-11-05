# Completed native Codex assistant-message observations

Checked on 2026-11-05 for [issue #749](https://github.com/realkarych/kora/issues/749).
This delivers a private bounded projection, without selecting a public output
candidate or activating session events, redaction or a registered provider.

## Source and native contract

Executable source was `6dbb5874221395e22d2a4f857fee25a31046329b`, based on master
`f6891dd811cd67fb92a0ecee28dd302295aec8bf`. The provider tree at
`src/kora/plugins/harness/codex` was `8d6172018bf83493da710fedee68d83c2d6bf629`;
the fixture tree at `tests/plugins/harness/codex` was
`372e1481c4f86626b12a8776cf301574ff1767a6`.
Production scope is 148 added lines in `app_server_messages.py`.

The decoder projects only `item/completed` notifications whose item is
`agentMessage`: thread/turn/message identity, exact text and optional phase.
Started items, text deltas and unrelated item kinds are not completed messages.
A preceding start or delta is not required. Additive delivery, question,
memory-citation and timestamp fields are discarded. Empty and whitespace-only
completed text remain observed values rather than being filtered or promoted.

Known phases are `commentary`, `partial_answer` and `final_answer`. Missing/null
phase remains unknown. An unknown phase or malformed recognized field refuses;
no phase is inferred from text or terminal status. This projection does not
interpret a declared final phase as the selected result of the whole session.

Identities are nonempty scalar Unicode within 256 UTF-8 bytes. Text has an
explicit positive caller byte allowance. Character length is checked before
encoding, bounding temporary encoding work for direct inputs as well as framed
RPC data. Overflow refuses without truncation. These private checks do not
establish that a native identifier is a valid public ASCII protocol identifier.

The reducer keeps one latest completed value and a caller-bounded set of IDs.
Thread, provisional turn, duplicate ID and both capacities are checked before
mutation. Refusal preserves the current value and remaining ID capacity.
`accept()` returns provisional native observations; `for_turn()` checks the
eventually acknowledged turn before returning selected evidence. Replacing the
last value retains no old text history. There is no delta concatenation, queue,
timer, task or connection owner. Retained payload is
`O(ID allowance × 256 + text allowance)`, including fixed bounded identities,
with ordinary object overhead.

The existing RPC frame/depth/message/deadline bounds remain active. Pre-send
rejection covers relevant messages in already-read lookahead, including a
partial frame completed during that drain. It does not inspect unread kernel
data. A trailing completion needs an explicit owner read after the turn helper
returns. Neither a completed item nor a native terminal establishes output
validity, usage completeness, runner first cause or runtime closure.

Primary pinned sources:

- [Completed item and phase schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ItemCompletedNotification.json).
- [Started item schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ItemStartedNotification.json).
- [Agent-message delta schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/AgentMessageDeltaNotification.json).
- [Native item and terminal handling](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1075-L1138).

## Linux real-interface execution

The real binary was Codex CLI 0.166.0 at upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Python 3.11.17 retained amd64 image manifest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran Linux amd64 under emulation on
LinuxKit 7.0.14 arm64, from a macOS arm64 host. Existing unprivileged,
network-none containers had no host mounts or credentials, fresh homes and a
loopback scripted Responses provider with zero configured request/stream retries.
The initial build can fetch the pinned image/archive; runtime made no external
model call.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-messages
```

The command exited zero after three fresh containers, each with exactly one
Responses connection/request and no hook or marker:

| Case | Actual observation |
| --- | --- |
| `messages-plain` | The fixture supplied a legacy message without phase. Exactly one completed message preserved its full text and unknown phase; the matching turn completed. |
| `messages-stream` | Three streamed messages preserved exact Unicode text and, in order, commentary, partial-answer and final-answer phases. The last completed value and all three distinct identities were checked; the matching turn completed. |
| `messages-kill` | A selected-thread/item text delta and start acknowledgement were observed at an explicit barrier. No completed message existed. The exact app-server was killed and reaped with SIGKILL; RPC EOF arrived without a terminal, and no completed message appeared. Fixture stream EOF was awaited before generic cleanup. |

All affected neighboring native modes passed on the same executable source.
Wall time includes setup and cleanup and is diagnostic only.

| Launcher mode | Exit | Wall seconds |
| --- | ---: | ---: |
| `--check-app-server-messages` | 0 | 15.42 |
| `--check-app-server-rpc` | 0 | 5.76 |
| `--check-app-server-interrupt` | 0 | 11.18 |
| `--check-app-server-errors` | 0 | 11.29 |
| `--check-app-server-usage` | 0 | 4.34 |

All twelve exact containers were confirmed removed; final probe inventory was
empty. Retained logs contain fixed case/barrier or closed native-error fields,
with no message/provider canary. Existing 30-second case, five-second cleanup
and 75-second attach deadlines were unchanged. Existing process/pipe, fixture
client/task/listener and private namespace owners still close resources on
failure, timeout and cancellation. No protected-runtime claim follows.

## Focused checks and remaining integration

On macOS arm64 / Python 3.11.17, 1,270 Codex and architecture cases passed in
37.38 seconds, including 57 new parser/state/wire/reporting cases. They exercise
all phases and unknown absence, exact scalar/byte boundaries, completion without
start, partial/other-item exclusion, duplicate/identity/capacity nonreplacement,
prior-value release, fixed errors/reporting, early/late acknowledgement, zero
turn bytes on pre-send refusal, explicit trailing reads and partial delta plus
EOF without completion. Scoped Ruff format/lint, strict Pyright, shell syntax
and diff whitespace checks passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

[Runner hosting #199](https://github.com/realkarych/kora/issues/199) retains
EventSink redaction/truncation, enforcement, first cause, finalization and real
launch integration. Public message/event mapping, output selection and strict
JSON extraction/validation, native macOS compatibility, authenticated hooks and
installed-provider conformance remain work item 15 integration. Selected facts
[#549](https://github.com/realkarych/kora/issues/549) and server execution
[#593](https://github.com/realkarych/kora/issues/593) receive no new selection or
execution authority. Existing TM-03, TM-06/TM-07, TM-10/TM-11 and TM-12 cover this
private observation boundary. No dependency, platform, public/persisted format
or verification gate changed.

The latest successful master mutation audit remains partial pricing/CEL evidence
at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`; the later run at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before executing mutations.
Neither supplies an applicable Codex finding or runtime pass.
