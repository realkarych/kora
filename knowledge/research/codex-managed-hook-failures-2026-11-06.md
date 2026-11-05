# Codex managed hook failure evidence — 2026-11-06

[Issue #804](https://github.com/realkarych/kora/issues/804) checks whether
a managed hook failure prevents Bash execution on the
[selected API-key execution profile](../specs/codex-execution-profile.md).
It compares the actual native result, the model-visible tool response
and the filesystem effect for one identical scripted tool call.

## Observations

| Hook behavior | Native hook status | Tool response | Marker after the turn |
| --- | --- | --- | --- |
| Exit 2 with the fixed blocking reason on stderr | `blocked` | Exact PreToolUse refusal | Absent |
| Exit 2 with empty stderr | `failed` | Successful Bash exit 0 | Exact marker |
| Exit 1 | `failed` | Successful Bash exit 0 | Exact marker |
| Timeout after the helper has started | `failed` | Successful Bash exit 0 | Exact marker |

Each case uses a fresh workspace, one bounded receipt for the identical
Bash tool identity and command, and two authenticated loopback model
requests. One `hook/completed` matches the acknowledged thread and turn
and the unique managed synchronous PreToolUse command handler. Its
entry distinguishes the explicit block and the three failures. Native
notification metadata is an observation; the fixture independently
checks the model response and exact marker contents. The marker includes
the selected nonlogin Bash and executor home. The command requires the
provider-key name to be absent from that executor environment.

For timeout, the fixture matches the kernel peer PID from the ready
socket to the recorded helper PID. It opens the pidfd while that exact
helper is live, then observes its exit and barrier EOF before teardown.
Pidfds for both native roles verify that neither role has exited at
that point. The selected fixture then reaps both direct native children and closes
the Responses server and its streams. The wrapper removes the exact
container. It does not separately inventory the executor
listener and WebSocket handles before removal.

The pinned [hook parser](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/events/pre_tool_use.rs#L200-L301)
distinguishes blocking from failure. The
[pinned timeout path](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/engine/command_runner.rs#L331-L400)
reports a failed hook and attempts to kill the hook command's process
group. The
[tool dispatch](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/registry.rs#L839-L890)
can continue after a nonblocking hook result. In the observed cases,
an explicit deny works, but a failed managed hook does not enforce
denial before effects. The upstream
[fail-closed request](https://github.com/openai/codex/issues/41979) was
open on 2026-11-06; it supplies no released guarantee for this pin.

## Reproduce on Linux

The fixture verifies official `codex-cli 0.166.0` at upstream
[6ff1e1ca3b30fc8b914720681fcf5fe62e95f856](https://github.com/openai/codex/tree/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856).
The Linux amd64 musl archive SHA-256 is
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 base manifest digest is
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Executable revision `b9e129f16df4412423ebb7d4130b111fdac11274` on base
`02cbb021220970d087af28aaf13fa4811f433b79` uses fixture tree
`b896a0c5e6d6daaa91d365d2c7dd5ba45737e682` and unchanged provider tree
`ff04b89185da78ce6d824abb2f46124915d5a938`. The complete executable
patch SHA-256 is
`e66d9a39a0b99a3a44be6f1295d82bc243d7923a6f946b18976dbed5b68c77e4`.
Run from the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
```

All four cases use non-root, network-isolated amd64 Linux containers
in the aarch64 LinuxKit 7.0.14 VM (Docker Engine 29.9.2, Docker Desktop
4.98.0, Buildx 0.38.1), without host mounts or operator credentials.
The synthetic local provider key and fresh executor bearer are fixture
inputs; no external model request is made. The pinned build may fetch
release assets. The native hook timeout is five seconds only in the
timeout image. Existing case, cleanup and attach limits remain 30, 5
and 75 seconds, respectively.

| Command mode | Cases | Exact container removals | Exit | Wall time (diagnostic) |
| --- | ---: | ---: | ---: | ---: |
| `--check-selected-hook-failures` | 4 | 4 | 0 | 36.78s |
| `--check-selected-launch` | 3 | 3 | 0 | 22.69s |
| `--check-pre-tool-use` | 5 | 4 | 0 | 20.88s |

The two selected modes verified 14 direct native child reaps across
seven cases; the two baseline Bash cases share one container. A
separate final Docker inventory found no remaining probe container.
Native logs, in table order, have SHA-256:

- `8234eb3c0c381889fe1fb3dbdcb606eb213161c91e05ea37ee03820b950b09bf`
- `32bf40c500cbce566f8cb11bba536b11febe1caeddfe108194b80850a3776753`
- `747ab9ef6d789f45ce4a5177ef78935c5cde2937034f31db3f7763f7cf245ba3`

The logs do not embed the Git revision. The run is associated with the
clean recorded fixture inputs. The initial fixture also observed all
four effects; the final revision adds exact liveness observations for
both native roles and fixes static issues.

## Local checks

At the same clean executable revision, macOS 27.2 arm64 with locked
Python 3.11.17 and uv 0.13.1 passed 1,601 cases: 671 Codex and 930
architecture, no failures, errors or skips. Wall time was 50.68s
(diagnostic). JUnit SHA-256:
`62521f140af76f9099be2c1f861d54460bd12b2e33cdae6abbaa8b79c72ab005`.

```sh
uvx --from uv==0.13.1 uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture
```

Scoped Ruff format and lint with `--ignore-noqa`, strict Pyright and
shell syntax passed. The local suite checks shared fixture and
architecture regressions; the three commands above establish the
actual Linux observations.

## Scope and limits

These are selected-profile Linux Bash observations. They do not
establish fault behavior for other tools, platforms or versions. The
direct patch baseline verifies the existing explicit allow and deny
cases only. The shared-UID fixture does not establish authenticated
production Policy transport or a protected execution domain.

[Runner host #199](https://github.com/realkarych/kora/issues/199) and
[launcher #184](https://github.com/realkarych/kora/issues/184) retain
enforcement and stop authority. A post-effect failure record cannot
satisfy pre-tool enforcement. Required policy stays enforced or refused
under [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md);
this experiment activates no production adapter or complete declaration.
