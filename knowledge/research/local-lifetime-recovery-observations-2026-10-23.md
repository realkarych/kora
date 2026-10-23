# Local lifetime recovery observations

Checked 2026-10-23. This record describes implementation evidence for issue #247; it does not broaden the local cleanup contract.

## macOS process-group enumeration

Apple XNU source was inspected at commit `f6217f891ac0bb64f3d375211650a4c1ff8ca1ea`. The [libproc wrapper](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/libsyscall/wrappers/libproc/libproc.c#L40-L54) maps a native error to a zero return. Zero alone therefore cannot distinguish an empty group from failure. The [kernel implementation](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/proc_info.c#L337-L457) enumerates the selected list under the process-list lock and returns the number of copied bytes, including zero for an empty result.

The native reader clears captured errno immediately before the valid nonnull-buffer call and captures it immediately afterward. Only zero bytes with zero captured error is accepted as an empty enumeration. Negative, malformed, full-buffer, native-error and expired observations remain UNKNOWN. Native calls remain nonpreemptible; cooperative checks do not establish a hard syscall duration bound.

The real macOS test `test_already_reaped_empty_group_recovers_with_stale_errno` launches a separately parented process group, observes its actual parent reap it, seeds a previous EPERM value, and requires an affirmative empty enumeration and recovery settlement. This test passed locally. Linux and final CI results remain separately required; source interpretation is not platform execution evidence.

## Recovered authority and disposal

A recovered observer never owns waitpid or old capture descriptors. Each signal requires a fresh exact live original leader and a complete matching group observation. Once that leader exits or is reaped, surviving descendants remain UNKNOWN; no numeric-only group or member signal is attempted. An already-absent leader needs matching host, boot and namespace, two complete empty-group scans and no pending retained watcher. These process facts do not settle earlier journal or filesystem effects.

Successful release and uncertain observer disposal are separate operations. Disposal closes local watches and permanently disables that observer's authority while retaining the external UNKNOWN outcome. The original parent's descriptor scope and the exact invocation claim must be reconciled independently before whole-invocation closure.

## Terminal RPC close

A child inside the scope closed by `close(None)` cannot await a receipt proving its own death. The typed client receives transport-closed failure; the parent retains the close task and obtains the true final ClosureReport. Subtree close returns its actual report when the caller remains outside that subtree. EOF and lost replies initiate or join parent-owned closure and never certify success themselves. The separate-process terminal and EOF tests assert child exit and the parent's final report. Future command composition must coordinate private cooperative cleanup before termination instead of expecting a successful response after its own scope closes.
