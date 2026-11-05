# Codex confirmed terminal result evidence — 2026-11-06

[Issue #797](https://github.com/realkarych/kora/issues/797) combines the
one-shot controller's confirmed terminal evidence with the shared
[terminal result and output contract](../specs/protocols.md#codex-confirmed-terminal-result).
The trusted caller selects the controller and its thread from the same
invocation. The projection consumes its typed terminal error, independently
of any earlier retry, and yields no result on transport loss without a
confirmed terminal. Only typed limit and authorization details on a failed
terminal select the corresponding provisional class. Native interruption
does not assign a runner cause or class.

The shared result contains only the closed subtype, error flag and
optional closed reason. HTTP status 100–599 is separate optional evidence;
it does not classify a terminal. Only admitted completed summary text
selected for output passes through the existing strict decoder. The
detached candidate stays outside the result and its representation; the
consumer copies a nested JSON value before mutation. JSON null is
present. Missing and invalid output retain native completion; identity
refusal propagates. The existing decoder enforces raw and canonical
bounds. Native summary admission precedes projection, even without
selected output.

## Source and native execution

The fixture verifies `codex-cli 0.166.0`, upstream
[6ff1e1ca3b30fc8b914720681fcf5fe62e95f856](https://github.com/openai/codex/tree/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856).
The Linux amd64 musl archive SHA-256 is
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 base manifest is
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Native runs used repository revision
`e3396cee7f02d523eef4073b05c4667af66eb0d9`. Provider directory tree
`62a806a8f66dc2bf18c02e10c64cb92f75811383` and fixture directory tree
`d6960809cb4f10634db1dad3925d676d45cd46b9` identify the native inputs.
Every file staged by the native build and the launch script remain
identical at the executable projection revision below. Logs do not embed
the Git revision; the association is recorded from the run and staged
input comparison.

The cases run in unprivileged, network-isolated Linux amd64 containers
on an aarch64 LinuxKit 7.0.14 Docker VM using Docker Engine 29.9.2,
Docker Desktop 4.98.0 and Buildx 0.38.1. The fixture uses a loopback model
endpoint with scripted responses, no host mounts or operator
credentials, and no external model request. The pinned build can
retrieve release assets. Existing 30-second case, five-second cleanup
and 75-second attach deadlines remain in force. The case driver closes
fixture connections and reaps the native server; the wrapper confirms
exact container removal.

Run from the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-errors
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-output
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-interrupt
```

| Mode | Verified native observations | Exit; removals; wall time (diagnostic) |
| --- | --- | --- |
| Errors | Failed terminals with matching nonretry notices: 401 and 503 `httpConnectionFailed`, 429 `responseTooManyFailedAttempts` | 0; 3; 14.01s |
| Output | Completed turns admit the final summary with valid and invalid JSON text; server death after a completed assistant item retains that observation without a terminal or terminal-selected output | 0; 3; 9.67s |
| Interrupt | Held model stream and running command yield native interrupted terminals; the command interruption observes child exit by pidfd. Server kill yields actual reap and RPC disconnect with an open tool prefix and no terminal. | 0; 3; 11.65s |

A separate final Docker inventory found no remaining probe containers.
The mode logs have SHA-256, in the order above:

- `7482a48f947e100fb48baea9b07029525b99420fa352cabe03b359cbee897023`
- `48a619b6cdae1ac77fc5a276c82ec91ccc203c809d169969bec8ab310fc4447f`
- `6b6984d1d5f312ca38b66a06ad66e322e6db90c3029069298cbfb3a1ae5c5d46`

## Shared projection

Executable revision `f3da519ab75bba34f4497d82bb655f31252b893b` has provider
tree `ff04b89185da78ce6d824abb2f46124915d5a938` and fixture/test tree
`3fb7ff5ff60fc5c7db6b08d7f8a1d3e1da64e3a6`. Its complete executable patch
from the native revision has SHA-256
`4eea009883653f7d488d15a2a26aa8e636c78cd895271190a3c46cb9c64529c5`.

```sh
uvx --from uv==0.13.1 uv run --no-sync pytest -q tests/plugins/harness/codex tests/architecture
```

Local macOS 27.2 arm64 with locked Python 3.11.17 and uv 0.13.1 passed
1,601 cases: 671 Codex (29 new), 930 architecture; no failures, errors or
skips. Diagnostic wall time 44.12s. JUnit SHA-256:
`4b642a4b2b0482d3db34f2d73a5621af470847b169627c612d4ac69acf533aa2`.
Scoped Ruff format and lint with `--ignore-noqa` and strict Pyright passed.

The stdlib Linux fixture verifies native controller inputs and cleanup,
while the full-package socket tests execute the shared result, event and
JSON candidate checks. Typed provider-limit and authorization mappings
are exercised by scripted wire messages; no live provider response
establishes them. The native fixture does not exercise this shared
projection, native macOS Codex, or the protected two-process runtime.

The adapter and [runner host](https://github.com/realkarych/kora/issues/199)
still own event publication, final first cause, policy precedence, limit
and retry signaling, effective output validation and publication, and
actual execution-domain closure. This projection supplies no launch,
enforcement or complete attempt result.
