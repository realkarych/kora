# Cooperative control-plane provider failure model

Checked: 2026-10-17. Method: primary Python documentation and static comparison with kora's async provider/standalone design. This is evidence for [ADR-0021](../../docs/decisions/0021-external-observations-outside-transactions.md), not an independent decision or an executable conformance result.

## What the runtime guarantees

- An event loop executes tasks on its thread until they yield. Blocking computation there delays other tasks and I/O. Network logging can block too. [Developing with asyncio](https://docs.python.org/3.11/library/asyncio-dev.html#running-blocking-code).
- Task cancellation requests an exception at a suspension opportunity; a coroutine can suppress it. A timeout wrapper therefore does not establish that arbitrary provider code has stopped. [Task cancellation](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.Task.cancel).
- `wait_for` cancels a timed-out task and waits for cancellation, so actual elapsed time can exceed the requested timeout. The scheduler must not wait inline for cancellation settlement. [Timeout behavior](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.wait_for).
- A thread future already running cannot be cancelled through `Future.cancel`. Offloading blocking work to threads does not create a termination boundary. [Concurrent futures](https://docs.python.org/3.11/library/concurrent.futures.html#concurrent.futures.Future.cancel).
- Async subprocesses permit explicit termination/kill and process exit observation. Buffered pipe capture can consume unbounded memory; process supervision also needs bounded I/O and lifecycle ownership. POSIX sessions/process groups help manage ordinary descendants but are not a hostile-code sandbox. [Async subprocesses](https://docs.python.org/3.11/library/asyncio-subprocess.html), [process creation and session options](https://docs.python.org/3.11/library/subprocess.html#subprocess.Popen).

## Implication for the MVP

A bounded in-process dispatcher can keep transactions short and reject stale observations while a conforming provider waits on a slow external service. Its liveness guarantee depends on nonblocking, cancellable installed providers, including import, initialization, hooks, parsing and logging. Representative conformance checks reduce mistakes without proving arbitrary code safe.

Process isolation is a stronger alternative only with a responsive supervisor, finite IPC/body/log limits, confirmed reaping before credit release and restart handling for surviving children. Expiring a database token does not prove physical termination. Neither process termination nor task cancellation retracts an external write already accepted remotely. Durable request identity and read-only reconciliation are still necessary.

These observations support the recorded cooperative MVP choice. They do not claim implementation, measured throughput, hard host-resource containment or fleet-wide concurrency enforcement.
