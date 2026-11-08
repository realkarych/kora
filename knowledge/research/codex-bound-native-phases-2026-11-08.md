# Selected native phase ownership on Linux

[#1150](https://github.com/realkarych/kora/issues/1150) extends the [bound native launch](../specs/protocols.md#bound-native-start) from synchronous acquisition to preparation, activation and stream delivery. The controlled fixture retains the [official Codex 0.166.0 and Bash pins](codex-selected-bash-pin-2026-11-08.md), synthetic API key and local Responses service.

Each acquired role has one phase handle. Valid operations check the original cutoff at admission and after their runner awaits. The owner belongs to its constructing loop. Wrong, repeated, overlapping and foreign-loop requests leave the admitted phase and sibling gates alone. An admitted failure or cancellation records one fixed typed phase cause, revokes the pair and leaves their exact physical resources with the owner. The complete existing activation frame remains the release point. An activated role can still be refused stream delivery and must be settled.

## Reproduction

From the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-fence
```

The wrapper builds a locked Linux x86_64 fixture and runs it with networking disabled. The accepted pair performs its local synthetic-model turn. The selected fixture still checks explicit stop before activation and after activation before delivery, with no model or hook effects in either refusal.

| Admitted adverse control | Observation before outer cleanup |
| --- | --- |
| Preparation of an exited second gate | The exact pidfd child exits before the app prepare call. Real channel failure records the closed reason and revokes the already ready exec writer. |
| Pending preparation cancelled | The exact second child is stopped, the valid prepare is pending, and overlapping and wrong-phase requests leave it and the exec writer intact. Cancelling the admitted task records cancellation and revokes the earlier writer before teardown. The exact child is resumed for settlement. |
| Cutoff at phase entry | The exact cutoff refuses a valid prepare, records expiry and revokes the earlier writer before setup or app delivery. |
| Cutoff on acknowledgement return | The clock observes closure of the exact app control socket after the activation frame, forcing equality at the post-await check. The admitted acknowledgement refuses before reaching the delivery barrier and revokes the earlier writer. |

Each adverse control verifies zero model and hook effects and removes the admitted workspace and exact container after both gates settle. Portable tests exercise the real gate phases, one-time delivery, foreign-loop requests and preservation of the initiating expiry through later refusal and stop. This evidence concerns the selected controlled launch. The provider-facing role-only context, production host, actual endpoint authority and before-effect Policy qualification retain their separate acceptance.

## Exact executable evidence

Tested executable revision `f829f637bac6d035fec14da8ce8b55fd274f59af` on 2026-11-08. The host was macOS arm64 with Docker Engine `29.9.2 7.0.14-linuxkit`. Each probe ran in Linux x86_64. Each wrapper exited 0. The container inventory was empty before and after every command, with all 35 expected removals observed.

| Wrapper flag | Removed containers | Wall seconds | SHA-256 of combined output |
| --- | ---: | ---: | --- |
| `--check-selected-bound-fence` | 7 | 79.853 | `a7ef0adbc4d69e2f9594c0a1a23b9d6a8b980763ca903c4e34a2a050b12c00eb` |
| `--check-selected-bound-slots` | 5 | 43.064 | `a33f3e0e3741c57aaa686e31f07566d893b73dec41e172d5bc3567dc333405fa` |
| `--check-selected-bound-configuration` | 4 | 35.126 | `1e7c43e845a2125f2e9a00f099d46d874d5c26d8642ba02cda68c711d5772723` |
| `--check-selected-bound-bash` | 2 | 44.125 | `359e44355cd961804daaedb99aa38a63d2b868e97efa1c9063886c78097be932` |
| `--check-selected-bound-checkout` | 3 | 28.805 | `d32b9394595f99a9aa93199a5625b98d4b82af43b311823e646d187ad84aa476` |
| `--check-selected-bound-installation` | 3 | 34.706 | `d2a51d235849db7be12089199fea22f1dfe9398eadafd2e92138d5ea279a570d` |
| `--check-selected-bound-environment` | 4 | 59.453 | `e4f50abc235cd5b5a82656d536bd885c2c676bc5035642c2b99e1797a56c4736` |
| `--check-selected-gated-stdio` | 4 | 60.135 | `4fadbf4e28ca8f45f3d83284a79a4189e6e9c87e7595d55a9d49437ea1968461` |
| `--check-selected-startup` | 3 | 14.037 | `8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22` |

On macOS arm64 with CPython 3.11.17, the following command passed 1,709 tests. The default repository markers deselected 21 measurement or host-fixture tests.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local tests/runner tests/conformance/test_local_process_lifetime.py tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
```

Strict Pyright reported zero errors. Ruff formatting and lint, shell syntax, diff whitespace and offline links passed.
