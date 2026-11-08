# Initial acquisition for the selected Codex role slots

[#1095](https://github.com/realkarych/kora/issues/1095) exercises one private
slot per role from the existing bound environment. The selected Linux fixture
uses the same monotonic cutoff for both roles. It claims and consumes the
exec role before materializing that role's environment and creating its
gate. After the exec endpoint reports readiness, the fixture claims and
consumes the app role. The acquisition callback runs synchronously after
the second cutoff check, and the fixture records the gate before awaiting it.

The inputs are the official Codex 0.166.0 Linux x86-64 musl binary and the
archive pin in the [bound environment record](codex-bound-environment-2026-11-08.md).
Container execution has networking disabled. The fixture uses a loopback
model endpoint and synthetic credentials. Dependency setup can fetch public
pinned inputs.

## Reproduction

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/plugins/runtime/local/test_bound_slots.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/runtime/local/test_environment.py tests/plugins/harness/codex/test_selected_bound_environment.py tests/plugins/harness/codex/test_selected_proxy.py tests/runner/test_gated_process_stdio.py tests/architecture/test_no_suppression.py
```

## Observed behavior

| Slot fixture case | Observation |
| --- | --- |
| Accepted pair | The exact executor readiness, role environments, authenticated model turn, managed hook and Bash marker passed. The app-server alone received the provider key. The exec-server and Bash child lacked it. Both streams, direct children and the model listener settled. |
| Duplicate exec claim | A released handle in the requested role made materialization adverse. The fixed launch refusal came first. No selected cwd opened and no socket or process was created for either role. |
| Stopped owner | The same released-handle control refused before reveal or gate acquisition. |
| Partial start | Exec registered and reported readiness before the app grant was released. App materialization failed. The total of one cwd open, four Unix socket constructions and one subprocess matched exec acquisition. The exec stream and child and the model listener settled before workspace and diagnostic scope cleanup. |

The three adverse cases had zero model connections and requests, hook
invocations and marker writes. The shell compared their complete attached
output against fixed records. Expected refusal remains the first cause if
cleanup fails. The fixture emits a separate cleanup failure and retains the
workspace and scopes with exact-container disposal until settlement can be
confirmed.

All three modes ran at `176d31eb089f919c7bde33fe1c403458e4b10a46` on
Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The running
containers were Linux amd64. Each mode returned exit 0 and removed four
exact containers. The matching container inventory was empty at the start
and after each mode.

| Mode | Wall time | Log SHA-256 |
| --- | --- | --- |
| `--check-selected-bound-slots` | 33.559 s | `0674a75451db72bd3632d86dd45daf11024a255637be807b9eac232b8782e0b6` |
| `--check-selected-bound-environment` | 28.951 s | `63163a4270a4308fa313d974a2e067013a6db0e57db5658b9ed46bbcb291afee` |
| `--check-selected-gated-stdio` | 46.421 s | `0e6fd61f02a94feae840fabbab930166ba490de8a554d65972eae07ba9fa0a01` |

The focused macOS arm64 CPython 3.11.17 selection passed 93 tests, including
same-owner claim replay, wrong role, foreign same-role claim, a duplicate
race, an alternate captured role set, exact expiry and cutoff crossed during
materialization. Production role context exposure, physical installation
identity, asynchronous activation and delivery fencing, and the required
before-effect Policy gate have separate qualification.
