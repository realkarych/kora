# Native Codex error decoding and failed-turn evidence

Checked on 2026-11-05 for [issue #740](https://github.com/realkarych/kora/issues/740).
This delivers a private decoder and selected-turn observation owner. It does not
activate public events, final error classes, quota handling or retry scheduling.

## Source and native contract

Executable source was `1ff50ac114b36b403f313d0a88d9187e57af0409`, based on
master `04288007f1a763eadb2bec4f08e560d23abe7f51`. The provider tree at
`src/kora/plugins/harness/codex` was `f3aab494de591cefbe8e760ffbd6f05c56b695ad`;
the fixture tree at `tests/plugins/harness/codex` was
`4165b899bad78693460585784b4f916dbd72d7ae`.
Production scope is 184 added lines in `app_server_errors.py`.

The decoder consumes `error` notifications with thread/turn identity, strict
`willRetry` and native `TurnError`, plus the same terminal error detail. The
pinned schema has 17 unit codes, four HTTP payload variants and the
`activeTurnNotSteerable` review/compact payload. HTTP status is optional and
nullable, with a native unsigned 16-bit domain; zero is observed zero rather
than missing. A future public projection must still enforce its own HTTP field
contract. Native messages, additional details, misalignment payloads and unknown
code text/payloads are not retained by the typed projection.

Missing/null code and an unrecognized future code have separate values. Known
codes in the wrong representation and malformed recognized payloads refuse with
fixed exceptions. Unknown future single-tag object payloads are discarded. The
single-turn reducer holds one latest immutable observation and checks its selected
thread and provisional turn before replacement. `accept()` returns provisional
observations immediately; `for_turn()` checks the eventually acknowledged turn
before returning selected evidence. It creates no history, queue, task or timer.
An internal-retry observation alone establishes no terminal outcome.

The existing RPC owner supplies frame, depth, message and deadline admission.
Pre-send refusal covers notifications in its already-read lookahead, including a
partial frame completed during the drain; it does not inspect unread kernel or
reader buffers. Notifications after the turn observer returns require an explicit
read by the borrowing owner. Neither helper takes process or connection ownership.

Primary pinned sources:

- [Error notification and TurnError schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ErrorNotification.json).
- [Native error-code translation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/error.rs#L499-L569).
- [App-server error and stream-error notifications](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1037-L1072).

## Linux native execution

The real binary was Codex CLI 0.166.0 at upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 image retained amd64 manifest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 and Buildx 0.38.1 executed Linux amd64 containers under
emulation on LinuxKit 7.0.14 arm64, from a macOS arm64 host. Containers used the
existing unprivileged user, `--network none`, fresh test-owned homes and an
allowlisted credential-free environment. The local Responses provider had zero
configured request and stream retries. There were no external model calls.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-errors
```

The command exited zero after three fresh containers. Each made exactly one
loopback Responses request and connection. The provider returned bounded JSON
with the scripted HTTP status and a nonsecret content canary. Each native turn
produced exactly one nonretrying error notice and a correlated failed terminal;
the decoded notice and terminal details agreed. No hook ran or marker appeared.

| Local HTTP response | Native code in notice and terminal | Preserved HTTP status |
| --- | --- | ---: |
| 401 | `httpConnectionFailed` | 401 |
| 429 | `responseTooManyFailedAttempts` | 429 |
| 503 | `httpConnectionFailed` | 503 |

The native name for 429 is not evidence of multiple observed requests: the
fixture verified exactly one. These generic HTTP failures do not reproduce
native `unauthorized`, `usageLimitExceeded`, `rateLimitExceeded` or
`serverOverloaded` signals, account reset windows, model attribution or internal
retry behavior. Those code shapes and retry/success ordering have parser/wire
coverage. No HTTP number or provider message is promoted into a product class.

All four affected native commands passed on the same committed executable source.
Wall times include Docker setup and cleanup and are diagnostics only.

| Argument to `run_hook_probe.sh` | Observations | Wall seconds |
| --- | --- | ---: |
| `--check-app-server-errors` | Three failed native turns and exact removal | 21.63 |
| `--check-app-server-rpc` | Normal completion and exact app-server exit | 5.65 |
| `--check-app-server-usage` | Real cumulative counts and decoded replay | 4.29 |
| `--check-app-server-interrupt` | Open stream, active command and killed-turn cases | 11.08 |

Each container removal was confirmed. The final Docker inventory contained no
probe containers. Retained logs contain fixed case/phase or closed-enum results,
with no provider/test canary. The 30-second case, five-second cleanup and
75-second attach bounds were unchanged. Existing process, listener, pipe,
pidfd and namespace cleanup owners remain in charge on every exit path.

## Focused checks and limits

On macOS arm64 with Python 3.11.17, 279 focused tests passed in 8.15 seconds,
including 91 new parser/state/wire cases. The cases cover every pinned variant,
unknown/missing distinction, malformed known representation, strict Boolean and
unsigned-16-bit fields, detached input mutation, identity nonreplacement, canary
exclusion from representations/failure traces/diagnostics, early provisional
identity, pre-send zero request bytes, explicit late reads and retry followed by
successful completion. Scoped Ruff formatting/lint, strict Pyright, shell syntax
and complete-diff whitespace checks passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

[Runner hosting #199](https://github.com/realkarych/kora/issues/199) retains
first-cause/final-result classification, event emission, authenticated policy and
launch, usage projection and actual production stop ownership. Vendor reset/group
mapping, complete provider-limit observations, final-session projection and
installed-adapter conformance remain work item 15 integration. The existing
mapping table's historical `turn/failed` wording is not implemented or amended
here; these native turns use the already-delivered `turn/completed` failed status.

[Selected facts #549](https://github.com/realkarych/kora/issues/549) and
[server execution #593](https://github.com/realkarych/kora/issues/593) receive no
new contract, provider selection or execution activation. No public event,
provider registration, dependency, supported platform or verification gate changed.
The latest successful master mutation audit was partial and covered only pricing
and CEL paths at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`; it supplies no
applicable Codex findings or runtime evidence.
