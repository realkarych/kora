# Linux service and session survival probe

Checked on 2026-11-01 for [issue #316](https://github.com/realkarych/kora/issues/316),
following the native-mechanism prerequisite in the
[local-runtime plan amendment](https://github.com/realkarych/kora/issues/184#issuecomment-6341271587).

## Source and environment

The isolated fork branch `codex/probe-runtime-services-316` contains
`probe/service_survival.py` and `.github/workflows/probe-runtime-services.yml`.
The successful [run 41295347029](https://github.com/klukashka/kora/actions/runs/41295347029)
checked out `ae604ff6450e8dc871670fbc0d1d55831d57ec5e` on Ubuntu 24.04.5,
GitHub runner image `20261025.353.1`, using `/usr/bin/python3`.
The probe source SHA256 is
`061cbfd62ac17e0abe840adfc79f9a62130744e110e7282c8c1d313da06e5610`.
The workflow grants only `contents: read` and pins the checkout action.

This is a native mechanism experiment, not a run of the kora runtime or its
conformance suite. The successful probe does not complete #316.

## Observations

- Real system-bus reads returned `KillUserProcesses=false` and `Linger=true`
  for the runner user.
- A detached child remained in the user service's control group. After restarting
  that service with `KillMode=process`, the same child PID answered through the
  existing Unix socket.
- A system service with `PAMName=login` and `KillMode=mixed` created a real logind
  session. Its detached child occupied `session-2.scope`, separate from the
  original service's control group.
- Stopping the PAM service changed the session from `active` to `closing`.
  The same child PID still answered through the same socket after that transition.
- Cleanup observed the user unit as `loaded` / `failed` after explicit termination,
  and the PAM unit as `not-found` / `inactive`. The socket protocol requested child
  exit; the probe does not independently join or verify child disappearance.
  Child lifetime is bounded to 30 seconds, and the worker and transient service
  to 60 seconds. These bounds are fixture safeguards, not product timing claims.

## Failed runs and corrections

The [initial run](https://github.com/klukashka/kora/actions/runs/41295123712)
at `c969d3a4d7a05d0ec6c9e62fa20975b0b18d09f4` passed the restart scenario but
could not start the PAM service with `KillMode=process`.
[Systemd service validation](https://github.com/systemd/systemd/blob/v255/src/core/service.c#L611-L612)
requires `control-group` or `mixed` for a PAM-enabled service.

The [second run](https://github.com/klukashka/kora/actions/runs/41295224570)
at `702baab1eea2fb0b807b525fa83e7103a96eb54f` passed both survival scenarios
with `mixed` on the PAM fixture. Its cleanup failed when a second stop addressed
the already unloaded transient unit. The final probe accepts that stop failure
only after observing the exact `not-found` / `inactive` state and still fails
on other unreadable or active states. Earlier runs remain failed.

## Implementation implications and remaining evidence

Keep the service-restart and session-end fixtures distinct. Systemd documents
that a PAM service's main process remains associated with the service and the
session, whereas its children belong only to the session scope
([systemd.exec](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)).
The session-end integration fixture can therefore use a PAM wrapper that starts
a separate test worker in the session scope, with explicitly safe inherited
settings. This experiment does not authorize an exception to kora's refusal
of a worker service configured with `KillMode=mixed`.

Pending #316 evidence includes real kora startup refusals, manager/unit identity
confirmation, applicable service versus scope properties, ancestor findings,
preservation of recorded runner addresses across lingering changes, and exact
tool and fixture resource release on success, failure and cancellation. The
Linux and macOS implementation checks and final-revision CI remain required.
