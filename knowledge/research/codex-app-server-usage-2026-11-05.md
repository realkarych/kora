# Codex app-server cumulative token snapshots, 2026-11-05

[Issue #730](https://github.com/realkarych/kora/issues/730) adds private
decoding and reduction of cumulative native token snapshots for one
fresh thread and one correlated turn. The [turn observer](codex-app-server-turn-2026-11-05.md)
supplies the native correlation. The [usage contract](../specs/protocols.md#codex-usage-contract-issue-142)
defines the separate attribution, completeness and accounting obligations.

## Wire mapping and scope

The pinned notification carries `threadId`, `turnId` and the
`tokenUsage.total` and `tokenUsage.last` counts. The reducer consumes only the cumulative
breakdown. It validates nonnegative signed-64 native counts and requires
`totalTokens = inputTokens + outputTokens`. Cache reads and writes are
subsets of input. Reasoning is a subset of output. The private projection
preserves those dimensions separately and subtracts both cache buckets
from input when both are known.
[Pinned notification schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ThreadTokenUsageUpdatedNotification.json).

An omitted wire cache-write field leaves that field and derived uncached
input unknown. An explicit wire zero is a present value. The pinned
Responses bridge can default missing upstream detail to zero before the
app-server sends a notification. Wire presence alone establishes only what
the peer reported.
[Pinned Responses bridge](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/codex-api/src/sse/responses.rs#L121-L145).

The known native context-window fill sets a nonzero total with empty
component counts. That shape fails aggregate equality. Coherent native
counts remain observations and require separate provenance before
billable attribution.
[Pinned accumulation and fill behavior](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/protocol.rs#L2518-L2556).

The reducer holds one coherent snapshot, never an observation history.
Replay leaves the latest snapshot unchanged. A regression preserves that
snapshot and sets a gap, which survives later advances. The comparison
checks cumulative exclusive buckets as well as the inclusive values.
For a lost cache-write dimension it preserves the last coherent snapshot.
The first observed turn is provisional until the caller presents the
matching correlated terminal identity. The usage observer refuses
notifications routed by the existing read-ahead drain before the send.
The drain classifies data already read by the transport.

## Native inputs

The executable revision was `ed57f3f996eee1020f743d0e1e4b9573c15976bb` from
master `e875f5c7675427d280f8c293ef6797e3afba40b0`. The Codex provider tree
was `9a9c14791105d9e474a97b0a49e16433f61607fa` and the fixture tree was
`d36d41db9430c9159b5a1630e8e0c540cc846b21`.

The fixture used Codex CLI 0.166.0 from upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran Linux amd64 under emulation on
LinuxKit 7.0.14 arm64. The unprivileged container used `--network none`
with a credential-free loopback Responses server, a fresh home and an
ephemeral thread.

## Native observation

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-usage
```

The first response requested the existing managed Bash marker command.
The second followed the correlated tool result. The numbers below are
input including caches, cache reads, cache writes, output including
reasoning, and reasoning respectively.

| Response | Synthetic per-response counts | Observed cumulative counts |
| --- | --- | --- |
| First | 100, 20, 30, 10, 4 | 100, 20, 30, 10, 4 |
| Second | 60, 5, 10, 20, 6 | 160, 25, 40, 30, 10 |

The fixture checked both full snapshots and the matching terminal turn
ID. It replayed the second decoded snapshot in memory and verified
that the final uncached input remained 95 and all cumulative dimensions
stayed unchanged. The managed hook receipt and final regular marker
were present. The existing deadlines bounded observation and cleanup.
The probe closed stdio, reaped its children, settled the server and
confirmed container removal.

All ten Linux modes exited zero on the recorded revision. The final
Docker inventory contained no probe containers. Wall times include
Docker setup and cleanup and are diagnostic.

| Argument to `run_hook_probe.sh` | Wall seconds |
| --- | ---: |
| `--check-app-server-usage` | 4.76 |
| No argument | 7.79 |
| `--check-cleanup` | 30.99 |
| `--check-write-stdin` | 13.46 |
| `--check-provider-env-key` | 7.99 |
| `--check-shell-controls` | 12.83 |
| `--check-executor-shell` | 68.70 |
| `--check-app-server-rpc` | 5.50 |
| `--check-app-server-startup` | 6.31 |
| `--check-pre-tool-use` | 19.92 |

## Focused checks and remaining scope

188 focused tests passed on macOS arm64 with Python 3.11.17, including
25 new cases. These cover strict counts and identity, missing versus
zero, exclusive-bucket regression, retained gap, synthetic fill,
same-snapshot replay and fixed diagnostics. Socketpair tests cover
zero request bytes after pre-send refusal, genuine usage after send
and before the reply, and a provisional usage ID that does not match
the terminal. Scoped Ruff format and lint, strict Pyright, shell syntax
and whitespace checks passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
git diff --check origin/master...HEAD
```

The record establishes native counts from two controlled model responses
on Linux. The private snapshot has no model, monetary value, or completeness
claim. Attribution, final coverage, normalized public usage bounds and
resume deltas remain work item 15. Runner hosting and projection belong to
[#199](https://github.com/realkarych/kora/issues/199). Selected facts and
canonical server start retain their existing owners in
[#549](https://github.com/realkarych/kora/issues/549) and
[#593](https://github.com/realkarych/kora/issues/593).
