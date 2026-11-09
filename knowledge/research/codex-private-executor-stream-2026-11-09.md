# Selected Codex private executor stream

[#1230](https://github.com/realkarych/kora/issues/1230) tests the
[bound launch owner](../specs/codex-execution-profile.md#bound-native-launch)
with the two selected official Codex 0.166.0 recipes. The provider view
refuses `exec-server` before admission. The same local owner starts that
role and returns its stream to trusted fixture composition. The provider
view borrows only `app-server`.

## Reproduction

Run these commands from the checkout with GNU `timeout`, a local Docker
socket and Buildx, uv, and CPython 3.11.17.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-private-executor
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-native-context
```

Both commands build the pinned fixture and use nonroot Linux amd64
containers with no network or host mounts. The private-stream case
ends after initializing both roles. The direct
fixture still runs its synthetic model turn and three expected refusals
under its historical topology. Both commands use the existing 30-second
startup or turn, 5-second cleanup and 75-second attach deadlines.

## Results

| Case | Observed result | Wall time |
| --- | --- | --- |
| Selected private stream | Hidden selector refused before acquisition, private executor and borrowed app startup matched, exact pair reaped, both parent endpoints absent, private tree and one exact container removed. | 22 s |
| Historical direct context | Accepted synthetic-key turn, two legacy allocation refusals before acquisition and one cancellation with both roles owned. All four expected verdicts, private-tree removals and exact container removals passed. | 34 s |

The selected case observed four distinct socket endpoints with no
cross-role descriptor inheritance, exact initial environments and argv,
the bound checkout and verified live configurations. Of the two observed
initial environments, only app-server contained the synthetic key. Consumed
startup stdout was checked before decoding with a 512 KiB limit per stream. The bounded file witness checked at most 128 entries and
16 MiB under the private tree. The host attach capture used an eight-block
file-size limit, disabled Docker log retention and emitted only the exact
expected events. Native stderr is discarded by the gate; unread stdout
is closed with its owned stream. Before and after probe container inventories were empty.

The executable inputs were tested at
`b8a36a68a7d55aa424e601df1d62c0e466e8e256`.
The native fixture tree is `cccd5b32d8ed430e55789db35698ce8e90769bf0`,
the source tree is `0d6a5ee0e0940d6ff6f9ca32541fecfcd22f1fb3`, and the
lock blob is `881ac4770ed91a6b738b9cde54c42323191c2bdc`. Docker 29.9.2
and Buildx 0.38.1 used a Linux arm64 daemon and a macOS arm64 host.

| Combined log | SHA-256 |
| --- | --- |
| Selected private stream | `729356e6e4f62e1d27c85800d2247feb679392882a6e8a3e7d88bd751d3a62bb` |
| Historical direct context | `9111562b50c1ea8c12946d63ce596bd1b3201a13d937485b99b145ee82b0b2ac` |

On macOS 27.2 arm64 with CPython 3.11.17, the focused bound-native, Codex
and architecture run passed 1,914 tests. The portable stop test observed
a registered private executor acquisition before stream delivery, a
refused competing start, and a previously borrowed app stream. It
confirmed that stop returned no private stream and both gates settled.
Strict Pyright and Ruff checks passed for the changed source and tests.

The selected case qualifies local startup and stream custody. Production
relay routing and post-forward settlement remain under #184. Policy
handoff and covered effects remain under #199 and #772. Server admission
and activation still depend on the qualified configured facts under #549.
