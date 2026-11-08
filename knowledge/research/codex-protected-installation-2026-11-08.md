# Protected executable selection for the Linux Codex fixture

[#1104](https://github.com/realkarych/kora/issues/1104) verifies the pinned
installed executable before either selected role may consume its slot or
materialize its environment. Both recipes carry the verified path into the
existing gate. The selected Linux container runs as UID/GID 65532, with all
capabilities dropped, `no-new-privileges`, networking disabled and no
supplied mounts. Its executable and ancestor directories are owned by root
and exclude group and world writes. This supplies protection against path
replacement and in-place writes by the selected fixture identity through
activation. The [earlier path replacement experiment](https://github.com/realkarych/kora/issues/184#issuecomment-6719644219)
recorded the effect of the same gate on a writable installation.

The source is the official Codex 0.166.0 Linux x86-64 musl archive, SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The installed ELF is 319,346,888 bytes, SHA-256
`b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8`.
The verifier checks a bounded canonical path descriptor-relative, with no
symlink following, and uses kernel execute access for the actual identity.
It matches the open file and its path entry through a bounded hash scan
with the original monotonic cutoff, then closes the acquired descriptors.
The outer deadline and exact container disposal cover a stalled syscall.

## Reproduction

Run from a committed checkout. The shell probe archives the exact source
revision. Dependency preparation can fetch public pinned inputs. The
running containers use a loopback model service and synthetic credentials.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-installation
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-fence
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/runner/test_installed_executable.py tests/runner/test_gated_process_stdio.py tests/runner/test_gated_process_settlement.py tests/plugins/runtime/local/test_control_frames.py tests/plugins/runtime/local/test_bound_slots.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/harness/codex/test_selected_bound_environment.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py
```

## Observations

| Selected installation | Observation |
| --- | --- |
| Protected executable and correct pin | Both live roles matched their exact argv and the pinned ELF identity. The fixture observed the same authenticated local turn, managed hook and Bash marker, then confirmed both owned streams and children settled. |
| Protected executable and wrong expected digest | A well-formed different digest produced fixed installation refusal before gate acquisition or either released canary could be revealed. |
| Replaceable location and correct expected digest | The fixture copied the official executable into its own workspace and renamed it there. Protection refused that actual launch recipe before gate acquisition or either released canary could be revealed. |

Both refusal cases checked zero gate working-directory opens, Unix sockets,
subprocesses, model connections and requests, hook invocations and marker
writes. The shell compared complete fixed refusal output and removed the
exact container for each case.

The native observation accepts the exact device and inode from
`/proc/<pid>/exe`. The measured Docker Rosetta environment exposes the
translator path through that kernel link while the direct translator path
is outside the container's namespace. The translated branch checks that
exact link and requires executable mappings with the exact paths and
identities of both the kernel-reported translator and the verified Codex
ELF. It uses one bounded mappings read under the existing live-PID check.
An initial attempt to stat the translator pathname directly failed in
this environment and the exact container was removed.

Portable checks exercise real file permissions, symlink and special-file
refusal, changed file identity, size, cutoff before and after a scan, and
exact descriptor closure. The protected container supplies fixture-specific
evidence. Production installation selection, Bash and managed configuration
binding, the public launcher and before-effect Policy retain separate
acceptance criteria.

## Run record

The native modes ran from clean revision `bd7e7852f51fc6f6b61c5b5bfa7a48e043b90350`
on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The
containers ran Linux amd64. Each mode returned 0, and the matching
container inventory was empty before and after every mode.

| Mode | Removed containers | Wall time | Log SHA-256 |
| --- | --- | --- | --- |
| `--check-selected-bound-installation` | 3 | 38.610 s | `9a7237aa34289275776be74fa54b200598d4bae24c23f5ba33b943f5f0c6378a` |
| `--check-selected-bound-fence` | 3 | 32.977 s | `f9f3528cc2598db564716a1d57900cdb39004c179b7bcb18af15885df1fd1ce8` |
| `--check-selected-bound-slots` | 4 | 38.812 s | `7d345cd1b0b1ab60f17614d09a3e5c777b3e8a41cf73a197e1c6c10445dc9742` |
| `--check-selected-bound-environment` | 4 | 34.997 s | `9ab45efea35cd3b108a141076a353bf852f87946224809e3fdf0e99375fc0f93` |
| `--check-selected-gated-stdio` | 4 | 40.987 s | `8c4347144de4242e9fb8d3466b0d0910650f90c0b065b563639c6a180cf6e6d9` |

The focused macOS arm64 CPython 3.11.17 selection passed 152 tests at the
same executable revision. Strict Pyright, Ruff, shell syntax and diff
whitespace checks passed.
