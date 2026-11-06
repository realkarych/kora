# Selected Codex turn through shared events

Measured on 2026-11-06 for [#872](https://github.com/realkarych/kora/issues/872). The executable revision is `2f1b61749cffa9fb3df718d3e2402fa12797481c`, with kora source tree `c7fa924b388848a5b880d29074f9af53d4b0f427`, fixture tree `32af0e7fa24a28b4bb90edca44c69fba529e337a` and lock blob `fa81e33e07da74047914defea6961cba6562caa3`. The native Docker build reads the committed package and fixture, including the pinned official Codex 0.166.0 Linux amd64 musl archive (SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`).

## Reproduce the selected turn

From a repository checkout with uv, CPython 3.11.17 and a local Docker socket, run the following. The selected mode reads the source and lock from the current Git commit.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-turn
```

The measured host used uv 0.11.7 and Docker Engine 29.9.2 with LinuxKit 7.0.14 on arm64, running the Linux amd64 CPython 3.11.17 container under emulation. The wrapper installed 33 hash-verified runtime and conformance wheels for the target in an isolated reservation under a shared 120-second deadline. Both uv stages used the explicit installed system interpreter from the private reservation. The run used a relative `TMPDIR` and deliberately unusable ambient uv index and tar options. Docker fetched or reused its pinned build assets. Build commands and the test container had no external network access. Both native roles and the scripted Responses server communicated through their existing stdio and loopback channels. The provider received the expected synthetic key. The selected Bash command checked key absence in its environment and wrote the exact executor-home and Bash marker.

| Case | Turn events | Usage events | Result events | Additional observation |
| --- | ---: | ---: | ---: | --- |
| Completed | 1 | 2 | 1 | Masked transcript, closed `codex_completed` result, separate strict JSON candidate |
| Cut | 1 | 1 | 0 | Confirmed prefix preserved, actual app-server SIGKILL and RPC disconnect, HTTP peer EOF before teardown |

The fixture holds the second scripted reply until the shared sink accepts the first usage event. Both runs count one limited event. The two cumulative observations project normalized token counts of `(50, 20, 30, 10, 4)` and `(95, 25, 40, 30, 10)` for input, cached input, cache writes, output and reasoning respectively. The cut retains only the first. The shared ledger retains the latest observation and attribution gap, with no model rows and unavailable aggregate coverage. The masked canary stays out of the message and the two test canaries stay out of the result data. The candidate remains a separate typed value for the caller.

The selected command exited zero in 34.952 seconds. Each case verified both direct native role processes reaped and the exact container removed. The cut waiter settled and the provider observed peer EOF before fixture teardown. The exact reservation and final probe container inventories were empty. A separate isolated Git input with a tracked symlink and the same relative temporary root returned exit 1 before wheel acquisition. The reservation and container inventories were empty after that refusal as well.

## Regression evidence and limits

The selected-startup mode passed three cases in 13.483 seconds on the final native inputs with a relative temporary root. On `aeb48ed714842cd059e5c50f3a30f9e896f2d034`, app-server usage passed one case in 4.881 seconds and app-server output passed three in 9.629 seconds. The final wrapper delta resolves the temporary root before allocation. The absolute-root baselines used the same physical reservation and the same native and fixture payload inputs. Each container was removed. Full-package socketpair tests exercise matching and foreign terminals, startup refusal, EOF, deadline advancement inside usage stamp supply and immediately before result emission, and the borrowed stop signal.

The observed closure covers the fixture's app-server, exec-server, provider stream, waiter and containers. Docker-managed caches are outside the reservation. The synthetic shared-UID fixture establishes no production endpoint identity, Policy authorization, macOS native behavior or domain release. The result remains provisional and the artifact candidate still requires the common validation and publication path.
