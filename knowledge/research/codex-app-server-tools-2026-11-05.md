# Codex shell and file-edit item observations

Checked on 2026-11-05 for issue #762, from kora base
`7ad6eff12af7bfe92cf44edb5e0f4f24b36e7db4`. Final executable/local/native
checks used `07feef9126094c16e83f89e3f3cb3abcdde2c11f`, provider tree
`72237bb040b3d2893c685c4db0b4415c5a05522f` and fixture tree
`0a4aa4a7f7cd8abbb5bf41c9427953433616727e`. Production scope is 203
added/deleted lines across the new decoder/reducer and existing session module;
tests and this record are excluded. Adding this record changes no executable
input.

## Private observation contract

The stdlib projection admits `item/started` and `item/completed` for native
`commandExecution` and `fileChange` items. It retains checked thread/turn/item
identities, native kind, notification phase, native status, and optional closed
command source and nullable int32 exit code. Identities use the existing
256-UTF-8-byte scalar bound. Arguments, command text, cwd, paths, patches,
aggregated output and model context are not retained. Unowned item kinds and
deltas are ignored. This is a projection of owned fields, not validation of
all native item content or declaration of supported policy coverage.

The [pinned started schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ItemStartedNotification.json)
and [completed schema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/ItemCompletedNotification.json)
allow four statuses in either notification: `inProgress`, `completed`, `failed`
and `declined`. Phase and status therefore remain independent. A started item
with status completed remains open until an actual completion notification.
Missing source remains unknown despite the native schema's agent default.
The four supplied command sources remain observations, not hook guarantees.
A nullable exit code and completion notification establish no process closure.

`TurnTools` keeps at most the caller's explicit `max_tools` ID entries, each
holding only native kind and completion state. Separate constant-time counters
track item IDs, starts, completions, open started items and completions without
an observed start. Completed IDs remain admitted to prevent recounting. Existing
items can complete at capacity. Duplicates, later starts after completion,
changed kind, identity mismatch and cap overflow refuse before mutation.
The invariant is completions = starts - open + completions_without_start.

The one-shot session now requires tool-count admission and publishes frozen
counts/latest metadata only after all reducers agree with acknowledged identity.
Already-read RPC lookahead, including a retained partial frame, refuses tool
items before send; unread reader/kernel data remains outside that inherited
check. Raw callbacks before acknowledgement stay provisional. Invalid later
input preserves the previous confirmed snapshot; EOF, cancellation and stop
create no missing completion. Open items may remain when a turn terminates.

No map scans, map copies or observation history occur per notification. Current
and provisional immutable metadata can coexist within the admitted bound;
callers own old snapshots they retain. Reducer and session weak-reference tests
check release of replaced observations. Existing RPC framing still owns the
transient full native frame, including unretained content. No task, queue,
stream, process or cleanup owner was added. These are owned-state bounds, not
RSS, latency or general leak-freedom guarantees.

## Native discovery and final assertions

The planned discovery at `d4e61af64c120576d37a87638a93b8f86bcfdb32` ran
`--check-pre-tool-use`, exited zero in 26.91 seconds and observed five cases in
four containers. Its provider tree matched the final executable source; fixture
tree `8db65aef45c86be659e01323227f897c424f04c2` had discovery assertions.
The final fixture pins actual presence/absence and checks item IDs against the
fixed model call and controlled hook receipts. It does not invent a declined
native item for a denied hook.

| Controlled case | Actual native items, in wire order |
| --- | --- |
| Bash allow | commandExecution started/inProgress, source unifiedExecStartup, no exit; completed/completed, same source, exit 0 |
| Bash deny | No tool item |
| Direct patch allow | fileChange started/inProgress, then completed/completed; no command source/exit metadata |
| Direct patch deny | No tool item |
| Hook decoder refusal | No tool item |

Bash's unifiedExecStartup source coexists with an actual PreToolUse receipt;
source cannot decide interception coverage. Existing native model-result and
marker checks independently prove file creation on allow and absence after deny.
No native lifecycle item on deny supplies a public permission event: actual
hook decisions and their delivery retain their own owner.

The command-interruption fixture waits for the real child, known primer usage
and the acknowledged actual tool-start observation before stopping or killing.
Before kill, the prefix is two starts, one completion and one open item. After
exact app-server SIGKILL/reap and RPC EOF, that prefix remains unchanged. The
successful interruption observed two starts/two completions/no open items; the
model-only case observed none. Successful interruption permits either truthful
one- or two-completion prefix, because observation stops at the matched native
terminal/control result and does not consume hypothetical later item frames.
It never completes an item merely to clear the counter. The independent child
pidfd/held-stream and hook/marker checks remain active before generic cleanup.

