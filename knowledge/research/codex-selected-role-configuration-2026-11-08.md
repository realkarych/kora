# Selected Linux Codex role configuration

[#1134](https://github.com/realkarych/kora/issues/1134) covers the [bound native start preflight](../specs/protocols.md#bound-native-start) in the controlled selected launch. It uses the same official Codex 0.166.0 installation and selected Bash pin as the [executable evidence](codex-selected-bash-pin-2026-11-08.md).

The owner creates each role parent and its HOME, CODEX_HOME, and TMPDIR exclusively through the held parent descriptor. The six placement identities must differ. It writes a private, single-link regular `config.toml` from the admitted recipe and retains the original descriptor through gate settlement. The selected slot consumes its claim, checks the placement paths and ancestry, opens the fixed config name without following symlinks relative to the held CODEX_HOME, and compares the original file identity and exact bytes on the same comparison descriptor. The buffered reader requests the expected size plus one byte for comparison. The admitted expected configuration has a 64 KiB ceiling. The comparison descriptor closes before checking the original cutoff and revealing any environment value.

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

Tested executable revision `ee05b255dd30c6b27108a034521af4a8a87ef864` on 2026-11-08. The host was macOS arm64 with Docker Engine `29.9.2 7.0.14-linuxkit`. Each probe ran in Linux x86_64. Each command exited 0. The container inventory was empty before and after every command, and all 30 expected removals were observed.

| Wrapper flag | Removed containers | Wall seconds | SHA-256 of combined output |
| --- | ---: | ---: | --- |
| `--check-selected-bound-configuration` | 4 | 36.049 | `3c6045ae4ab87653a42d30e1d2cfb6aa23745ebc368f6f267e227065d4c78f0b` |
| `--check-selected-bound-bash` | 2 | 21.309 | `bc8859448eb8aab4acc1d006759e516aa03f1ba1239cf45f374582f5185a754b` |
| `--check-selected-bound-checkout` | 3 | 26.081 | `1b606a1785fc8a1365b0508d6b97d924488c5e07ecbf9d0fad7d8a51d58c9bd7` |
| `--check-selected-bound-installation` | 3 | 26.731 | `730b90679309e93820d608cc00961da5d9b5b0ff31ffdd8d97804cb0dddb78a2` |
| `--check-selected-bound-fence` | 3 | 28.677 | `231f58695ec2aca21bbedafd4c90e1000197df7fd41b2ea7ec4d9d96f107e4df` |
| `--check-selected-bound-slots` | 4 | 28.062 | `8de052b172cbc3ba527a54085a9a8ab69176dede79f6194c507f4b10d63ab095` |
| `--check-selected-bound-environment` | 4 | 25.984 | `2a9eac8bc187e1171604fde15c269e81b6f3bbc7b8c780b3dd13649be352a25d` |
| `--check-selected-gated-stdio` | 4 | 43.275 | `e19a3447f5b3ad467960de732318ad71d858aa0e3122c7585fdc53822f6b7627` |
| `--check-selected-startup` | 3 | 14.261 | `8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22` |

On macOS arm64 with CPython 3.11.17, `uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local` passed 1,451 tests. The repository default selection deselected 21 measurement or host-fixture tests. Strict Pyright reported zero errors, and Ruff formatting and lint, shell syntax and diff whitespace checks passed.

This evidence covers the cooperative selected fixture at the pre-disclosure boundary. The root identity check and anchored unlink observation establish the observed fixture removal. Concurrent hostile path protection through configuration use and production before-effect Policy retain separate acceptance.
