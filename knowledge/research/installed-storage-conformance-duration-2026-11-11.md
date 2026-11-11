# Installed storage conformance run duration

Recorded 2026-11-11 under [#1382](https://github.com/realkarych/kora/issues/1382), part of [#1347](https://github.com/realkarych/kora/issues/1347). [Plan and diagnosis](https://github.com/realkarych/kora/issues/1382#issuecomment-6797134545).

## Source failure

`tests/cli/test_conformance.py::test_installed_provider_supports_local_tooling[artifact_storage-fixtures_storage-configuration5-18]` ran the installed `kora conformance` command under a 60-second `subprocess.run` timeout. In the macOS conformance job of [run 43013974893](https://github.com/realkarych/kora/actions/runs/43013974893), the timeout killed the command after 60.5 s.

- Four pytest groups of that job started together and shared one hosted runner. Two of them took 430.7 s and 442.9 s. The job deadline cancelled the third.
- The command's captured stderr held only the provider-loading record, so the run gave no other sign of progress.
- Green hosted runs of the same case took 26.6–41.8 s.

## Where the time goes

Measured locally on an idle machine (macOS arm64, CPython 3.11.17):

| Run | Duration |
|---|---|
| The storage case through the installed command | 21.7–22.9 s |
| Each of the other five providers in the same test | 1.8–4.3 s |
| The same storage checks in-process, `tests/conformance/test_artifact_storage_conformance.py` | 8.8 s for the file; 2.85 s for the 256 MiB staging round trip |

A profile of the command parent shows 141 process starts. 140 of them are `python -I -m kora.conformance.io_helper`: the parent starts a fresh helper interpreter for every journal mutation, and the run reaches journal revision 100.

- One helper start with its imports costs about 80 ms; the module import alone is 66 ms.
- Helper starts therefore account for about 11 s of the 22 s run.
- The parent spends 21.3 s of 22.9 s waiting for its children.

When several pytest groups share a runner, each helper start and the 256 MiB hashing slow down together. The case is a long sequence of short real steps. No hang was observed.

## Change

The owner's 2026-11-11 form from the [progress-bounded test guard record](conformance-progress-guards-2026-11-11.md) now applies to this test:

- The 60-second value stays.
- The value is measured from the last observed progress instead of the start of the command.

The progress signal is the command's existing structured diagnostics.

- The test sets `KORA_LOG_COMPONENT_LEVELS` to include `conformance.lifetime=DEBUG`. The parent then reports `started`, `activated` and `closed` lifetime records on stderr.
- A local storage run produced 41 records: the provider-loading record and 40 lifetime records, all with one invocation id. The longest interval between two consecutive records was 3.1 s, during the 256 MiB round trip, in a 21.8 s run.
- Every chunk of stderr that completes a line counts as progress.

Mechanism:

- `progress_run` in `tests/conformance/progress_guard.py` starts the command with its stdout and stderr on pipes that it owns and drains both concurrently.
- When the scope expires, it kills and reaps its direct child, then raises the same `subprocess.TimeoutExpired` with the command, the value and the output captured so far.
- `tests/support/provider_tooling.py` names the unchanged value `INSTALLED_COMMAND_SECONDS`. `InstalledTooling.environment()` builds the command environment for both paths.

The record assertions keep exactly one `protocols.loading.provider_loaded` record for the selected protocol.

- Every record must be at DEBUG level, so a lifetime warning still fails the test, as it did before.
- Every other record must be a `started`, `activated` or `closed` lifetime record of the reported invocation.
- There must be exactly one `started`, and the last lifetime record must be `closed`.

Regressions in `tests/conformance/test_progress_guard.py` use a real child process and an event loop whose time the test steps forward:

- `test_process_progress_scope_outlives_spaced_output_lines`: three 40 s silent steps, 120 s in total, each followed by a line. The command completes and returns its output.
- `test_process_progress_scope_kills_a_silent_child`: one 61 s silent step raises `TimeoutExpired`. Afterwards the child's stdin has no reader left.

## Limits

- One silent interval longer than 60 s still fails, by design.
- The total run time is bounded by the job deadline, as for the other converted guards.
- Other tests that call `InstalledTooling.run` keep the whole-command guard. In the same failed job, `test_installed_static_token_authentication_conformance` took 43.3 s of its 60 s without failing.

## Observation for a later decision

Most of the command's own cost is one helper process per journal mutation. The [conformance lifetime specification](../specs/conformance-lifetime.md) requires blocking durable writes to run in "bounded owned helper processes". Reusing a helper across mutations, or making its start cheaper, would shorten every installed conformance run, including runs by provider authors. It would also change helper lifetime and process ownership, which is the owner's decision. This change does not touch it.
