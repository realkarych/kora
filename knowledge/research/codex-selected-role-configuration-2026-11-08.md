# Selected Linux Codex role configuration

[#1134](https://github.com/realkarych/kora/issues/1134) covers the [bound native start preflight](../specs/protocols.md#bound-native-start) in the controlled selected launch. It uses the same official Codex 0.166.0 installation and selected Bash pin as the [executable evidence](codex-selected-bash-pin-2026-11-08.md).

The owner creates each role parent and its HOME, CODEX_HOME, and TMPDIR exclusively through the held parent descriptor. The six placement identities must differ. It writes a private, single-link regular `config.toml` from the admitted recipe and retains the original descriptor through gate settlement. The selected slot consumes its claim, checks the placement paths and ancestry, opens the fixed config name without following symlinks relative to the held CODEX_HOME, and compares the original file identity and exact bytes on the same comparison descriptor. The read is bounded by the expected size plus one byte, with a 64 KiB admission ceiling. The comparison descriptor closes before checking the original cutoff and revealing any environment value.

An admitted failure stops the slot set and fences the owned gates. A failure before returning the complete launch still unwinds partial descriptor acquisitions. Successful process settlement, closure of role anchors and removal of the admitted scratch root are checked separately. The root remains anchored until its actual inode is unlinked and the root name is absent.

## Reproduction and result

From the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-configuration
```

The wrapper exports hash-locked Linux fixture dependencies and builds the pinned Linux x86_64 probe. The execution container runs with networking disabled, a synthetic provider key, a local Responses fixture, an outer deadline and exact container removal.

| Case | Control | Result required by the wrapper |
| --- | --- | --- |
| Accepted pair | Both recipes use their captured placements and exact config | Both verified native roles complete the local turn, close their streams and are reaped. The admitted workspace is removed. |
| Early exec edit | Append valid TOML whitespace in place and release both reveal scopes | Bytes refusal, zero native role acquisition, zero model requests, zero hook effects, and confirmed removal. |
| Late app file replacement | Wait for real exec readiness, replace the app config with identical private bytes on a different live inode, and release both reveal scopes | File identity refusal before app disclosure or start, exec writer fenced, only the prior exec gate settled, zero model or hook effects, and confirmed removal. |
| Late app placement replacement | Wait for real exec readiness, move the app CODEX_HOME and create a new private placement with valid equivalent config | Placement identity refusal with the same fence, settlement, zero-effect and removal checks. |

The late controls keep independent redaction registrations for the values already delivered to exec-server while the shared reveal scope is released. The native refusal output is matched exactly. The portable regressions also exercise an actual symlinked role parent without outside creation, partial ownership unwind, same-byte file replacement, file mode, hard links, symlinks, bounded bytes, cutoff with the comparison descriptor already closed, and refusal to remove a replacement scratch root.

## Exact executable evidence

Tested executable revision `709efd3bfd7a89f6f2be58332e16dc2481d6fd60` on 2026-11-08. The host was macOS arm64 with Docker Engine `29.9.2 7.0.14-linuxkit`. Each probe ran in Linux x86_64. Each command exited 0. The container inventory was empty before and after every command, and all 30 expected removals were observed.

| Wrapper flag | Removed containers | Wall seconds | SHA-256 of combined output |
| --- | ---: | ---: | --- |
| `--check-selected-bound-configuration` | 4 | 45.959 | `6f149dbd0d75e38efd8bfd1c9fdc47eca1b17bb41b6aa705ff378e4f476a70f9` |
| `--check-selected-bound-bash` | 2 | 31.649 | `338b1278ae27be37df8cce55edac5d55215a81677a4a1c8087fa8461420fc170` |
| `--check-selected-bound-checkout` | 3 | 38.762 | `58e21bf8f642e58e563d067d5a6d68bf557a89edf35befab4c63d5d3d74a5b38` |
| `--check-selected-bound-installation` | 3 | 43.826 | `fb092157a6aecdf1c679122b333507c29f13d8e9ed97780e5a0d25463c4b10e9` |
| `--check-selected-bound-fence` | 3 | 41.485 | `dc3b693daa6786fb7ff4ccc5ff7c6d70712cf98a63ab046323258c21e24f2104` |
| `--check-selected-bound-slots` | 4 | 44.666 | `4968e91c38db6f89703c4ac4965ba811877ea8d233df2edc54be793b85ed9f1a` |
| `--check-selected-bound-environment` | 4 | 50.050 | `30f0bb5d390eb6ebfd0bf87bf544b2c4394c158e4ac37e7faa7bb295f83548c4` |
| `--check-selected-gated-stdio` | 4 | 89.792 | `eb7096cb7cc812be3f399f443733e25b15b8ee312855f2f4825d26e247ed0462` |
| `--check-selected-startup` | 3 | 14.479 | `8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22` |

On macOS arm64 with CPython 3.11.17, `uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local` passed 1,451 tests. The repository default selection deselected 21 measurement or host-fixture tests. Strict Pyright reported zero errors, and Ruff formatting and lint, shell syntax and diff whitespace checks passed.

This evidence covers the cooperative selected fixture at the pre-disclosure boundary. The root identity check and anchored unlink observation establish the observed fixture removal. Concurrent hostile path protection through configuration use and production before-effect Policy retain separate acceptance.
