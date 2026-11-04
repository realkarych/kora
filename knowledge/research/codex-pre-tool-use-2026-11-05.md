# Codex Bash and direct patch hook input evidence, 2026-11-05

[Issue #722](https://github.com/realkarych/kora/issues/722) adds a private
decoder for pinned main-session `PreToolUse` input and a projection into the
existing tool policy request. The decoder bounds the byte input before parsing
and admits the exact Bash and direct `apply_patch` shapes. The projection
takes its workspace and node path from the caller. The reported hook cwd and
other envelope context do not supply policy authority. [Policy contract](../specs/protocols.md#12-policy-dd-29).

## Pinned inputs

The source revision was `01c2b4386e5d508ba3f635312f30f879c0c292fa` from master
`213f08c5291c7d6f4430d601c3dd85e8c87f8c5e`. The fixture tree at
`tests/plugins/harness/codex` was `dceb9ecded80397b08e3869705bb13c862dc6c21`.
The provider source tree at `src/kora/plugins/harness/codex` was
`8dd42551e46f16a31171504e311b41c7b5b965d4`.

The fixture used Codex CLI 0.166.0 from upstream commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 was `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran the Linux amd64 image under
emulation on LinuxKit 7.0.14 arm64. The unprivileged containers used
`--network none`. The five hook input cases used fresh session workspaces
and a credential-free loopback Responses server. The staged decoder and
its JSON parser were the actual provider modules.

## Native observations

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-pre-tool-use
```

The default target matches `^Bash$`. The patch target matches
`^apply_patch$`. The two Bash cases use the same shell tool call and the
two patch cases use the same custom tool call. The loopback server checks
that Codex advertised the patch tool as a custom tool and that the
follow-up output is correlated to the original call.

| Case | Hook and completed-turn evidence |
| --- | --- |
| Bash allow | The decoded receipt matched the native call ID and exact command. The command created the marker. |
| Bash deny | The decoded receipt matched the same native call ID and command. Codex returned the fixed refusal and the marker was absent. |
| Direct patch allow | The hook ran once and its decoded receipt matched the native call ID and exact patch. Codex returned exit code zero and the expected changed path. The regular marker contained the expected bytes. |
| Direct patch deny | The hook ran once and its decoded receipt matched the same native call ID and patch. Codex returned the fixed hook refusal correlated to that call. The marker was absent. |
| Decoder refusal | The Bash hook ran once. The deliberate one-byte cap refused the input before decoding, so there was no decoded receipt. Exit 2 with a fixed nonempty stderr reason produced the correlated native refusal and the marker was absent. |

The pinned source places the selected cwd at the top level for the
built-in local Bash executor. The selected remote executor can report
the host context cwd there. The fixture admits the shape and discards
the reported cwd. The nested Bash and direct patch payloads carry the
command text. In the pinned implementation, a controlled exit 2 sets
the blocking state when the hook writes a nonempty stderr message.
[Pinned Bash input](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L336-L364),
[pinned patch input](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/apply_patch.rs#L370-L375),
[pinned hook envelope](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/events/pre_tool_use.rs#L176-L195),
[pinned hook exit handling](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/events/pre_tool_use.rs#L268-L283).

The patch success assertion uses the native result header with the exit
code, wall time and output label, followed by the exact changed path. An
initial development run expected the summary without that header and failed
the result assertion. The recorded run uses the confirmed native output
format and separately checks the final file contents.
[Pinned result formatter](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/mod.rs#L92-L116).

## Regression and cleanup

All nine invocations of `sh tests/plugins/harness/codex/run_hook_probe.sh`
exited zero on the same source revision. The new mode confirmed removal of
all four containers after the app-server children were reaped. The two
existing stall checks exercised deadline and child cleanup. The final
Docker inventory returned no containers with the probe's name prefix.
Wall times include Docker setup and cleanup and are diagnostic.

| Argument to `run_hook_probe.sh` | Wall seconds |
| --- | ---: |
| `--check-pre-tool-use` | 20.07 |
| No argument | 8.38 |
| `--check-cleanup` | 31.00 |
| `--check-write-stdin` | 13.45 |
| `--check-provider-env-key` | 7.98 |
| `--check-shell-controls` | 12.69 |
| `--check-executor-shell` | 67.84 |
| `--check-app-server-rpc` | 5.45 |
| `--check-app-server-startup` | 6.30 |

## Local policy and static checks

On macOS arm64 with Python 3.11.17, 450 focused tests passed. The real
`allow_deny` provider denied a protected target reached through a symlink
in the supplied workspace even though the reported cwd contained an
ordinary file at the same relative path. The provider also exercised an
ordinary patch, Bash allow and argument-specific deny, and the existing
shell-routed patch guard. The decoder tests exercised exact call ID and
text preservation, malformed and oversized input, unsupported tools, and
subagent context refusal. Scoped Ruff format and lint, strict Pyright,
shell syntax and whitespace checks passed.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/policy \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
git diff --check origin/master...HEAD
```

## Remaining ownership

The Linux fixture exercises controlled hook outcomes and the local tests
exercise the existing policy provider. Production hook delivery and failure
handling, including protected installation, attempt and turn binding, and
executor and workspace authority, belong to [runner host #199](https://github.com/realkarych/kora/issues/199).
The observations cover two tools in the pinned Linux fixture. Configured
declarations and complete selected-profile coverage remain under
[#549](https://github.com/realkarych/kora/issues/549). Canonical server start
remains under [#593](https://github.com/realkarych/kora/issues/593).
