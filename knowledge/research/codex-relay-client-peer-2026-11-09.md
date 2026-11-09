# Selected Linux Codex relay client peer

[#1223](https://github.com/realkarych/kora/issues/1223) checks whether the
selected relay can distinguish the pinned app-server from a second same-user
process at one listener when both present the correct registration bearer.
The [earlier TCP observation](https://github.com/realkarych/kora/issues/184#issuecomment-6718403077)
used a competing listener on a different port.

## Result

The fixture starts the real app-server and exec-server from official Codex
0.166.0, then binds the spawned app-server's pidfd before executor
registration. The fixture coordinator connects to the same relay address
with the exact bearer and a valid WebSocket upgrade. Its connected inode
is present in its own descriptor view and absent from the selected live
app-server's view. The relay refuses this request before upgrade or slot
admission and observes zero writes to native stdio. The real app-server
subsequently connects to the same listener, matches the expected peer,
finishes the fixed Bash turn and creates the exact marker.

The witness checks the accepted socket's kernel descriptor and loopback
tuple against one established TCP row in each direction. It observes
client-inode membership in both processes twice while the app-server
pidfd stays live. Missing, nonunique, changing or inaccessible relevant
observations fail the fixture. The table is bounded to 256 KiB and
1,024 rows and each descriptor observation to 256 entries. The original
cutoff is checked again before reserving the slot. [Linux TCP table format](https://docs.kernel.org/networking/proc_net_tcp.html)

The native roles were both reaped. The model and relay endpoints settled.
The witnessed nonzero inodes from both connection pairs were absent after
closure. Residual TCP rows without those inodes are compatible with closure. The exact container was removed
and both probe inventories were empty. Diagnostics use fixed outcomes.

## Reproduction

Run with GNU `timeout`, a local Docker socket and Buildx, uv and
CPython 3.11.17. The build can fetch the pinned official executable and
hashed fixture dependencies. Execution uses a nonroot Linux amd64
container with `--network none`, no host mount and synthetic loopback
model authentication. The existing turn, cleanup and attach deadlines
are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-executor-peer
```

Executable revision `0f3e46c4c1098bec1c2a65782f47d06a6b495d47`, fixture
tree `3f6fed3dd88eb97014084446613057c08c3c44aa`, Codex provider tree
`0a9f1d0daf1e42b707f3e71e4a7ab127e3382bed` and lock blob
`881ac4770ed91a6b738b9cde54c42323191c2bdc`. The run used Docker 29.9.2,
Buildx 0.38.1 and LinuxKit 7.0.14 on a macOS arm64 host. It passed in
18 seconds. Combined log SHA-256
`0580d0b1f77068e48a8b02869abfbfa7e1ee79dff9ce4395e435d4fb741249bf`.

The first run at `b61c0e09d7466607b071f644913adb9c7257dd46` failed to
establish the decoy observation. It enumerated descriptor entries to exhaustion before reading their links,
which can close the scan's own descriptor. The
corrected fixture reads each link while the scan is open. That failed
run also reaped both children and removed its exact container. Combined
log SHA-256 `ce1547ae72fae4b910e5dc6aa39d3ee6adcb4403827aed6631384feb8f0e3448`.

The existing `--check-selected-executor-guard` matrix passed all
18 cases at the same executable revision with peer witnessing disabled.
It reported 18 direct role-pair reaps and 18 exact removals, with empty
before and after inventories. Wall time was 173 seconds. Combined
log SHA-256 `afff8174273768c1d386d6254b3093fba9f1fd4de985617f5dd787fbee91b026`.

## Limits

These are bounded connected-socket observations in one cooperative Linux
fixture. Production peer custody, continuous identity, descriptor transfer,
reconnect, hostile same-user isolation and Policy authority have separate
qualification. The documented Linux table is a fixture observation
interface. Production interface selection remains with [#184](https://github.com/realkarych/kora/issues/184)
under the [selected profile](../specs/codex-execution-profile.md#native-connection-capability).
