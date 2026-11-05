# Native Codex interruption and killed-turn observations

Checked on 2026-11-05 for [issue #735](https://github.com/realkarych/kora/issues/735).
This fixture evidence covers real native interruption and process exit.
Production cancellation, deadline scheduling, final classification and protected
execution-domain closure remain unimplemented by this change.

## Inputs and environment

The executable source was `060222924d15f6d92fc8258aba35f49f65ef59e3`, based on
master `4d5feac457164f5780614bc9e5292e2502fc8bf0`. The unchanged provider tree
at `src/kora/plugins/harness/codex` was
`9a9c14791105d9e474a97b0a49e16433f61607fa`; the fixture tree at
`tests/plugins/harness/codex` was `cbb1ea6de066aed0dd796590cc19132fecff3198`.

The real binary was Codex CLI 0.166.0, upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 image retained its pinned amd64 manifest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 and Buildx 0.38.1 executed Linux amd64 containers under
emulation on LinuxKit 7.0.14 arm64, from a macOS arm64 host. Containers were
unprivileged, used `--network none`, and received fresh test-owned homes and
credential-free loopback Responses configuration. The managed one-shot feature
overrode the user configuration, and no interactive `write_stdin` was advertised.

## Reproduction and observations

From the repository root with the existing local Docker prerequisites:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-interrupt
```

The command exited zero. Each case used a fresh container; each removal was
confirmed before the next case.

| Case | Observed result |
| --- | --- |
| `interrupt-model` | After the local server wrote SSE headers and `response.created`, the same native thread/turn accepted `turn/interrupt`. Both its empty reply and the matching `turn/completed` with status `interrupted` arrived. The server observed stream EOF before cleanup. The app-server remained alive, no managed hook ran and no usage snapshot was invented. |
| `interrupt-command` | The two fixed commands reached the managed hook in order. After the second child reached its private socket gate and the first native usage snapshot was observed, the child pidfd was still nonreadable. The interrupt reply and matching interrupted terminal arrived. The pidfd then became readable while the gate stayed connected and before generic app-server cleanup. The marker was absent and the app-server remained alive. |
| `interrupt-kill` | At the same live-child and known-usage barrier, the fixture killed its exact created app-server child. RPC reported disconnection, the child was reaped with SIGKILL and no terminal was accepted. The last coherent snapshot remained available and the marker was absent. Container removal covered the owned PID namespace, including any remaining descendants; app-server exit alone was not treated as domain closure. |

The command cases send a completed `:` primer through the real tool interface,
with its own fixed call ID and nonzero synthetic usage. The next local request
must contain its correlated successful tool result before the fixture returns the
blocked command, whose response adds zero synthetic usage. Both managed receipts
and exactly two Responses requests are checked. The model-stream case admits one
request and no hook receipt.

The blocked command executes the fixed installed Python child. That child sends
its PID over one private Unix socket and waits for an explicit release byte
before writing the marker. The fixture keeps that connection open through native
interruption and exact pidfd exit observation. EOF never releases the marker.
The pidfd supplies exit evidence only and is never used to signal a reported PID.
The tool timeout is 60 seconds, beyond the unchanged 30-second case deadline,
so its own timer cannot substitute for native interruption.

The retained cumulative snapshot has native input 100, cache reads 20, cache
writes 30, output 10 and reasoning 4, giving uncached input 50. Interruption and
kill preserve those observed counts without summing the overlapping native
`last` field or fabricating a final interval. No model attribution, price,
complete usage, public `SessionResult` or production partial-usage projection
is established by this fixture.

## Native ordering correction

The exploratory revision `18750cf7270c87777ca0d9daf36f5e568cc71b11` passed model
interruption but the command case reached the 30-second deadline. Diagnostic
revision `02fa8e6` confirmed command readiness without a usage snapshot; that
case also failed at the same deadline. Both failed runs confirmed container
removal. Their outcomes remain failures.

Pinned Codex drains pending tool work before sending token counts. Waiting for
the first count while the first command was deliberately blocked therefore
prevented the stop action. The same-turn primer establishes already-observed
usage before the second command blocks. The repaired source passed all three
cases without changing any deadline or native stop requirement.
[Primary native ordering](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/session/turn.rs#L3347-L3364)
and [interrupt parameters](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v2/TurnInterruptParams.json)
identify the pinned interface used.

## Verification and remaining ownership

The existing default hook allow/deny, stalled-hook cleanup and cumulative usage
commands were run on the same executable source. The fixture retains its
30-second case, five-second cleanup and 75-second outer attach limits. Socket,
pidfd, selector-registration, native-pipe, process, listener and container owners
retain explicit cleanup. Fixed structured diagnostics contain case/phase results,
without native payloads, tool arguments or transcripts.

All four native commands exited zero and the final Docker inventory contained
no probe containers. The three recorded regression-command wall times include
Docker setup and cleanup and are diagnostics only.

| Command argument | Result | Wall seconds |
| --- | --- | ---: |
| `--check-app-server-interrupt` | All three fresh native cases passed | Not measured |
| `--check-cleanup` | Stalled hook and container cleanup passed | 31.13 |
| No argument | Managed allow and deny passed | 8.16 |
| `--check-app-server-usage` | Native cumulative snapshots and replay passed | 4.31 |

On macOS arm64 with Python 3.11.17, 188 focused tests passed in 8.23 seconds.
Scoped Ruff formatting/lint, strict Pyright, shell syntax and complete-diff
whitespace checks passed. No new production line or dependency was introduced.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-usage
uv run --no-sync pytest -q tests/plugins/harness/codex \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
```

[Runner hosting #199](https://github.com/realkarych/kora/issues/199) owns production
launch and resource lifetime, authenticated hooks, first-cause classification and
fixed deadline/grace enforcement. This probe does not execute a D+30 grace
schedule, an unresponsive app-server escalation policy, macOS native Codex
interruption or protected-runtime domain termination. Those require their actual
consumers and native acceptance evidence. The final PR's Linux/macOS CI checks
repository compatibility, not those absent product capabilities.

[Selected facts #549](https://github.com/realkarych/kora/issues/549) and
[server execution #593](https://github.com/realkarych/kora/issues/593) keep their
existing boundaries. No provider is registered and no execution or validation
path is activated. The latest successful master mutation report, source
`6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`, was partial and audited only pricing
and CEL paths; it supplies no Codex lifecycle findings or completion evidence.
