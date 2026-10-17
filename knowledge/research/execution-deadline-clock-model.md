# Execution deadline clock and stop-policy evidence

Checked: 2026-10-17. Method: primary Python, Linux man-pages and Docker documentation, compared with the existing runtime/common-executor lifecycle. This supports [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md); it is neither a decision nor executable conformance evidence.

## Clock properties

- Python's monotonic clock cannot go backward, is unaffected by system-clock updates and has an undefined reference point. Use local elapsed differences, not persisted cross-host readings. [Python time.monotonic](https://docs.python.org/3/library/time.html#time.monotonic).
- A scheduled sleep may return arbitrarily later because of system scheduling. A timer API does not establish a finite production scheduling-latency bound; configuring such a number would not prove it. [Python time.sleep](https://docs.python.org/3/library/time.html#time.sleep).
- On Linux, `CLOCK_MONOTONIC` does not include suspended time; the distinct `CLOCK_BOOTTIME` does. A generic monotonic timer therefore does not prove wall-clock expiry through host suspension. This is Linux evidence, not a claim about every macOS clock. The common contract explicitly excludes unsupported suspension/control failure rather than adding platform clock branches. [Linux clock_gettime manual](https://man7.org/linux/man-pages/man2/clock_gettime.2.html).

## Stop policy and evidence

Docker describes an initial stop signal followed by forced kill when its stop timeout elapses. Pass the remaining grace derived from the original absolute escalation threshold rather than relying on a fresh default timeout after reconnect. Sending the request or timing out the control call is not an observed termination receipt. [Docker container stop](https://docs.docker.com/reference/cli/docker/container/stop/).

The [cooperative-provider evidence](cooperative-provider-failure-model.md) separately explains that cancellation need not settle a coroutine, a worker thread is not a stop boundary and process termination does not retract a remotely accepted request.

## Consequences for the selected contract

Persist the earliest UTC scope deadline and the supported finite clock/conversion uncertainty. Convert conservatively to local nominal interruption and escalation thresholds; later sampling only shortens installed timers. Keep observed expiry sticky across clock correction/adoption, and never grant a fresh grace after delayed delivery. No timer or deployment assumption establishes exact physical stopping, rollback of candidate/MCP effects, closure of a pre-authorized remote write or released capacity.

The useful partition guarantee assumes functioning local supervisor/runtime control and the stated clock model. Local trusted process behavior, protected Docker domain behavior, metadata transitions and fake-protocol decisions require separate future evidence. Real-runtime conformance belongs to implementation; documentation CI only checks repository documents, diagrams and schemas.
