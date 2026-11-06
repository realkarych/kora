# macOS native I/O fixture feasibility

Checked: 2026-11-06. Repository baseline: `b9e3f2cc`.

## Purpose and evidence limits

This record preserves the FSKit provisioning and NFS recovery investigations for
[#830](https://github.com/realkarych/kora/issues/830). It records feasibility
findings, not a new runtime contract or a working fixture. No bundle installation,
mount, privileged experiment or native qualification was performed for this record.

The inspected local SDK was macOS 15.4. Public online documentation can include
newer APIs; their presence does not establish availability on macOS 15. Source
inspection and independent review do not substitute for execution on the target
macOS 15 runner. No measured failure duration is claimed.

The implementing work remains open: [native fixture #778][fixture],
[fixed-file FSKit fixture #802][fixed-file] and [provisioning #827][provisioning].
The [separate diagnostic-storage proposal #800][storage-proposal] is not evidence
that the route or fixture exists. This record does not qualify the collector,
resolve its dependency on native evidence, or close those implementation issues.

## Constraints retained

The investigation keeps the existing requirements rather than replacing an
unavailable positive test with a successful refusal:

- `state_root` and coordination remain on accepted local filesystems. Neither
  user-space nor network storage is newly admitted for that authority.
- The diagnostic fixture must exercise real regular-file I/O, observe the held
  operation and prove actual release. A callback, pipe or cooperative delay is
  not evidence of a blocked filesystem operation.
- Borrower settlement retains its original five-second bound. Test, job and
  enclosing cleanup cutoffs remain unchanged; phase transitions do not reset them.
- Cleanup targets only resources whose ownership and native identity are known.
  Ordinary unmount and actual resource closure remain required where applicable.
- Uncertain outcomes retain durable ownership and logical capacity. Owner death,
  lease availability, signal delivery or an absent control connection does not
  establish closure.
- Forced unmount, killing system FSKit daemons, private registry edits, global
  resets, reboot and destruction of the test host are not passing cleanup.
- Native owner/server loss and partial acquisition remain required cases. A
  successful healthy-path demonstration cannot replace them.

These constraints come from the linked implementation plans and the
[approved cleanup contract][cleanup-contract]. Their inclusion here does not
create another source of normative runtime requirements.

## FSKit: what can be observed

The inspected SDK 15.4 `FSClient.h` exposes discovery through
`fetchInstalledExtensions`; `FSModuleIdentity.h` supplies bundle identity, URL
and readonly enabled state. The corresponding public
[FSClient documentation][fsclient] and [enabled-state API][enabled] explain those
observations. A successful response identifying the reviewed installation with
`enabled == false` is evidence of observed disabled state, not a session join.

The [approved cleanup order][cleanup-contract] is:

1. Disable the exact owned installation.
2. Establish no-new-session exclusion and actual settlement.
3. Delete only the verified owned bundle inventory.
4. Confirm absence of the original registration and close remaining owner handles.

The order deliberately permits an unresolved intermediate state in which bundle
files are gone but registration remains. It does not permit deletion before
session settlement. Partial removal cannot automatically continue when a fresh
disable/session witness is no longer obtainable.

Known child identities can use the existing native process-token and kqueue exit
observation in `tests/plugins/storage/pressure_native.py`. This proves exit of
an identified process; it does not enumerate all framework sessions or exclude
an extension launch already queued by the system host.

After authorized bundle removal, candidate registration observations are a
successful FSClient inventory and the complete PluginKit inventory, including
all versions and instances. The inspected `pluginkit(8)` documents
`-m -A -D -vv --raw -p com.apple.fskit.fsmodule` for that inventory scope.
Exact decoding and no-match behavior still require target-platform qualification.
Errors, truncation and unavailable output must remain unknown, not an empty set.

## FSKit: the missing barrier

Two obligations are not supplied by the inspected public interfaces:

1. A complete account of already admitted sessions or invocations that may still
   access the installation.
2. A barrier preventing later sessions, including already queued launches.

The examined alternatives have narrower scope:

- `FSUnaryFileSystem.unloadResource` completes one resource's lifecycle.
- `FSVolume.deactivate` concerns one volume; the provisioning-only stage creates
  no volume and cannot use its lifecycle as a whole-extension witness.
- `AppExtensionProcess.invalidate` concerns the caller's connection. Apple's
  [extension hosting documentation][extension-hosting] allows other connections
  to keep the extension alive. The fixture does not own the system host's connections.
- `didFinishLoading` can help identify an incarnation after startup. It cannot
  account for a queued launch before the extension's instrumentation executes.
- An extension-local admission flag can refuse callbacks after it runs, but
  cannot stop the earlier launch or join the system host's connections.

These are scope deductions from the inspected SDK declarations, not native
experiments. No supported complete barrier was established. A finite quiet
process snapshot, repeated empty inventory or local counter cannot supply the
missing completeness argument.

The provisioning route itself also remains unqualified: supported signing,
actual enablement on macOS 15 and unattended hosted execution are not established.
The SDK entitlement and discovery APIs alone do not prove those steps succeed.

The appropriate disposition is blocked implementation, not a universal claim
that macOS can never support such a barrier. Revisit when a concrete supported
host mechanism covers both obligations, before implementing a polling substitute.

## NFS: alternative considered

A directly owned user-space NFSv3 server would avoid the system extension
registration lifecycle. A private loopback export and a controlled response gate
could hold a native regular-file WRITE. The [Apple mount manual][mount-manual]
documents `sync` as waiting for a server response rather than merely queuing I/O.
That is useful source support for a candidate held-write observation, not proof
of the actual syscall, bytes or recovery behavior on the target runner.

[UNFS3][unfs3] was examined as an existing server candidate. Its
[manual][unfs3-manual] describes explicit NFS/MOUNT ports, TCP-only operation and
disabling portmapper registration. This does not qualify loopback isolation,
build dependencies, mount permissions or this workload on macOS 15. No dependency
or network-backed diagnostic route was adopted.

## NFS: recovery sequence examined

The restart-based design would need all of the following:

1. Retain exact export, backing, endpoint and native server identity in bounded
   ownership state outside the mount. Stop new fixture admission on recovery.
2. Establish actual server death. Preserve the mount and backing; do not infer
   death from control-channel loss or allow competing server incarnations.
3. Restore valid filehandles and the original owned endpoint. Preserve genuinely
   stable data and use truthful WRITE/COMMIT verifier semantics after volatile
   state loss. Account for lost replies and repeated operations.
4. Wait for the kernel client to reconnect and replay. Establish the original
   syscall outcome, relevant byte/error evidence and actual borrower closure
   within the original five-second settlement cutoff.
5. Perform ordinary unmount under the existing enclosing cleanup cutoff while
   the service remains available for any required filesystem operations. Confirm
   mount absence before closing server/control resources and removing backing.

Steps 1–3 provide prerequisites for recovery, not a bound on step 4. Server
readiness is not evidence that the kernel client completed reconnect, replay,
writeback or closure. Ordinary unmount remains unestablished if those operations
are outstanding.

[RFC 1813's COMMIT discussion][nfs-commit] requires replay of uncommitted data
after an unexpected verifier and leaves retransmission timing to the client
implementation. Its [duplicate-request discussion][nfs-replay] also distinguishes
recovery correctness from merely repeating an operation after a lost reply.
Restoring an endpoint does not give the fixture control over client scheduling.

The Apple manual documents potentially indefinite outstanding operations when
the server is unavailable. The examined options do not supply the missing bound:

- `timeo` sets an initial retransmission interval, not an end-to-end recovery limit.
- `retrans` controls the retry count for soft mounts.
- `soft` and `intr` change error/interruption behavior; those errors alone do not
  prove closure of all native effects or successful ordinary unmount.
- `deadtimeout` leads to forced unmount, which does not meet the cleanup criterion.
- `sync` supports response waiting, not bounded reconnection or cleanup.

These documented semantics support rejection of the proposed mechanism. They
are not measurements of the target runner or proof of impossibility for every
possible NFS implementation.

## Why a surviving frontend does not close the failure case

A persistent RPC frontend could preserve client connections while replacing a
backend. It would then be the actual protocol-serving process. A backend-loss
test would leave the frontend's own death uncovered and narrow the failure model.

A frontend that can independently serve recovery is a new server design, not
an already established guardian adapter. It still needs truthful filehandle,
replay and durability behavior, and a mechanism covering its own loss. No size
estimate or bounded-recovery guarantee is established for that alternative.

## Disposition and conditions for revisiting

Reject the current restart-based NFS candidate for implementation under the
unchanged settlement and ordinary-unmount requirements. A fast successful restart
alone would not resolve the identified native-client lifecycle gap. Retained
`UNRESOLVED` ownership is correct failure accounting, not a passing cleanup result.

Revisit NFS only with a concrete mechanism that covers death of the actual
RPC-serving process, bounded borrower closure and ordinary unmount. Revisit
FSKit only with a supported complete session and launch-exclusion boundary.
Any future alternative must retain the original failure cases and receive its
own complete implementation estimate; unknown recovery code is not free reserve.

Native qualification remains pending for both routes. The prior provisioning
estimate is conditional, and no complete implementation within 1,000 lines is
claimed here. The collector's separate size exception does not apply to fixtures.

[fixture]: https://github.com/realkarych/kora/issues/778
[fixed-file]: https://github.com/realkarych/kora/issues/802
[provisioning]: https://github.com/realkarych/kora/issues/827
[storage-proposal]: https://github.com/realkarych/kora/pull/800
[cleanup-contract]: https://github.com/realkarych/kora/issues/827#issuecomment-6680756230
[fsclient]: https://developer.apple.com/documentation/fskit/fsclient
[enabled]: https://developer.apple.com/documentation/fskit/fsmoduleidentity/isenabled
[extension-hosting]: https://developer.apple.com/documentation/extensionfoundation/adding-support-for-app-extensions-to-your-app
[mount-manual]: https://github.com/apple-oss-distributions/NFS/blob/main/mount_nfs/mount_nfs.8
[unfs3]: https://github.com/unfs3/unfs3
[unfs3-manual]: https://github.com/unfs3/unfs3/blob/master/unfsd.8
[nfs-commit]: https://www.rfc-editor.org/rfc/rfc1813.html#section-3.3.21
[nfs-replay]: https://www.rfc-editor.org/rfc/rfc1813.html#section-4.5
