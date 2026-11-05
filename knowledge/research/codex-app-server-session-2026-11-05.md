# Codex acknowledged turn-session observations

Checked on 2026-11-05 for issue #757, against kora base
`f9c223ee7986d2940f52a14d5a3e6c033c276d5d`. Executable implementation and
all local/native checks used `c690ce17a1ab227c9940e010b7dedd29d725bb66`.
The provider tree was `0023ef6ab7c4512da2a11b9c99fdde04903325de` and the
fixture tree was `37a1c9874429a9fa35a42e629ba0a8e88660bd93`. Adding this
record changes neither executable input. Production scope is 146 added lines;
tests and this record are excluded.

## Delivered boundary

`AppServerTurnSession` composes the existing turn, usage, error and completed
message primitives over a borrowed RPC connection. Its caller must supply an
initialized, exclusively driven connection and an already selected idle native
thread. It runs one turn only. The used flag is set before the first await;
concurrent, repeated and failed-run reuse refuse with `session_already_used`.
The caller retains the RPC streams, process, native thread lifecycle and cleanup.

A frozen, content-opaque `TurnObservations` snapshot starts empty. Usage, errors and completed agent messages in already-read RPC lookahead
refuse before `turn/start` is sent, including continuation of a retained partial
frame. Unread StreamReader/kernel data is outside that pre-send check and can be
received as provisional input after sending. Observations received while the
start reply is pending remain provisional.
Only after all reducers agree with the acknowledged turn does the controller
publish their combined snapshot. Later malformed or mismatched input cannot
replace the last confirmed state. Synchronous raw callbacks run after internal
admission but can still receive provisional notifications before acknowledgement;
their input is not confirmed snapshot evidence.

Snapshots retain current usage and its sticky regression gap, latest message and
bounded message count, and latest closed native error. A successful helper return
adds actual terminal status, its selected summary message and closed decoded
terminal-error details. Raw terminal error text is returned through the existing
completion value, not retained in the snapshot. A malformed terminal detail
refuses without publishing a terminal. EOF, cancellation, deadline, observer
refusal and protocol failure leave the last confirmed partial snapshot readable.
No terminal or output is invented, and natural completion racing stop preserves
its actual native status.

The existing helper/RPC implementation still owns the exclusive read, interrupt
exchange, deadlines and settlement of its read and stop-wait tasks. The controller
adds no queue, task, stream, process, retry or cleanup owner. Retention is a
caller-bounded set of message IDs, each at most 256 UTF-8 bytes, current scalar
usage/error values and a constant number of message texts. Completed-message and
terminal-summary texts retain their existing separate one-MiB admission bounds.
Rejected provisional state can coexist with a prior confirmed value. There is
no snapshot/notification history or per-observation ID-set copy; callers own
snapshots they deliberately retain. A weak-reference regression checks release
of replaced messages after callers drop old snapshots. These are owned-state
bounds, not a one-MiB RSS, general latency or leak-freedom guarantee.

## Local and real-wire evidence

On macOS arm64 with Python 3.11.17, all 46 new socketpair/state cases passed in
0.14 seconds. They cover early/late/early-terminal ordering, mixed observations,
acknowledgement binding, cross-stream identity and malformed-input refusal,
pre-send zero-byte refusal, sticky usage regression, closed terminal errors,
produced null from a summary without an item, failed schema one-shot admission,
concurrent/repeated use, message/text bounds and replacement release. Cancellation,
EOF, truncation, injected-clock expiry, callback exceptions and false observer
returns preserve confirmed partial messages/usage/errors and settle owned tasks.
Stop tests cover both reply/terminal orders and all three native terminal statuses.

The complete focused run passed 1,596 cases in 41.64 seconds: 450 Codex,
930 architecture and 216 existing StructuredOutput cases, with zero failures,
errors or skips. Scoped Ruff format/lint, strict Pyright, shell syntax and
complete diff whitespace passed. Initial authoring checks caught import/format
and test-helper signature issues; these were corrected without weakening checks.
JUnit does not embed Git/command identity; its association is coordinator-recorded.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture \
  tests/ir/test_structured_output.py tests/ir/test_structured_output_contract.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

