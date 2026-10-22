# Research: the local runtime's OS mechanisms on GitHub's Linux and macOS runners

Checked on **2026-10-22** on GitHub-hosted runners. The probe script `probe/probe_local_runtime.py` and its workflow `.github/workflows/probe-local-runtime.yml` ran on branch `probe/local-runtime-mechanisms` of the fork `klukashka/kora`. Every row below comes from run `39736834054` at commit `6e26c5b62ee72777bb22ba130a25c8892b2ca369`; the earlier run `39736590301` at commit `56d456f5f31152fdb263580115c9cdd1688e7d77` used a group scan and a logind read that commit `6e26c5b` corrected, and none of its results is used. Its logs: `gh run view 39736834054 --repo klukashka/kora --log`. Both ran with CPython 3.11, standard library and `ctypes` only:

- `ubuntu-24.04`: Linux 7.0.0-1017-azure, x86_64, glibc 2.39, CPython 3.11.17, systemd user manager present;
- `macos-15`: macOS 15.7.9, arm64, CPython 3.11.9.

The local-runner probe of [2026-10-19](local-runner-adoption-probe-2026-10-19.md) ran on macOS only. This probe checks the mechanisms that the implementation plan of issue #184 relies on, on the CI machines that will run its tests.

## Linux

| mechanism | observed |
| --- | --- |
| `os.pidfd_open`, `signal.pidfd_send_signal` | work; `os.waitid(P_PIDFD, …, WEXITED \| WNOWAIT)` reports the exit status and leaves the zombie (`/proc/<pid>/stat` state `Z`) |
| `PIDFD_SIGNAL_PROCESS_GROUP` (flag `1 << 2`) | supported |
| `SO_PEERCRED` and `SO_PEERPIDFD` (option 77) | both work on a Unix socket pair; an accepted listening socket was not probed |
| `openat2` with `RESOLVE_BENEATH \| RESOLVE_NO_SYMLINKS` (syscall 437) | a plain path opens; a path through a symlinked directory fails with `ELOOP`; `..` fails with `EXDEV` |
| `renameat2(RENAME_NOREPLACE)` | refuses an existing target with `EEXIST` |
| `prctl(PR_SET_CHILD_SUBREAPER)` | an orphaned grandchild is reparented to the subreaper and listed in `/proc/self/task/<tid>/children` |
| process-group scan of `/proc/*/stat` | finds a group's members, with a killed leader held unreaped in state `Z`, splitting the command name at the last `)` |
| user manager | `/run/user/1001` and its `systemd/private` socket exist and `Linger=yes` before any preparation; `KillUserProcesses` is `false` (read through `busctl`) |
| `systemd-run --user --scope --collect -p Delegate=yes` (informational: issue #194 dropped the delegated scope) | starts a scope under `user@1001.service/app.slice`; `cgroup.kill` is writable and a child cgroup can be created |
| unprivileged user and pid namespaces (`unshare --user --pid --fork`) | allowed |
| `fdatasync` of a 512-byte append | median 0.20 ms, p95 0.30 ms before the host fixtures were prepared; median 0.20 ms, p95 0.44 ms after |
| host fixtures | a 16 MiB `tmpfs` mounts with `sudo`, and writing to it without `sudo` fills it to `ENOSPC`; a root-owned 0700 directory is created; `loginctl enable-linger` succeeds |

## macOS

| mechanism | observed |
| --- | --- |
| `LOCAL_PEERPID` and `LOCAL_PEERCRED` (`SOL_LOCAL`) | both work; `xucred` version 0 with the peer uid |
| `os.waitid` | **absent** in CPython 3.11.9 on macOS |
| `kqueue` `EVFILT_PROC` with `NOTE_EXIT \| NOTE_EXITSTATUS` | fires on exit without reaping; registering after the child has exited fails with `ESRCH` |
| `renamex_np(RENAME_EXCL)` | refuses an existing target with `EEXIST` |
| a killed group leader held unreaped | `proc_listpids(PROC_PGRP_ONLY, pgid)` still lists it; `proc_pidinfo(PROC_PIDTBSDINFO)` on it fails with `ESRCH`; `killpg(pgid, 0)` succeeds while another member lives and fails with `EPERM`, not `ESRCH`, once only the zombie leader is left |
| `F_FULLFSYNC` of a 512-byte append | median 0.92 ms, maximum 13.1 ms |
| `confstr(_CS_DARWIN_USER_TEMP_DIR)` | a 49-byte path (`confstr` returned 50, counting the terminating NUL); with the probe's 16-character placeholder socket name below `kora/`, the socket path is 70 bytes, within the 104-byte `sun_path` |
| `ioreg -rd1 -c IOPlatformExpertDevice` | returns `IOPlatformUUID` in about 12 ms |
| host fixtures | without `sudo`, a 16 MiB APFS RAM disk created with `hdiutil` fills to `ENOSPC`; with `sudo`, a `root:wheel` 0700 directory is created |

## Not probed

Peer attestation on an accepted listening socket and from the connecting side, the pid read from a pidfd's `fdinfo`, the boot-relative process start time on macOS (`proc_pid_rusage` `ri_proc_start_abstime`), and the exit status in the data of a `NOTE_EXITSTATUS` event, the service-manager rules of the local runtime (`KillMode=process` survival, the refused `KillMode`, `OOMPolicy` and `ManagedOOM*` settings) were not probed; the runner work item verifies them on CI.

## Consequences for the implementation plan of #184

- **Exit detection on macOS** cannot use `os.waitid` with `WNOWAIT`. The `kqueue` event fires without reaping; that its data carries the exit status with `NOTE_EXITSTATUS` was not observed, so the reaper's reading of the status from the event is an assumption the runner work item verifies, with a single reap when it releases the leader.
- **Group emptiness on macOS** cannot use `killpg(pgid, 0)`, which reports `EPERM` for a group holding only a zombie leader. The plan's rule holds: list the group with `proc_listpids` and count a member whose `proc_pidinfo` fails with `ESRCH` as not live.
- **A `kevent` registration that fails with `ESRCH`** must be followed by an immediate status check, as the plan states.
- **The probed Linux mechanisms** all behave as the plan assumes on the CI kernel; `openat2`'s `EXDEV` and `ELOOP` are the refusals to map.
- **The CI host fixtures** the plan needs, a small filesystem and a foreign-owned directory, can be prepared on both runners; the Linux runner already lingers.
- **Unprivileged user namespaces** are available on the Linux runner, so the different-pid-namespace leg of `test_process_exit_evidence_requires_same_host_and_pid_namespace` can also run against a real namespace, in addition to the injected identity source.
