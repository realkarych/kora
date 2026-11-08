# Selected Codex alternate executor registry — 2026-11-08

[#992](https://github.com/realkarych/kora/issues/992) observes an alternate native executor bootstrap on official Codex 0.166.0. The pure selected launch constructor refuses the four exact `CODEX_EXEC_SERVER_NOISE_*` control names listed in the [selected profile](../specs/codex-execution-profile.md#private-launch-construction) in both workload name collections. The existing conflict result, neighboring unreserved names and native role recipes are unchanged.

## Source and observation

The pinned [environment discovery](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L197-L239) checks the [Noise settings](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L642-L683) before using the ordinary environment provider. The alternate manager inserts the `remote` environment and [starts connecting at insertion](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L573-L588). The [registry client](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/remote.rs#L254-L278) sends the selected registry environment ID in the connect route and a harness public key in the request.

Both cases use fresh app-server and exec-server roles and verify `CODEX_EXEC_SERVER_URL=none` in the app-server environment. The override adds the four names only to app-server. Its URL is the owned loopback listener. The registry ID, bearer and account header are synthetic and distinct from the model key and selected executor bearer. The same `environment/info` lookup receives a matched RPC refusal in both cases. `environment/status` verifies the state after that refusal.

| Case | Settled status of `remote` | Observed HTTP requests |
| --- | --- | --- |
| Selected app-server environment | `unknown` | Zero |
| App-server with the four Noise values | `disconnected` | One exact loopback `POST /cloud/environment/kora-probe-registry/connect`. One expected bearer and one expected account header. Fixed HTTP 403. |

The accepted registry request matches the bounded connect body shape. The listener refuses every other route and any extra connection. The lookup cases start no turn and confirm absence of the hook receipt, invocation file and workspace marker. The evidence covers alternate discovery and a refused local connection before a model request. Actual launch and peer binding remain with [#184](https://github.com/realkarych/kora/issues/184), required Policy with [#199](https://github.com/realkarych/kora/issues/199), and complete declarations with [#549](https://github.com/realkarych/kora/issues/549).

The initial discovery run at `ad9ef02f4ab1e3ba39936cb8a2da6afd144df46d` passed the control and failed its provisional `pending` requirement for the override. Both containers were removed. [The refined plan](https://github.com/realkarych/kora/issues/992#issuecomment-6715067032) waits for the native lookup refusal before checking the status. Both discovery cases then passed at `2cec307afde75bc29b1cdfad8fcf632a09f514e9`.

## Reproduction

Executable revision `c4db577ceb57a98886aac0db7723adf300fbdaf3`, provider tree `12129d3d6c6e2e426b21b351a9123034c7dfbb19`, fixture tree `7121f207d64a77838cc27410a9c8ec81ceb074e2`. The fixture verifies the [official Codex 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at source `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. Builds can fetch pinned inputs. The nonroot execution containers have `--network none` and no host mounts. All exchange endpoints are loopback and credentials are synthetic. The case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-noise-registry
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Alternate registry | 2 | 4 | 2 | 0 | 7.05 s |
| Selected launch regressions | 4 | 8 | 4 | 0 | 31.87 s |

The modes closed their owned streams and listeners, and probe-container inventories were empty before and after. Combined stdout/stderr log SHA-256 values in table order were `48d5c9c6750483b19e3d93601927b9c24fc5faccafaf0564d9c900b5cd90108b` and `4f858acc4c7e281bcf15c553bf5dfa140a5a010caa1694fc4657ce742a9fff42`. Diagnostics contain fixed case and outcome labels.
