# Selected native launch context on Linux

[#1154](https://github.com/realkarych/kora/issues/1154) implements the [local bound native context](../specs/protocols.md#bound-native-start). The selected fixture uses the [official Codex 0.166.0 and Bash pins](codex-selected-bash-pin-2026-11-08.md), a local Responses service and a synthetic API key. Trusted fixture observation correlates both role starts with their exact native processes, initial environment, working directory, argument vector and stdio descriptors. Only app-server receives the synthetic API key. Neither role inherits the supervisor-side stdio peers.

The returned reader and writer have only the four approved operations. The runtime retains the gates, closure and physical settlement. A consumed acquisition failure records a safe typed initiating cause in the same fence used by the asynchronous phases. The fixed public errors contain no native input or exception chain for mapped failures. The earlier fence retains precedence. A pre-admission refusal cannot change the owner's cause.

## Reproduction

From the repository root, with the locked development tools and Docker available:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-native-context
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
```

The wrapper builds a locked Linux x86_64 fixture. The runs use network-disabled containers, the local synthetic model and per-case deadlines. The wrapper checks the expected outcome and removes each exact container. Every admitted role is settled under the fixture's owned deadline.

| Case | Required observation |
| --- | --- |
| Selected context ready | Competing requests for the exact exec-server slot yield one borrowed stream and one fixed refusal. A noncanonical app role is refused. The exact app-server slot and both borrowed streams then complete the local model turn, managed hook and marker. The trusted fixture verifies the live role identity and settles both roles and sockets. |
| Legacy allocation with a live grant | Crafted environment and cwd are refused with a fixed nonretryable error. Zero native starts, no model or hook effect and no launch handle. |
| Legacy allocation with released grants | The same fixed refusal occurs before access to either released scope. Zero native starts and no effect. |
| App-server preparation cancelled | The second role has been admitted, preparation is pending, and the app stream has not been delivered. Cancellation propagates and remains the private initiating cause. The earlier borrowed exec writer fails and its exact supervisor socket is gone before outer teardown. Both registered processes settle. |
| Cutoff after second registration | The existing slot refusal stays fixed. The owner records the exact deadline, fences the earlier writer and retains both registrations through settlement. |

The portable regressions exercise real borrowed stdio, the legacy SDK capability distinction, invalid role types, refusal of unbounded reads, cutoff before consumption and retention of an admitted checkout deadline after a later refusal. This evidence concerns the controlled local launch. [Part D](https://github.com/realkarych/kora/issues/184#issuecomment-6680494511) owns the production endpoint, [the harness host](https://github.com/realkarych/kora/issues/199) owns the adapter and Policy handoff, and [#1004](https://github.com/realkarych/kora/issues/1004) tracks separate before-effect feasibility.

## Exact executable evidence

The host was macOS arm64, with Docker Engine `29.9.2 7.0.14-linuxkit` and CPython 3.11.17. Native wrappers ran on Linux x86_64. All listed wrappers exited zero and the selected container inventory was empty before and after each command.

The final executable revision was `19bf9a8313f147f96d40ce9ea5d039c217e10d5e`. Initial regression evidence ran on `ed319efb156674c87652a97eb90bb2f6303edc23`, and the consumed-acquisition checks ran on `372ae1454938fcfaa09bdb1c0cf2acb461da0f38`. The table uses the latest observed run for each wrapper. The final private cutoff classification was exercised on the final executable revision by the slot wrapper. The earlier phase, configuration and checkout runs checked the same recorded cause and gate settlement for their respective cases.

| Wrapper flag | Executable revision | Removed containers | Wall seconds | SHA-256 of combined output |
| --- | --- | ---: | ---: | --- |
| `--check-selected-native-context` | `19bf9a8` | 4 | 33.135 | `4db88755b5c1218cd0f13719be9c525fa3d40d4fe88cacd1ac00ee8acbc9a4a4` |
| `--check-selected-bound-fence` | `372ae14` | 7 | 59.041 | `904bf6672fd1748b92217bc6b2e56c372afcb694aa29eae7b03828283bf9f32f` |
| `--check-selected-bound-slots` | `19bf9a8` | 5 | 44.053 | `6b8cd490b5deca01c6ed480d107d6dccca4ec108ba019b9b1946d43d09bd3e05` |
| `--check-selected-bound-configuration` | `372ae14` | 4 | 34.555 | `fd5f7d00724d83ed0da25bd4850ef21a079fbf602442d338532e133a072cc236` |
| `--check-selected-bound-bash` | `ed319ef` | 2 | 29.347 | `8d3a2dd22d9f924fca49a815901aa3a1e22247df1002dd2cf6160e626b52c708` |
| `--check-selected-bound-checkout` | `372ae14` | 3 | 30.401 | `f8062e3cfa0c67114b23f2aab37ebcfff8e583de5aa2f262af24655f7d826cd0` |
| `--check-selected-bound-installation` | `ed319ef` | 3 | 24.842 | `5c08a237c265ea4f2c01e9754e018dc1650013bcc86a4cad61553dbc6be0e388` |
| `--check-selected-bound-environment` | `ed319ef` | 4 | 33.951 | `f9c0a910d3da42599b28a20d536d72cf872afe7f46b45256ad7b025e162193d1` |
| `--check-selected-gated-stdio` | `ed319ef` | 4 | 49.441 | `8c14c11ad5c3f8d14e52a91013ecef77446da8233ac78fdc0123f42b8514c6c9` |
| `--check-selected-startup` | `ed319ef` | 3 | 14.147 | `8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22` |

On macOS, this affected-area selection passed 1,712 tests on `ed319efb156674c87652a97eb90bb2f6303edc23`. The default repository markers deselected 21 measurement or host-fixture cases.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local tests/runner tests/conformance/test_local_process_lifetime.py tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
```

The final executable inputs passed 66 targeted macOS tests:

```sh
uv run --no-sync pytest -q tests/plugins/runtime/local/test_bound_native_launch.py tests/plugins/runtime/local/test_bound_slots.py tests/runner/test_gated_process_stdio.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py
```

Strict Pyright reported zero errors on the final executable inputs. Ruff formatting and lint, shell syntax and diff whitespace passed.
