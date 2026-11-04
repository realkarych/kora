# Codex app-server turn observation evidence, 2026-11-05

[Issue #728](https://github.com/realkarych/kora/issues/728) adds a private
single-turn observation over the [bounded stdio client](codex-app-server-rpc-2026-11-04.md)
and [startup check](codex-app-server-startup-2026-11-04.md). The caller supplies
an initialized connection and owns an idle thread exclusively for the
observation. The native `turn/start` method can steer an active turn and
return the existing ID. The fixture creates a fresh thread before using
the observer.
[Pinned start implementation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/turn_processor.rs#L639-L716).

The observer sends one text item. The start reply supplies the turn ID
with `inProgress`. The terminal observation must carry the selected
thread ID and the reply's turn ID. The terminal status is `completed`,
`failed` or `interrupted`. A terminal can be read while awaiting the reply.
The observer holds one such observation until it can validate the start
reply and match the IDs. Other notifications flow to the caller in wire
order. The private result excludes turn items and retains an optional
bounded error object for in-memory inspection. Its representation and
failure reasons exclude peer data.
[Pinned start reply](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnStartResponse.json),
[pinned terminal notification](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnCompletedNotification.json).

The observer refuses a terminal encountered during the RPC read-ahead
drain before sending the request. That check applies to bytes the
transport has already read, including the continuation of a partial
frame. The underlying socket and reader buffers are outside that check.
The fixture also refuses a terminal observed during pre-turn setup
requests. Duplicate refusal applies to terminals observed before the
helper returns. The borrower discards the connection after a failed
observation and retains responsibility for transport and process release.

## Inputs

The executable revision was `ee8383cd21e23fc552aca2ea1ea94f4b885999d9` from
master `1bd9ed9c56cfb6da47492fcf3953ff3335623612`. The provider tree at
`src/kora/plugins/harness/codex` was `c5aefddb36095a3e1c5d0fc33f1f7b9d9845e64e`.
The fixture tree at `tests/plugins/harness/codex` was
`d9f75c4e1dede155851e48673e7efe95f9d71424`.

The fixture used Codex CLI 0.166.0 from upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran the Linux amd64 image under
emulation on LinuxKit 7.0.14 arm64. The containers ran unprivileged with
`--network none`. The RPC mode used credential-free loopback Responses.
The provider case used its existing synthetic credential controls.

## Native cases

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-rpc
sh tests/plugins/harness/codex/run_hook_probe.sh --check-provider-env-key
```

| Case | Observation |
| --- | --- |
| Completed | The real app-server acknowledged the start and the observer correlated a completed terminal on the fresh ephemeral thread. The fixture accepted one local Responses request. |
| App-server exit | The fixture waited for both the checked start reply and the first admitted local model request, then killed and reaped the exact child it created. The observer reported disconnection before any accepted terminal. |
| Native failure | With the configured provider key omitted, the observer correlated a failed terminal. The fixture verified the exact missing-key reason and zero loopback connections or requests. The synthetic-key positive control completed with the expected loopback request. |

The fixture retained the 30-second observation and five-second cleanup
budgets. It closed stdio, reaped the app-server and settled the loopback
server. The existing stall checks retained their own child and cleanup
assertions. The wrapper verified every container removal.

All nine fixture commands exited zero on the recorded source revision.
The final Docker inventory contained no probe containers. Wall times
include Docker setup and cleanup and are diagnostic.

| Argument to `run_hook_probe.sh` | Wall seconds |
| --- | ---: |
| `--check-app-server-rpc` | 5.50 |
| `--check-provider-env-key` | 8.09 |
| No argument | 7.85 |
| `--check-cleanup` | 30.98 |
| `--check-write-stdin` | 13.59 |
| `--check-shell-controls` | 12.58 |
| `--check-executor-shell` | 68.07 |
| `--check-app-server-startup` | 6.86 |
| `--check-pre-tool-use` | 20.10 |

## Wire and static checks

On macOS arm64 with Python 3.11.17, 163 focused tests passed. The new
socketpair regressions check exact input, matching early and late
terminals, all three terminal statuses, wrong or malformed identities,
invalid start and terminal fields, duplicate observed completion, zero
turn request bytes on premature completion, pre-turn setup refusal,
explicit observer refusal, deadline, message budget, cancellation and
disconnection. The malformed
input cases check fixed failure output with a private-data canary. Wire
tests exercise `interrupted` as a received native status.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
git diff --check origin/master...HEAD
```

## Remaining ownership

The correlated terminal is a native observation. Event and usage
normalization, provider error classification, native interrupt and
escalation evidence and installed-adapter conformance remain work item 15.
[Runner hosting #199](https://github.com/realkarych/kora/issues/199) owns
protected process and session lifetime. [Selected facts #549](https://github.com/realkarych/kora/issues/549)
and [canonical start #593](https://github.com/realkarych/kora/issues/593)
retain their existing roles.
