# Codex app-server startup identity evidence, 2026-11-04

[Issue #708](https://github.com/realkarych/kora/issues/708) adds the private
startup check over the [Codex stdio client](codex-app-server-rpc-2026-11-04.md).
The check uses installation inputs supplied by its caller. It compares the
first product in the reported user agent with the expected originator and
build version, the reported platform with the expected Unix operating system,
and the reported home with the caller's canonical prepared path. It sends
`initialized` after all three comparisons pass.

The build version is in the first product token. The client version supplied
in the request can appear separately in the suffix. The native home resolver
canonicalizes explicit `CODEX_HOME`. The startup checker compares text and
does not perform filesystem operations on the peer's path.
[Pinned initialize response](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/schema/json/v1/InitializeResponse.json),
[pinned user-agent producer](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/login/src/auth/default_client.rs#L152-L178),
[pinned home resolver](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/utils/home-dir/src/lib.rs#L20-L50).

## Native inputs

The tested source was `bff88b0b6b2554439e61dcbb89a8f83a90ca6c0f` from
master `d6ed92108d5ea74f57ff9bb33d4c82ad797c9b7d`. The existing
[managed hook fixture](codex-managed-hook-probe-2026-11-04.md) pins Codex
CLI 0.166.0 from commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`.
The Linux amd64 musl release archive has SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Docker Engine 29.9.2 and Buildx 0.38.1 ran the Linux amd64 image under
emulation on LinuxKit 7.0.14 arm64. The fixture stages the actual client and
startup modules for isolated Python import, uses fresh homes and runs
unprivileged with Docker `--network none`. The new cases use a credential-free
loopback Responses endpoint.

## Native startup cases

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-startup
```

| Case | Observed result |
| --- | --- |
| Matching identity | The check passed. The fixture opened an ephemeral thread, accepted one loopback Responses request and observed the matching completed turn. |
| Different expected version | The real app-server responded. Its reported version differed from the deliberate trusted expectation, producing `startup_version_mismatch`. The loopback observer recorded zero connections and requests. |
| Different expected home | The real app-server responded. Its reported canonical home differed from the separate prepared directory in the expectation, producing `startup_home_mismatch`. The loopback observer recorded zero connections and requests. |

Both refusal cases kept the loopback observer open while the fixture closed
stdin, drained stdout and reaped Codex. The fixture then settled the observer
and its tasks under the existing shared five-second cleanup deadline. It
checked zero connections and requests after settlement. The wrapper confirmed
removal of all three containers. The observation ceiling remains 30 seconds.

All eight fixture commands exited zero. Wall times include Docker setup and
cleanup and are diagnostic.

| Argument to `run_hook_probe.sh` | Wall seconds |
| --- | ---: |
| `--check-app-server-startup` | 6.64 |
| No argument | 7.53 |
| `--check-cleanup` | 30.96 |
| `--check-write-stdin` | 13.95 |
| `--check-provider-env-key` | 7.99 |
| `--check-shell-controls` | 12.31 |
| `--check-executor-shell` | 67.12 |
| `--check-app-server-rpc` | 5.45 |

The final Docker container inventory returned no probe containers.

## Wire and static checks

On macOS arm64 with Python 3.11.17, the 28 startup tests, 40 existing transport
tests and 30 architecture tests passed. Scoped Ruff format and
lint, strict Pyright, shell syntax and whitespace checks passed. The local
wire-test revision was `b140d01c28e06eecc9b14b6d69dbf7a8c4a1ece4`. That
revision adds one test per omitted reply field and leaves the native inputs
unchanged.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex \
  tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
uv run --no-sync ruff format --check src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync ruff check --ignore-noqa src/kora/plugins/harness/codex tests/plugins/harness/codex
uv run --no-sync pyright src/kora/plugins/harness/codex tests/plugins/harness/codex
sh -n tests/plugins/harness/codex/run_hook_probe.sh
git diff --check origin/master...HEAD
```

The wire tests check the exact acknowledgement sequence, both supported
operating-system labels, peer-field and identity refusals, fixed diagnostics,
startup-notification isolation, cancellation, deadline and EOF. Refusal cases
half-close the client's write side and read the peer through EOF to establish
that no acknowledgement or follow-up bytes were sent. Tests of the macOS
label exercise the wire logic. The native binary evidence above is Linux only.

The check establishes consistency of a connected server's reported fields.
The pinned server marks its internal connection initialized while handling the
request. Kora's acknowledgement gates advancement on the client side.
Binary integrity before launch, placement protection and process ownership
remain with the launcher and [runner host #199](https://github.com/realkarych/kora/issues/199).
[Selected provider facts #549](https://github.com/realkarych/kora/issues/549)
and [canonical server start #593](https://github.com/realkarych/kora/issues/593)
retain their existing owners.
