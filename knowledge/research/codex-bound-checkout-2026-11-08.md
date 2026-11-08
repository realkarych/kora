# Checked working-directory identity for the selected Codex roles

[#1117](https://github.com/realkarych/kora/issues/1117) binds the selected
Codex app-server and exec-server to one actual working-directory descriptor.
The private fixture captures the checkout's path, ancestor identities and
leaf identity. An admitted slot checks the selected view and closes its
comparison descriptor before materializing its environment. The original
cutoff and stop latch are checked again before reveal. A refused contender
cannot run the preflight. A failed admitted start uses the common fence.

Both gates duplicate the exact retained descriptor and change directory
through it before readiness. The owner retains it through gate and stream
settlement. The accepted pair also verifies each live process's actual
working-directory device and inode under the existing PID guard. The
selected fixture has one trusted controller. The cooperative-local
same-user residual still applies to later path and content changes.

## Reproduction

Run from a committed checkout with the existing pinned Linux probe inputs.
Containers run with networking disabled, a loopback model service and
synthetic credentials. Setup can fetch public pinned inputs.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-checkout
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-installation
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-fence
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-environment
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-gated-stdio
uv run --no-sync pytest -q tests/plugins/runtime/local/test_bound_checkout.py tests/plugins/runtime/local/test_bound_slots.py tests/plugins/runtime/local/test_bound_environment.py tests/plugins/runtime/local/test_control_frames.py tests/runner/test_installed_executable.py tests/runner/test_gated_process_stdio.py tests/runner/test_gated_process_settlement.py tests/plugins/harness/codex/test_selected_bound_environment.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py
```

## Observations

| Checkout case | Result |
| --- | --- |
| Accepted pair | Both owned live roles had the captured cwd device and inode, exact argv and executable identity, and separate expected streams. The existing authenticated turn, managed hook and Bash marker passed. Both gate owners, streams and the checkout descriptor settled. |
| Replace the selected view before the exec start | The replacement had a distinct inode under the same lexical path. With the shared and app-only secret handles already released, the checkout refusal came before materialization. No native gate or Unix role sockets were acquired. |
| Replace the selected view after exec readiness | The already-owned exec role reached real readiness. A distinct directory replaced its path before the app preflight. The released app-only handle never reached materialization. One process and four Unix socket acquisitions matched the exec role alone. Its stream and child settled before checkout cleanup. |

Both refusals checked the moved original and the replacement path for
marker effects. Model and hook observations remained active through gate
settlement and recorded no effects. The shell compared complete fixed
refusal output and confirmed exact container removal. The launcher path
uses the existing protected Codex executable from the [installation
fixture](codex-protected-installation-2026-11-08.md).

Portable tests exercise a real checkout rename-and-replace, deadline
recheck before disclosure, foreign claim refusal, and exact descriptor
closure. The real-gate descriptor test moves the selected directory after
verification and installs a replacement at the same path. The child
writes only in the moved original, which distinguishes the retained
working-directory descriptor from a new path open. Production checkout
protection, role placement and configuration binding, Bash, the public
launcher and before-effect Policy retain separate acceptance criteria.

## Run record

The native modes ran from clean revision `e80ba32c5f8b7d542125b52de0abf5fbb7f93aac`
on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The
containers ran Linux amd64. Each mode returned 0, and the matching
container inventory was empty before and after every mode.

| Mode | Removed containers | Wall time | Log SHA-256 |
| --- | --- | --- | --- |
| `--check-selected-bound-checkout` | 3 | 27.278 s | `64525dec74b763dd7ca3d93cb48acee4f2aea60557ef0d3b371b910cd7563bec` |
| `--check-selected-bound-installation` | 3 | 38.855 s | `be8089c41635c67bb6c4d027428a95ed80e7b5967de3a0c7d741a0f833a14321` |
| `--check-selected-bound-fence` | 3 | 37.830 s | `587857c4ae611ae28e5e6e52ff60a5cf64fcb068e01ed4b04e98c1a40a16d210` |
| `--check-selected-bound-slots` | 4 | 55.956 s | `665756c4fcc4c5b7f900f6e77c6cd5e9b7b1f3f4ffc87cb884a87782fbf94b38` |
| `--check-selected-bound-environment` | 4 | 30.176 s | `091923e72a24d42906233c41fc6a7c93aadb8fca8355174189ba456d086c6398` |
| `--check-selected-gated-stdio` | 4 | 47.206 s | `eadafa54c8a3ffb0d88d02bb8bc6f796a3e45101e85604494cc96b4a1cee5fa7` |

The focused macOS arm64 CPython 3.11.17 selection passed 157 tests at the
same executable revision. Strict Pyright, Ruff, shell syntax and diff
whitespace checks passed.
