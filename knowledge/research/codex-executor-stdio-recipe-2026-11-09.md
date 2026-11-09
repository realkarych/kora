# Selected Codex executor stdio recipe

[#1228](https://github.com/realkarych/kora/issues/1228) verifies the private
Codex constructor for the Linux relay route. The constructor produces
both role recipes, including each exact argv, working directory, native
configuration and role-owned environment. The relay route selects
`exec-server --strict-config --listen stdio://`. Its recipe takes no
transport digest. The relay registration bearer belongs to the separate
guard and dynamic app-server registration.

The direct fixture route keeps its exact WebSocket arguments and existing
lowercase digest syntax. Both routes apply the same lexical path,
configuration and environment reservations. Missing or malformed direct
digest, any digest on the stdio route, and an unsupported selector refuse
before materialization. The local selection remains an internal constructor
input. The [selected profile](../specs/codex-execution-profile.md#bound-native-launch)
assigns actual launch and stream custody to #184.

## Linux results

| Command suffix | Result |
| --- | --- |
| `--check-selected-executor-peer` | One same-listener valid-bearer decoy refused before native traffic, followed by the exact app-server and guarded Bash marker. One child-pair reap and one exact removal. |
| `--check-selected-executor-guard` | All nine direct controls and nine guarded stdio cases passed, including Bash and direct patch allow, refusal, error and timeout. Eighteen child-pair reaps and exact removals. |

Both native roles in the guarded cases use the recipe's actual argv, cwd,
configuration and environment. The app-server sends the expected synthetic
key to the loopback model. A bounded Linux observation of the live executor's
initial environment omits that key. The Bash allow marker checks its
absence in the child environment. The guard and loopback endpoints settled,
and all before and after probe inventories were empty. The fixtures preserve
the 30-second turn, 5-second cleanup and 75-second attach deadlines.

## Reproduction and input identity

Run from the checkout with GNU `timeout`, a local Docker socket and Buildx,
uv, and CPython 3.11.17. The image builds against the pinned official
Codex 0.166.0 and hashed fixture wheels. Execution uses nonroot Linux
amd64 containers with `--network none`, no host mounts and a synthetic
loopback model.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-executor-peer
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-executor-guard
```

The executable inputs correspond to `cf37ee582cf9071f333ce68ac47530f2440388c1`.
The fixture tree is `33571037ee0bc98561b70198b6e04db9d064efb5`, the Codex
provider tree is `d419ee8ffff805de71b932ba17cd7376925ff388` and the lock blob
is `881ac4770ed91a6b738b9cde54c42323191c2bdc`. Docker 29.9.2, Buildx 0.38.1
and LinuxKit 7.0.14 ran on a macOS arm64 host.

| Evidence | Wall time | Combined log SHA-256 |
| --- | --- | --- |
| Same-listener peer | 16 s | `0f7c1826973b580bdf516e401790b574cd368eedb34e4849e5111eee53f1d6c3` |
| Direct and guarded matrix | 171 s | `f36e8dc5525f0b530169b072ce3fa7cf30f6fb1314e8e950a1f401b9fd3a574e` |

The test-only delta at `1923b760a3c1bbada8e98adf188e02e0a05a20ca`
repairs collection of the scalar owner-name cases for both transports.
The changed unit test is outside the native Docker context. Strict Pyright
and all 918 selected Codex and architecture tests passed after that delta.

The measured scope is the exact private role recipe and its fixed Linux
fixtures. Production runtime ownership, relay and Policy qualification,
configured facts and activation remain with #184, #199, #549 and #772.
