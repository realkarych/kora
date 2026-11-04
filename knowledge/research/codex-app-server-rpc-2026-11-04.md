# Codex app-server stdio client evidence, 2026-11-04

[Issue #696](https://github.com/realkarych/kora/issues/696) implements the private
stdio connection layer for the Codex adapter. The client borrows streams from its
caller. The isolated fixture owns Codex, the model server and their cleanup.

## Inputs

The tested source revision was `e77c7ef1994d6981c3581a884f99396f44f1d475`,
based on `c7e8a89a92227cee7c9fb04f828b043fec0362cb`. The existing
[managed hook fixture](codex-managed-hook-probe-2026-11-04.md) pins Codex CLI
`0.166.0` from upstream commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`.
The Linux amd64 musl archive SHA-256 is
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The image stages the repository client in the isolated Python import path. It
runs unprivileged with Docker `--network none` and a fresh fixture home.
Docker Engine 29.9.2 and Buildx 0.38.1 ran the Linux amd64 image under emulation
on LinuxKit 7.0.14 arm64. The successful and interrupted client turns used
credential-free loopback Responses.

Codex uses newline-delimited JSON, generally without a version member. The first
native run showed that the app-server adds `emittedAtMs` to notifications. The
client preserves the optional signed timestamp outside `params`.
[Pinned request and reply shapes](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/rpc.rs)
and [pinned notification shape](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/common.rs#L2135-L2145).

## Real app-server results

From the repository root, use a local Docker Engine, Buildx and `timeout`, as
described by the managed hook fixture.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-rpc
```

- `client-complete` ran the real initialization sequence, opened an ephemeral
  thread, received one fixed local SSE response and observed a completed
  `turn/completed` with the started thread and turn IDs.
- `client-exit` received the turn-start reply and accepted one local Responses
  request while keeping its response pending. The fixture waited for the
  stdio observer, then sent SIGKILL to the exact live child it started.
  The client observed disconnection before any terminal event and the fixture
  observed the reaped child's `-SIGKILL` status.
- Both used the existing 30-second observation and five-second cleanup budgets.
  Cleanup awaited closure of stdin, drained stdout, settled the loopback server
  and its tasks, and reaped the child. The Docker wrapper confirmed container
  removal after each case.

All commands below exited zero. Times include Docker setup and cleanup and are
diagnostic observations.

| Argument to `run_hook_probe.sh` | Observed cases | Wall seconds |
| --- | --- | ---: |
| `--check-app-server-rpc` | Completed turn and disconnection | 7.63 |
| No argument | Managed hook allow and deny | 7.61 |
| `--check-cleanup` | Stalled managed hook | 30.96 |
| `--check-write-stdin` | Interactive control and one-shot allow/deny | 12.88 |
| `--check-provider-env-key` | Implicit account state and custom-provider key present/absent | 7.80 |
| `--check-shell-controls` | Default shell, login and alternate shell | 11.94 |
| `--check-executor-shell` | Selected executor shell, login, deny and stalled hook | 66.62 |

The final Docker container inventory returned no probe container.

## Decoder depth across Python versions

The [first CI run](https://github.com/realkarych/kora/actions/runs/41800544126)
failed one direct-wire test on Linux with Python 3.15.0. That interpreter
accepted a frame nested 2,001 containers deep, while the earlier test had
relied on the decoder's recursion failure. The transport now counts JSON
containers before decoding and refuses a depth above 64, including the outer
object. Brackets inside strings and escaped quotes do not count.

An isolated Linux amd64 run with CPython 3.15.0 confirmed the original
acceptance at depth 2,001. The revised source accepted depth 64 and rejected
depths 65 and 2,001 as `rpc_invalid_frame`. The wire tests retain the original
deep frame and exercise the exact boundary with structural characters inside
a string. The Docker base for this comparison was
`python:3.15.0-slim-bookworm` at digest
`sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d`.
The complete native Codex campaign above was repeated on the revised source.

## Local transport checks

The 40 direct-wire cases and 30 architecture cases passed on macOS arm64 with
Python 3.11.17. The direct-wire tests run through ordinary CI on Linux and macOS.
Scoped Ruff format and lint, strict Pyright, shell syntax and whitespace checks
passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex/test_app_server_rpc.py \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
git diff --check origin/master...HEAD
```

The tests cover frame and message boundaries, exact reply identity, incremental
notification order, zero-byte refusal of premature or over-budget exchanges,
cancellation, stalled reads and writes, and actual socket closure. The client
routes already-read input before sending the next request. The fixture sources
independent integer IDs and the client refuses reuse within the connection.
Peer error data and input that fails parsing or notification observation stay out
of the fixed diagnostic reasons. A trusted observer returns an explicit
refusal for invalid notifications. Its unexpected programming failures belong
to that observer's caller.

The 128 KiB and 256-message budgets are fixture settings. Native evidence
covers Linux amd64 and this protocol exchange. Production launcher protection,
configured facts, the full policy and resource matrices, usage normalization,
and session hosting remain work item 15 and [#199](https://github.com/realkarych/kora/issues/199)
integration work. [#549](https://github.com/realkarych/kora/issues/549) owns
selected facts and [#593](https://github.com/realkarych/kora/issues/593) owns
the saved contract and canonical start.
