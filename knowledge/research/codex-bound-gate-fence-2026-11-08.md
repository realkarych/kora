# Shared activation and stream fencing for bound Codex roles

[#1099](https://github.com/realkarych/kora/issues/1099) exercises one private
stop owner for the two gated native roles from the [bound role slots](codex-bound-native-slots-2026-11-08.md).
The fixture keeps each exact process owner as soon as the native gate is
acquired, inside the working-directory descriptor ownership block. Both
gates use the existing launch cutoff. Stop revokes the role slots, control
writes and stream delivery synchronously. Each exact owner remains
available for physical cleanup. Closure failures remain visible separately
from the initiating refusal. A watcher can wake before successful removal,
and its socket stays owned until removal succeeds.

The executable input is the official Codex 0.166.0 Linux x86-64 musl binary
and the archive pin recorded with the [bound environment](codex-bound-environment-2026-11-08.md).
The Linux containers run with networking disabled, a loopback model server
and synthetic credentials. Dependency setup can fetch public pinned inputs.

## Reproduction

Run from a committed checkout. The shell probe uses `git archive` to select
its exact source revision.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-fence
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/plugins/runtime/local/test_control_frames.py tests/runner/test_gated_process_stdio.py tests/runner/test_gated_process_settlement.py tests/plugins/runtime/local/test_bound_slots.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/harness/codex/test_selected_bound_environment.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py
```

## Native observations

| Case | Result |
| --- | --- |
| Accepted pair | Both gated native roles and the selected startup completed. The fixture observed the real synthetic-key model turn, managed hook and Bash marker, then confirmed both streams and children settled. |
| Stop after app gate readiness, before acknowledgement | The fixture invoked the real acknowledgement, observed its refusal, and reaped the app gate after natural exit 72. The already borrowed exec writer was closing synchronously. |
| Stop after app acknowledgement, before stream delivery | The fixture invoked the real stream borrow and observed `closed`. The already borrowed exec writer was closing synchronously. The child can act on the frame already sent at this boundary, so the result establishes delivery refusal and cleanup. |

In both stop cases the fixture observed two working-directory opens, eight
Unix sockets and two subprocesses. It confirmed both exact gate owners and
streams settled. The model listener remained active through gate
settlement and observed zero connections or requests. There were zero hook
invocations and marker writes. The shell checked the complete fixed refusal
records and removed the exact containers.

The portable checks exercise a stop with two prepared real gates, a
suspended write after a failed socket close, and preservation of the
original deadline when automatic socket disposal fails. These observations
qualify the selected launch boundary. The production launcher and the
before-effect Policy gate have separate acceptance criteria.

## Run record

The native modes ran from clean revision `72d0f84e19eb1b92bad448c75451a06e61dc53c4`
on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The
containers ran Linux amd64. Every mode returned 0. The matching container
inventory was empty before and after each mode.

| Mode | Removed containers | Wall time | Log SHA-256 |
| --- | --- | --- | --- |
| `--check-selected-bound-fence` | 3 | 27.821 s | `187671622e9a0052c8154ba9538fd7bf01dd4c8268b68572323135779b8963c2` |
| `--check-selected-bound-slots` | 4 | 31.480 s | `2a850f43e5d1d2f3672aa622d76a9e1e8060806259a7d4f7c4856c84fcb0b268` |
| `--check-selected-bound-environment` | 4 | 33.704 s | `8e7958af518a6018a69ee1476d96afc16380a2b5b1b2bddbabac42ef03002e97` |
| `--check-selected-gated-stdio` | 4 | 40.816 s | `bea1de8fbf40c3f4ee7b6a5601cbd689c4e3bb26b0d2c84bed7f39e173989498` |

The focused macOS arm64 CPython 3.11.17 run passed 146 tests at the same
revision. Strict Pyright, Ruff lint and changed-file formatting, shell
syntax and diff whitespace checks passed.