## Native Linux execution

Real Codex CLI 0.166.0 remains pinned to upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, with Linux amd64 musl archive
SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 amd64 image manifest remains
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker 29.9.2 / Buildx 0.38.1 ran Linux amd64 under emulation on LinuxKit
7.0.14 arm64 from a macOS arm64 host. Containers used their unprivileged user,
private PID namespace, no host mounts, `--network none`, fresh homes and an
allowlisted credential-free environment. Responses came only from the owned
loopback fixture. No external model call occurred.

Run each mode with
`sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-MODE`:

| Mode | Cases | Exit | Wall seconds, diagnostic |
| --- | ---: | ---: | ---: |
| `usage` | 1 | 0 | 10.82 |
| `errors` | 3 | 0 | 11.41 |
| `messages` | 3 | 0 | 9.31 |
| `output` | 3 | 0 | 9.32 |
| `interrupt` | 3 | 0 | 11.09 |
| `rpc` | 2 | 0 | 5.43 |

Thirteen cases exercised the controller, with the two RPC cases as neighboring
transport regressions. Eight ordinary terminal checks compared combined state
with the independent existing fixture reducers and native completion. Two kill
cases retained the acknowledged partial message state without a terminal:
text delta alone was not adopted, while a completed item survived without
becoming final output. All three interruption cases checked combined usage and
status, including known usage preserved after exact app-server SIGKILL and EOF.
Existing child pidfd, held-stream, hook receipt and absent-marker assertions
remain active and precede generic cleanup where required.

All fifteen cases recorded exact container removal; final inventory was empty.
Logs contain fixed cases and closed native error codes, without content canaries.
The 30-second case, five-second cleanup and 75-second attach bounds are unchanged.
Retained command metadata records zero exits, elapsed times and source/tree
identity; source/runtime/command association is coordinator-recorded rather than
embedded in each native log. The stdlib-only native image runs the controller
and native schema/summary path. Full-package wire tests separately run existing
core/Pydantic output projection; this is not production native-to-core delivery.

## Independent source review

Three fresh read-only single-profile assignments inspected the design and the
complete five-file executable diff from the stated base through `c690ce17`,
including governing contracts, controller/reducer/caller boundaries, all 46 new
wire cases and native staging/assertions. Correctness, security and performance
source reviews passed without findings. Each inspected the actual local JUnit,
all six native logs, command/source-tree metadata and empty final inventory;
those inputs matched the reviewed executable trees. No reviewer edited,
installed, executed candidate code, published or delegated.

These conclusions cover correlation/partial-state integrity and simplicity,
untrusted content and safe refusal, bounded retained state and inherited task
lifetime. They do not establish complete final-head supported-platform CI.
The issue and implementing PR retain reviewer provenance, evidence-assertion
review, final CI and complete input reconciliation before merge.

## Remaining activation

[Runner hosting #199](https://github.com/realkarych/kora/issues/199) retains
first cause, enforcement, independent escalation, EventSink redaction,
authenticated policy/launch, full effective-schema/artifact validity and
finalisation. Output projection remains an explicit existing caller operation.
Native completion is not a stopped receipt, and a false usage-gap flag is not
complete usage or zero spend. Public event mapping, full adapter registration,
resume, installed conformance, production integration and native macOS
compatibility remain work item 15. [Selected facts #549](https://github.com/realkarych/kora/issues/549)
and [server execution #593](https://github.com/realkarych/kora/issues/593) gain no
selection or execution authority.

Existing TM-03, TM-06/TM-07, TM-10/TM-11 and TM-12 cover this private composition.
Libraries propagate fixed typed refusals, snapshots have opaque representations,
and the existing probe owns fixed structured diagnostics. No public/persisted
contract, dependency, platform, threat authority or verification gate changed.
The latest successful master mutation report at
`6b6d2bcbba8372114cb4f2429cc8c1102fee0be1` is partial pricing/CEL evidence:
113 executed, no reused results and remaining pending work. The later run at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before mutation execution.
Neither provides an applicable Codex finding or runtime pass.
