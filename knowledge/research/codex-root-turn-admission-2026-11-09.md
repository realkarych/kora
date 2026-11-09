# Selected Codex root-turn admission

[#1234](https://github.com/realkarych/kora/issues/1234) qualifies one root
turn through the [selected controller](../specs/codex-execution-profile.md#execution-and-path-binding).
The trusted local launch helper borrows app-server's stream from one
attempt owner and pairs it with that owner's admission. The controller
takes the frame and individual read limits from the same handle.

The owner grants once on its own loop and before its original stop or
cutoff. The RPC checks and encodes the frame before the synchronous
claim. It calls the writer immediately after the claim. The owner
retains the spent state through uncertain delivery, a lost reply, an
active competing client, and a replacement RPC.

## Reproduction

Run these commands with GNU `timeout`, local Docker and Buildx, uv, and
CPython 3.11.17.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-root-admission
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-turn
sh tests/plugins/harness/codex/run_hook_probe.sh
```

The fixture builds an image with the pinned official Codex 0.166.0 binary
and runs nonroot Linux amd64 containers with networking disabled and a
synthetic loopback model. The root test uses a locally owned direct executor as a controlled
transport for the native turn. The app-only provider view refuses the
executor selector before acquisition. Trusted local composition starts
the executor separately.

## Results

| Case | Observed result | Wall time |
| --- | --- | --- |
| Owned root admission | Exactly one `turn/start` writer handoff and two expected model requests. A deliberate repeat against the prepared thread reached the owner and returned the spent refusal. Both native roles and observed endpoints settled. The private tree and exact container were removed. | 45 s |
| Historical selected turn | The complete turn and app-server peer loss after `SIGKILL` both passed. Both native pairs and two exact containers settled. | 34 s |
| Lightweight direct fixture | The allow and deny cases both passed. The exact container was removed. | 9 s |

All six before and after probe container inventories were empty. The
selected root case bounded the host attach capture and published the
fixed result, child-reap and workspace-removal verdicts. The existing
30-second turn, 5-second cleanup and 75-second attach deadlines applied.

The executable inputs were tested at
`5815007571027116c55aeb2664a8a07a3cc0011b`. The native fixture tree is
`91db89ab0d5770d9b07843fbd3628ea7373b836c`, the source tree is
`6ddf3719000dbe146a1977d772a3945953ba68a1` and the lock blob is
`881ac4770ed91a6b738b9cde54c42323191c2bdc`. Docker 29.9.2 and Buildx
0.38.1 used a Linux arm64 daemon on a macOS arm64 host.

| Combined log | SHA-256 |
| --- | --- |
| Owned root admission | `a51dd10d49beb662ec75f637c85623b19335153e5c3008410bee199f661dbb85` |
| Historical selected turn | `c171d86e442f575c7b600d4d529515ca07027509769824d9725697a140702017` |
| Lightweight direct fixture | `81cb3d568e04e701d468e345a89832e61d118dfeefc762f77db27ed7411fcba1` |

On macOS arm64 with CPython 3.11.17, the focused bound-native, Codex and
architecture run passed 1,920 tests in 43.76 seconds. The portable
cases exercise the real app stream's 4 KiB read budget, owner stop and
cutoff during the request, a rejected foreign loop, outbound frame encoding refusal,
writer-entry and drain failure, an observed complete frame with no reply,
and a fresh attempt. Strict Pyright and Ruff passed on the changed files.

Production app-method admission, the relay, post-forward settlement,
Policy and configured facts retain their separate qualification under
#184, #199, #549 and #772.
