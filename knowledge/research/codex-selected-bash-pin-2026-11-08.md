# Verified Bash in the selected Linux Codex launch

[#1125](https://github.com/realkarych/kora/issues/1125) checks the protected
Bash executable before the selected fixture constructs its role recipes
or materializes either role's values. The verified path supplies both
recipes and the exact executor metadata expectation. The dedicated bound
image sets the protected UID 65532 account shell to `/usr/bin/bash`.
The pinned native executor selects the shell from the account record.
The existing unbound gated and startup fixtures keep their own image and
shell behavior.

The selected image contains GNU Bash 5.2.15 at `/usr/bin/bash`, a root-owned
regular file of 1,265,648 bytes with SHA-256
`55b89ab22bee4792a210f493a53fb066accd5d30b69837c28d98be5ff863efcf`.
The fixture reuses the protected file check and original launch cutoff.
The controlled container runs with the existing unprivileged UID,
dropped capabilities and disabled runtime networking. The selected
Codex version and archive pin remain those in the [installation
record](codex-protected-installation-2026-11-08.md).

## Reproduction

Run from a committed checkout. Dependency setup can retrieve public
pinned inputs. The launched containers use a local scripted model and
synthetic credentials.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-bash
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-checkout
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-installation
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-fence
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local/test_bound_checkout.py tests/plugins/runtime/local/test_bound_slots.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/runtime/local/test_control_frames.py tests/runner/test_installed_executable.py tests/runner/test_gated_process_stdio.py tests/runner/test_gated_process_settlement.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py
```

## Observations

| Selected case | Observation |
| --- | --- |
| Protected Bash and correct digest | Both official roles started with the checked Bash path. The local model supplied a fixed command to the real managed hook. The exact call and turn correlated with successful hook completion and one completed command. A synchronous helper held its Bash parent alive before the command wrote its marker. |
| Protected Bash and wrong expected digest | The same path and size with a different well-formed digest produced fixed refusal before either role or its witness was acquired. Shared and app-only admission scopes were already released. The fixture observed no native gate, model, hook or marker effects. |

The witness uses the kernel-attested Unix peer and the helper's actual
parent. Both processes must match the captured checkout and selected
thread and call. The parent must match the checked Bash image by native
device and inode or by the existing explicit Rosetta translator and ELF
mapping evidence. The witness guards both live identities while opening
reap anchors. It grants cleanup authority only after both liveness guards
pass and acknowledges the helper before the original turn cutoff. The
command then writes the exact expected marker. The probe observes reaping
of both Bash and its helper through their retained identities, closes the
witness socket and anchors, settles the two role gates and streams, and
removes the exact container. Failure cleanup cancels the witness when
the shared fence latches and settles the witness and role owners under the
same cleanup deadline.

The controlled fixture establishes the admitted Bash image and exact
process effects for this command. Production interpreter selection,
effective configuration, the public launcher and failure-safe
before-effect Policy retain separate acceptance criteria.

## Run record

The native modes ran from clean revision `2c58ba24cdd4b443a47557c34612633e7f86184f`
on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The
containers ran Linux amd64. Each mode returned 0, and the matching
container inventory was empty before and after every mode.

| Mode | Removed containers | Wall time | Log SHA-256 |
| --- | ---: | ---: | --- |
| `--check-selected-bound-bash` | 2 | 40.711 s | `21aefe6a3ad0c328b306cde8a37da81b2f2eed9a93b5591ecc2ccc588a62d771` |
| `--check-selected-bound-checkout` | 3 | 40.293 s | `bc10581bf6d29df0784e20f8fff0c62556d9c362fe3df9a4f6a53eca52a035cf` |
| `--check-selected-bound-installation` | 3 | 31.376 s | `ba2a83dd4cf6660c70cac9af695ab2dc4c9eef940d54e234ac692376e40923d7` |
| `--check-selected-bound-fence` | 3 | 38.547 s | `57a15389a2744e460d230065b7db43eb45bddd2fd092358e898c8ef38c88c2d0` |
| `--check-selected-bound-slots` | 4 | 44.017 s | `9d6c209ab955f97028764f17d654ab7a00a32eccee9f3ca53f8eec490cd99848` |
| `--check-selected-bound-environment` | 4 | 34.879 s | `1a5d41c37786e9d6268aba782599ff5448334b1d9a81f27d7dfa5595494110bc` |
| `--check-selected-gated-stdio` | 4 | 46.123 s | `3164946de0d38758ad1892dbc323f4d89530696e18481c7ad1d979f95512c6bc` |

The focused macOS arm64 CPython 3.11.17 selection passed 972 tests at the
same executable revision. Strict Pyright, Ruff, shell syntax and diff
whitespace checks passed.
