# Owned gated Codex app-server stdio on Linux

[#1052](https://github.com/realkarych/kora/issues/1052) gives the selected
fixture an app-server transport from the real runner gate. The runtime owner
creates a private Unix socketpair and passes the child end as standard input
and output. The existing bounded gate protocol still controls execution.
The supervisor owns the peer and lends the stream pair once after
acknowledgement. The selected exec-server remains under the fixture's
existing launch owner.

## Native inputs and checks

The fixture uses the official Codex 0.166.0 Linux x86-64 musl release at
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The network-disabled container runs as its existing unprivileged UID.
It supplies a synthetic machine ID because the pinned slim image omits the
file required by the real identity reader. The kernel supplies the boot
identity, process namespace and process observations. The model credential
and executor bearer are synthetic, and the model endpoint is local to the
container.

The app-server's stdio descriptors match their two gate descriptors. The
fixture verifies that there is no extra child alias and that the retained
supervisor peer is absent from the native roles. It brackets the live role
observations with pidfds and verifies the expected initial key grant. The
approved Bash command checks key absence before writing its marker.

For the content-free refusal check, the real gate receives a bounded
launch record containing synthetic credential and content canaries. The
fixture closes the gate before activation, observes acknowledgement
refusal, and releases the owned child. The wrapper compares the complete
attached output with fixed records.

## Reproduction

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-inventory
uv run --no-sync pytest -q tests/runner/test_gated_process_stdio.py
```

The native mode uses the pinned release and a locked archive of the
repository inputs. Build setup can fetch the public archive and locked
wheels. The running fixture requires neither credentials nor a model
provider connection.

## Results

The final gated mode at `f7c8a3fa0be43820afdcc6af3e47edb647d6a496`
passed four cases on Docker 29.9.2 with Buildx 0.38.1 and LinuxKit 7.0.14
on macOS arm64. The native container was Linux amd64.

| Case | Observation |
| --- | --- |
| Selected inventory present | The gated app-server ran the scripted authenticated turn, invoked the expected managed hook and produced the exact marker. |
| Managed hook missing | The gated app-server refused startup before any model request, hook invocation or marker. |
| Managed hook matcher changed | The gated app-server refused startup before any model request, hook invocation or marker. |
| Prepared gate fenced before acknowledgement | The owner closed and reaped the gate. Complete attached output matched the expected two fixed records. |

Each of the three native pairs verified the same app-server descriptor and
role-key observations, closed the exact supervisor endpoint, and reaped both
children. The refusal case reaped its single gate child. All four exact
containers were removed. The unchanged direct hook-inventory control passed
three cases with six direct reaps and three exact container removals on
`ff798324af20cb5667a31049a94556f91079e48d`. Only failure diagnostics on the
gated refusal path changed afterward. The probe inventories were empty before
and after both runs.

| Mode | Wall time | Log SHA-256 |
| --- | --- | --- |
| `--check-selected-gated-stdio` | 42.39 s | `5a65c5286fcc5122295e2e58b110b95b51e1de0b5076aff21e78d48b4b61ce1e` |
| `--check-selected-hook-inventory` | 22.52 s | `b9bf4760e8914fdb022a1065c84750d23ffeebd8c8be278416a78c70cf035d8a` |

The local gate regressions exercise an exact child PID and byte exchange,
concurrent borrow refusal, prepared-gate refusal, interrupted preparation,
deadline precedence, blocked-writer closure and partial acquisition.
They require the stream to settle before process release. The focused
runner, lifetime, Codex and architecture selection at `ff79832` passed 1,978
tests on macOS arm64 with CPython 3.11.17. Strict Pyright, Ruff, lock and
shell checks passed on the final source.

## Scope

The result concerns the internal gate path and the selected official Linux
native pair. Production role slots, physical executable binding, real
credential grants, the common harness host and the before-effect Policy
gate retain their own qualification. Native macOS coverage and complete
whole-attempt closure remain separate.
