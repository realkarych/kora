# Selected native role acquisition under the local runtime owner

[#1145](https://github.com/realkarych/kora/issues/1145) gives the selected Linux fixture a provider-neutral [bound native acquisition owner](../specs/protocols.md#bound-native-start) in LocalRuntime. The fixture keeps the official Codex 0.166.0 archive and protected Bash from the [selected executable evidence](codex-selected-bash-pin-2026-11-08.md). The same synthetic provider key and local Responses service supply the accepted case.

The local runtime owner captures each role's exact argv and installed executable, common checkout, role configuration, complete captured adapter assignments and harness grant names, directory resource identity, original monotonic cutoff and stdio bound. It checks the nonsecret recipe and placement agreement before any value disclosure. The consumed slot checks the retained checkout and configuration and uses its admitted environment to construct the real runner gate. The owner registers every returned gate before verifying acquisition identity and checking the original cutoff again. A consumed-slot failure immediately revokes every registered gate and keeps them for physical settlement. Unknown and used selectors have no stop authority.

## Reproduction and native results

From the repository root:

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bound-slots
```

The wrapper builds the locked Linux amd64 fixture and runs each selected case in a network-disabled container. It verifies the exact fixed outcome sequence, the synthetic local model and selected commands, and removes each container under the existing outer bound.

| Case | Boundary demonstrated |
| --- | --- |
| Accepted roles | Both actual roles acquire owner-registered gates and complete the selected local turn with exact observed native inputs and closed streams. |
| Stopped owner | Released handles, zero new role acquisitions or model and hook effects, and fixed refusal. |
| Released app grant | Unknown and used selectors leave the real ready exec writer live. The admitted app then fails materialization, immediately closes the exec writer and starts no second gate. |
| Native spawn refusal | After real exec readiness, a temporary unprivileged Linux process limit rejects app gate creation. There are two audited attempts and two retained handles, with a valid exec registration and no app registration. The exec writer is already closing when the refusal reaches the caller. |
| Handoff expiry | The injected clock observes the original cutoff at the second actual process-create event. Both gates register, the handoff refuses before the app gate can prepare or activate, and the earlier writer closes synchronously. |

The last three cases check zero model and hook effects, exact closure of every retained gate and removal of the admitted workspace. Portable regressions exercise cross-recipe cwd, complete adapter assignments, grants, executable and placement disagreements before disclosure. They also check detached safe error translation for invalid materialized input, existing pending-claim and replay behavior, and a recorded native gate acquisition failure with a bad directory descriptor.

The selected fixture covers private acquisition and synchronous fencing. The public role-only context, production host and Policy handoff, protection through configuration use, and whole-domain qualification retain their separate acceptance.

## Exact executable evidence

Tested executable revision `8b1f3a5c1ab55f71b53ddcbb72dffd9fe4f60427` on 2026-11-08. The host was macOS arm64 with Docker Engine `29.9.2 7.0.14-linuxkit`. Each probe ran in Linux x86_64. Each wrapper exited 0. The container inventory was empty before and after every command, with all 31 expected removals observed.

| Wrapper flag | Removed containers | Wall seconds | SHA-256 of combined output |
| --- | ---: | ---: | --- |
| `--check-selected-bound-slots` | 5 | 44.219 | `02c6cac00ff7235164639babf99a8fb16baf262dd140c412238b9f9a292072cc` |
| `--check-selected-bound-configuration` | 4 | 36.227 | `334df9c15eacde17afc148e3eed54f14542812d40781ed4b9c2dc3359604028a` |
| `--check-selected-bound-bash` | 2 | 24.851 | `41a9670fddfb13fd992a64c0fc46883292f667654b62e65d37deb53e8ab5ae35` |
| `--check-selected-bound-checkout` | 3 | 42.180 | `2042d7f12d9e02ade9a9c67bbfdef8a9059493b34db2105a6eea58bdc33b9f5a` |
| `--check-selected-bound-installation` | 3 | 39.169 | `a1c05db9d15006cd5c744cadd9e00b45ddede2fe838615066a505c34d34d8217` |
| `--check-selected-bound-fence` | 3 | 33.557 | `3076525973577cef784128621c9c7f088ac4518e4d8e4eab9cba0f7aa308a57a` |
| `--check-selected-bound-environment` | 4 | 39.536 | `78450032732376b2b6c33ebfcb178e6482f811e43266adb52a34c0c9fac5277d` |
| `--check-selected-gated-stdio` | 4 | 40.598 | `7fa370f92b887b4f73a1c8f59e3399b67a013c6830f6cbac5ffb3fa595824d69` |
| `--check-selected-startup` | 3 | 14.445 | `8fd9a40346c07cf29ddc2e21e6fef0cdfd49082e6cc5127f9b071a7a9d445f22` |

On macOS arm64 with CPython 3.11.17, the following command passed 1,707 tests. The default repository markers deselected 21 measurement or host-fixture tests.

```sh
uv run --no-sync pytest -q tests/plugins/harness/codex tests/plugins/runtime/local tests/runner tests/conformance/test_local_process_lifetime.py tests/architecture/test_import_layers.py tests/architecture/test_no_suppression.py
```

Strict Pyright reported zero errors. Ruff formatting and lint, shell syntax, diff whitespace and offline links passed.
