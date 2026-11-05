# Native Codex structured-output request and strict candidate projection

Checked on 2026-11-05 for [issue #752](https://github.com/realkarych/kora/issues/752).
This is a private output boundary. It does not activate a registered adapter,
public events, artifact publication or complete saved-contract validation.

## Source and selection

Executable source was `bb73ca61ca53e0ddaa6013b15626cc4293d485fa`, based on master
`29a3050e1c4eed64fe69e9995afa5b6a743e253b`. The provider tree at
`src/kora/plugins/harness/codex` was `5f05c10924708293aa46295bdc3f969c97e57da8`;
the fixture tree at `tests/plugins/harness/codex` was
`3e4cebd4482f300b0fe10ad5ac090cd7560e59c2`.
Production scope is 100 additions plus two deletions across the RPC, turn and
new output module; tests and research are excluded.

The optional schema is omitted from `turn/start` when absent. When supplied,
including an empty object, the existing RPC encoder/decoder makes a detached
JSON snapshot under that client's actual frame/depth/scalar limits before the
first await. Pre-send callbacks can mutate the caller's original object without
changing the sent schema. Rejected snapshots send no turn bytes and leave RPC
ownership untouched. The final request still undergoes ordinary whole-frame
admission. None of this evaluates the schema or resolves its references.

The private turn completion retains at most one assistant message from the
successful terminal's explicit `itemsView: summary` form. That recognized form
must contain exactly one valid `agentMessage`, using the existing identity and
one-MiB text bounds. Missing, not-loaded, full and unknown views supply no inferred
selection; malformed recognized summaries refuse. Failed/interrupted terminals
retain no summary message. The original selected-thread and acknowledged-turn
correlation still governs return, including a terminal received before the start
reply. A summary does not count another streamed completed message.

Selection follows the app-server's terminal summary, rather than a local
last-message or phase heuristic. A completed individual item, including one with
a final-answer phase, is not a terminal result. The three-argument private
`TurnCompletion` constructor remains supported with an absent message.

## Strict host projection and limits

The host-side function accepts that completion and the selected thread. A
non-success terminal or absent summary returns no candidate. Message thread and
turn must match before parsing. Raw character admission precedes UTF-8 encoding;
the existing one-MiB raw allowance bounds direct inputs too. The function reuses
`kora.ir.structured_json.decode_json` and the existing `OutputCandidate` model,
including strict single-value JSON, unique members, finite numbers, scalar
Unicode, depth/integer and canonical-byte bounds. No parser is reimplemented.

A produced JSON null is a present `OutputCandidate` whose value is null.
Malformed JSON is a fixed refusal, not missing data or a prose-extraction
fallback. Candidates use `source: result_text`; their values are detached.
Decoder, Unicode and model failures expose only fixed reasons with hidden causes.
Candidate values remain content and must not be rendered into diagnostics.

Temporary schema, text, decoded and candidate representations are proportional
to the existing admitted input/output limits, plus fixed metadata and ordinary
object overhead. This is not a one-MiB process-memory or latency guarantee. No
production queue, task, process or connection owner was added.

Parsing establishes the existing JSON value domain, not validity against an
inline/effective ArtifactType schema. Runner first cause can still suppress an
otherwise completed native result. Native completion is not a stopped receipt.
Full cause/output validity and publication authority remain with their owners.

Primary pinned sources:

- [Turn start and outputSchema](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnStartParams.json).
- [Turn completion and items views](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnCompletedNotification.json).
- [Summary emission](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1353-L1392)
  and [successful, failed and interrupted paths](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1540-L1608).

## Real Linux native boundary

The real binary was Codex CLI 0.166.0 at upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`; Linux amd64 musl archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Python 3.11.17 retained amd64 image manifest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran Linux amd64 emulation on LinuxKit
7.0.14 arm64, from macOS arm64. Existing unprivileged network-none containers
had fresh homes, no host mount or credentials, and a loopback scripted Responses
provider with zero configured request/stream retries. The initial build can
fetch the pinned image/archive; runtime made no external model call.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-output
```

All three fresh cases verified the exact JSON schema in the provider request's
`text.format`, one loopback request/connection, no hook and absent marker:

| Case | Observed result |
| --- | --- |
| `output-valid` | The native successful summary selected the same message and exact valid JSON text observed at item completion. |
| `output-invalid` | The native successful summary preserved duplicate-key JSON text unchanged despite the schema request. This demonstrates why request delivery does not establish trusted validation. |
| `output-kill` | An acknowledged start and matching fully completed item were observed while response completion was held. The exact app-server was killed/reaped with SIGKILL. RPC EOF arrived with no accepted terminal or terminal-selected text; fixture stream EOF was awaited. |

The native container continues to stage only dependency-free Codex modules. It
tests schema transport and summary projection, and intentionally does not run
the host core/Pydantic candidate function. Full-package real socketpair tests
exercise the strict projection through the same turn helper. These are separate
evidence layers, not complete native-to-core runner qualification.

| Launcher mode | Exit | Wall seconds, diagnostic |
| --- | ---: | ---: |
| `--check-app-server-output` | 0 | 15.59 |
| `--check-app-server-messages` | 0 | 9.64 |
| `--check-app-server-rpc` | 0 | 5.41 |
| `--check-app-server-interrupt` | 0 | 10.92 |
| `--check-app-server-errors` | 0 | 11.18 |
| `--check-app-server-usage` | 0 | 4.25 |

All fifteen exact containers were confirmed removed, and final probe inventory
was empty. Retained logs contain fixed case/barrier or closed native-error
fields without message/provider/output canaries. Existing 30-second case,
five-second cleanup and 75-second attach bounds were unchanged. Process/pipe,
fixture client/task/listener and private namespace cleanup owners are preserved.

## Local checks and remaining integration

On macOS arm64 / Python 3.11.17, 1,550 focused cases passed in 37.32 seconds,
including 64 new output cases and 216 existing StructuredOutput cases. Checks
cover schema omission/empty/exact snapshot and mutation, zero bytes plus usable
RPC after refusal, early/late summary correlation, controlled reads, malformed
summary shapes, all JSON roots and produced null, exact raw/canonical bounds,
strict JSON refusals, detached values and completed-item EOF without output.
Scoped Ruff format/lint, strict Pyright, shell syntax and whitespace passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture \
  tests/ir/test_structured_output.py tests/ir/test_structured_output_contract.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

[Runner hosting #199](https://github.com/realkarych/kora/issues/199) retains
first cause, full effective-schema/artifact validity, EventSink redaction,
authenticated policy/launch and result delivery. Native-to-core production
integration, public events, registration, native macOS compatibility and complete
conformance remain work item 15. [Selected facts #549](https://github.com/realkarych/kora/issues/549)
and [server execution #593](https://github.com/realkarych/kora/issues/593) gain no
selection or execution authority. Existing TM-03, TM-06/TM-07, TM-10/TM-11 and
TM-12 cover this private boundary; no public/persisted contract, dependency,
platform, threat-model authority or verification gate changed.

The latest successful master mutation audit remains partial pricing/CEL evidence
at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`; the later run at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before executing mutations.
Neither supplies an applicable Codex finding or runtime pass.