## Final Linux execution

Real Codex CLI 0.166.0 remains pinned to upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, Linux amd64 musl archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 amd64 image manifest remains
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker 29.9.2 / Buildx 0.38.1 ran Linux amd64 under emulation on LinuxKit
7.0.14 arm64 from a macOS arm64 host. Each unprivileged container had a private
PID namespace, no host mounts, network none, fresh homes and an allowlisted
credential-free environment. Model responses came only from owned loopback.

Run `sh tests/plugins/harness/codex/run_hook_probe.sh ARGUMENT`:

| Argument | Cases | Containers removed | Exit | Wall seconds, diagnostic |
| --- | ---: | ---: | ---: | ---: |
| `--check-pre-tool-use` | 5 | 4 | 0 | 28.18 |
| `--check-app-server-interrupt` | 3 | 3 | 0 | 11.82 |
| `--check-app-server-usage` | 1 | 1 | 0 | 4.39 |
| `--check-app-server-errors` | 3 | 3 | 0 | 11.31 |
| `--check-app-server-messages` | 3 | 3 | 0 | 9.32 |
| `--check-app-server-output` | 3 | 3 | 0 | 9.28 |
| `--check-app-server-rpc` | 2 | 2 | 0 | 5.47 |

All twenty cases verified; nineteen exact container removals were recorded and
final inventory was empty. The default hook container runs both Bash cases.
Logs contain only fixed cases, closed scalar metadata and bounded counts,
without content canaries. Case 30-second, cleanup five-second and attach
75-second deadlines are unchanged. Full host checks overlapped this serial
native campaign; wall timings are diagnostics, not an uncontended benchmark.
Retained command metadata binds source/tree identities and zero exits;
source/runtime/command association is coordinator-recorded rather than embedded
in each native case log.

## Local checks and independent review

On macOS arm64 / Python 3.11.17, the focused run passed 1,694 cases in 43.12
seconds: 548 Codex, 930 architecture and 216 StructuredOutput cases, with zero
failures/errors/skips. The tool/session subset passed 144 cases, including 98
new parser/state/real-wire tool cases, in 0.23 seconds. Scoped Ruff format/lint,
strict Pyright, shell syntax and diff whitespace passed. Initial authoring
checks caught redundant casts, untyped test collections and a test fixture's
wrong field key; fixes preserved assertions and every gate. JUnit carries no
Git/command identity; its association is coordinator-recorded.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture \
  tests/ir/test_structured_output.py tests/ir/test_structured_output_contract.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

Three fresh independent read-only profiles reviewed design and the complete
seven-file executable diff through `07feef9`: correctness, security and
performance passed without findings. Each inspected actual local JUnit, all
seven native logs, command/tree identity and empty final inventory. Those trees
matched the reviewed revision. No reviewer executed candidate code, installed,
edited, published or delegated. These source/native conclusions do not establish
complete final-head CI; reviewer provenance, research-assertion review and final
supported-platform reconciliation belong to the implementing PR and issue.

## Remaining activation

The [confirmed completion handoff](codex-confirmed-tool-completions-2026-11-08.md)
delivers each admitted native result through the private session. The handoff
keeps the distinction between native observations and shared tool result events.

The stdlib native image exercises actual native metadata/session behavior.
Full-package socketpair tests separately exercise host protocol/output consumers;
no complete production native-to-core path or native macOS compatibility is
claimed. NormalisedEvent/content truncation, complete HarnessAdapter and
installed conformance remain work item 15. [Runner host #199](https://github.com/realkarych/kora/issues/199)
retains authenticated hook transport, ToolCallLedger enforcement, EventSink,
first cause, independent escalation, final output validity and finalisation.
[Selected facts #549](https://github.com/realkarych/kora/issues/549) and
[server execution #593](https://github.com/realkarych/kora/issues/593) gain no
coverage declaration, selection or execution authority. MCP/subagent/hosted and
other tools remain outside this projection.

Existing TM-03, TM-06/TM-07, TM-10/TM-11 and TM-12 cover the private observational
flow. Fixed refusals and opaque representations exclude content; the existing
probe owns closed structured diagnostics. No public/persisted model, dependency,
platform, authority, deadline or verification policy changed. Latest successful
master mutation evidence at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` is partial
pricing/CEL only; the later run at `3f26b587bc6697dd5ece88e4fda9580b439aa3eb`
failed before executing mutations. Neither supplies an applicable Codex finding.
