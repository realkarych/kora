# Bound local environments for the selected Codex roles

[#1058](https://github.com/realkarych/kora/issues/1058) exercises the local
runtime's shared environment binding through both real runner gates. The
fixture detaches the authored text and secret-handle maps once. The selected
Codex constructor checks their exact names. The local binder then checks
both recipes and the exact per-role harness grants before it reveals any
values. Each transient result supplies the complete environment of one
role.

## Inputs and ownership

The probe uses official Codex 0.166.0 Linux x86-64 musl at
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
It runs in the same locked network-disabled container as the
[gated stdio fixture](codex-gated-stdio-2026-11-08.md). The model endpoint
is local. The model key and executor bearer are synthetic.

The fixture checks the exact live PID-bound initial environment of each
role and verifies four distinct socket identities. Neither native role
inherits either retained peer or the other role's child end. The first
model request authenticates with the app-server key. The selected Bash
command checks key absence before creating its expected marker. A common
cleanup cutoff covers both gate owners and the local model listener. The
fixture retains diagnostic scopes through the dependent output checks.
Unconfirmed cleanup leaves its workspace and scopes with the exact
container disposal path.

## Reproduction

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/plugins/runtime/local/test_environment.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/harness/codex/test_selected_bound_environment.py tests/runner/test_gated_process_stdio.py
```

Build setup can fetch the public pinned archive and locked wheels. The
running probe uses only its local scripted model and synthetic values.

## Observed results

Both native modes ran at `8f1143606c625ab18160e32d5ab0abf7ffcbdfd5` on
Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The running
containers were Linux amd64.

| Case | Observation |
| --- | --- |
| Accepted roles | Exec readiness arrived on its owned stream. The exact authenticated turn, managed hook and marker succeeded. Two subprocess audit events and eight Unix socket construction events matched the gate pair. Both borrowed streams and direct children settled. |
| Second-role runtime `PATH` collision | The local owner refused before revealing the released app grant. The actual gate path acquired no Unix socket and launched no subprocess. |
| Exec assignment claims the app key grant | The local owner refused the name before revealing the released grant. The actual gate path acquired no Unix socket and launched no subprocess. |
| Authored secret name `BASH_ENV` | The provider rejected its unset reserved name with a released handle before the local binder or either gate started. |

Each refusal has zero local model connections and requests, zero managed
hook invocations and no marker. Complete attached output matches the two
fixed diagnostic records. The legacy gated mode also passed all four cases,
including both hook inventory refusals and the prepared gate fence. All
eight exact containers were removed. The probe container inventory was
empty before and after both modes.

| Mode | Wall time | Log SHA-256 |
| --- | --- | --- |
| `--check-selected-bound-environment` | 32.129 s | `863ca9ebc29a1974e4622d040b1e50dbd888b69308bab6e9bae3e1ef53efa558` |
| `--check-selected-gated-stdio` | 38.629 s | `f519c4c3f34591ed433eedda25f445ba5919be1778091ceb7389e2357959103a` |

The focused macOS arm64 CPython 3.11.17 selection in the reproduction
command passed 62 cases. It includes original single-role behavior,
immutable snapshots, released handle precedence, and preservation of an
original error during workspace cleanup. A broader selection on the
previous staged head passed 873 cases and failed the existing
`test_owned_loopback_settles_socket_before_tls_handshake` with a TCP reset.
The test and loopback source are unchanged by this patch.

Production role slots, physical executable and workspace identity, actual
credential grant delivery, host integration, native macOS and required
before-effect Policy activation remain separate qualification boundaries.
