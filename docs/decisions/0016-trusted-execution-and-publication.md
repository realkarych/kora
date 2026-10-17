# ADR-0016: Trusted lifecycle state and a separate publication phase

- Status: architectural direction accepted, 2026-10-17; document pending owner review
- Amended by: [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md), 2026-10-17 (execution uncertainty, dispatch closure and retained reservations); wording pending owner review
- Deciders: project owner
- Related: [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0007](0007-attempt-model-and-retries.md), [ADR-0009](0009-target-repository-trust-boundary.md)
- Resolves: [issue #51](https://github.com/realkarych/kora/issues/51); credential acquisition and principal identity remain [issue #43](https://github.com/realkarych/kora/issues/43)

## Context

The candidate workspace contains code the agent can execute and modify. Keeping the resumable journal, plan scripts and credential helpers in that tree lets candidate code replace control state. Running collection in the candidate's Git working copy also lets its configuration execute code with the publisher's authority: disabling hooks does not disable Git filters. Cleaning the main harness process's environment does not isolate surviving descendants.

The runtime and VCS must still compose without knowing each other's implementation, including on infrastructure with no filesystem shared with the worker. Publication credentials must be acquired after a session lasting hours and again after worker recovery, without exposing a parent credential to candidate code.

## Decision

**One attempt, separate authorities.** A lifecycle supervisor owns preparation, the resumable journal, plans, phase checkpoints and publication. The harness, commands, tools and their descendants form the candidate execution domain. On a protected runtime, OS/runtime authority prevents that domain from reading or writing the control directory, publisher storage or publication channel. A path outside the workspace is insufficient without that authority. Candidate messages are observations; only the supervisor creates authoritative preparation and publication results.

The harness adapter runs on the supervisor side and launches the harness binary through a runtime-supplied process launcher. SDK messages, output and hook requests cross that narrow channel. The launcher carries argv, cwd, constructed environment, stdio and termination; it gives candidate code neither the worker's environment nor the supervisor's control authority. A runtime does not implement separate integrations for each harness. Complete launch, hooks, interruption and nested-tool behaviour must be demonstrated by the harness/runtime conformance cases before a protected combination is supported.

**Prepare, execute, seal, inspect, authorize, publish.** Preparation creates the workspace from trusted provider settings and records its base before candidate code starts. Checkout helpers and credentials stay in the preparation authority and are removed before execution. After execution, the supervisor stops the entire candidate domain, including descendants, and creates a private immutable handoff without writable aliases. If a protected runtime cannot establish termination and sealing, publication fails. Path replacement, symlinks and special files must not redirect trusted reads or writes.

The VCS plans preparation of a publication from that handoff in fresh trusted metadata. The VCS decoder emits a complete, bounded provider-neutral content index of all newly published versions and revision metadata; the trusted scanner reads its immutable raw content without giving scan secrets to decoder subprocesses. kora inspects the exact resulting revision and fixes its destination before any publication credential is delivered. The worker then obtains fresh publication authorization through the VCS integration and calls `Runtime.finalize(handle, authorization)`. That call can publish only the prepared result for that attempt. It cannot start another session, select another revision or destination, or replace inspection with candidate assertions. There is no new central publisher or mandatory credential service; workers still connect outbound only, and the supervisor has no API credential.

**Preserve Git history.** The reference VCS transfers candidate commit objects into a fresh publisher repository without inheriting candidate configuration, hooks, helpers, attributes, alternates or writable object storage. Existing candidate commits retain their identities and parents. Uncommitted changes become one additional commit prepared before authorization. Its complete identity, including metadata, is persisted once; recovery never recreates it with another timestamp. Checks cover all newly published reachable history, including secrets added and later deleted, as well as the final tree. The publish operation uses the exact inspected object id and the platform's deterministic attempt branch, never a candidate-selected remote or branch. An author who explicitly grants candidate code a code-host credential also grants it direct external capabilities; this boundary cannot revoke that grant.

**Recover the prepared result.** The runtime keeps a durable private checkpoint binding the attempt and workload digest, plans, trusted base, phase, sealed input digest, prepared revision, inspection result, destination and publication outcome. Secret values are not checkpointed. `lookup` and `adopt` recover this state and the protected journal. Repeated finalization returns the same result. An unknown push outcome is reconciled against that exact revision: equal reconciles content, and confirmed absence permits the same non-retired publication only after earlier dispatchers and their operations are closed (ADR-0017); an unexpected revision is a conflict. No force overwrite is allowed. A lost sealed input or prepared result is an infrastructure error, with terminal failure deferred until execution and dispatcher/publication closure is established (ADR-0017), not permission to regenerate a different result under the same identity. Published branches are not rolled back.

**Local is for trusted development and tests.** In the MVP, `local` uses ordinary host processes on Linux and macOS without a sandbox, separate OS identity, administrator permission or extra trust opt-in. It follows the same lifecycle and publication code, keeps control files outside the candidate tree and uses fresh publisher metadata, but provides no security boundary against hostile same-user code. Process groups are lifecycle management. Selecting `local` is sufficient in a trusted installation; an installation requiring protected execution rejects its declared weaker capability. This is a runtime property, not a feature confined to standalone mode, and the SQLite/local first slice stays simple.

Docker uses separate candidate and publisher containers under one logical attempt handle; trusted control state stays with the host-side supervisor. The candidate has no control mounts, Docker socket, privileged mode or host PID access. The whole candidate container must be confirmed stopped before private transfer and publication. Publisher containers mount only the sealed input and trusted publication state. These are required design properties, not security claims proved by the presence of a container. An independent runtime must establish equivalent authority, containment and transfer properties or reject protected execution.

**Capabilities are enforced.** `protected_execution` states whether the runtime enforces this complete boundary. Deployment policy `require_protected_execution` rejects a runtime that lacks it with a structured validation/configuration error. It is independent of network enforcement and credential proxy support. Local declares false; Docker can declare true only with the specified isolation configuration and conformance evidence. Protocol fakes prove composition and recovery, not OS security.

## Alternatives considered

- Keeping authoritative files under `.kora/` and removing helpers before execution does not protect the journal or publisher from later candidate writes.
- Disabling hooks or inspecting only the final diff does not neutralize executable Git metadata or secrets in intermediate commits.
- A publisher under another process in the same candidate domain leaves filesystem, credentials and surviving descendants in that domain's reach.
- Squashing the final file tree into one platform commit simplifies transfer but loses the candidate's commit history. Preserving that history is the chosen behaviour.
- Requiring a sandbox or VM for local development would add installation work before the first slice. Local remains a deliberately trusted environment.
- A mandatory token broker or separately deployed publication service adds operational scope without establishing the runtime boundary. Credential backends remain within the existing VCS integration.

## Consequences

- The runtime contract gains late finalization and protected recovery state. VCS plans consume an immutable handoff and yield a prepared publication; they do not perform credentialed work in the candidate repository.
- Journal integrity authenticates platform lifecycle facts, not the completeness or honesty of a compromised harness's observations. Harness-derived usage and tool-policy coverage retain their documented limits.
- Result authority and lease epochs still fence API writes. They do not atomically revoke an external push already in flight; partition, ownership and cancellation policy remain separate from this decision.
- Detailed DTOs, failure mapping, executor placement and future conformance cases are in [protocols.md](../../knowledge/specs/protocols.md) sections 2, 4 and 13. Recovery is in [scheduler.md](../../knowledge/specs/scheduler.md) section 3.
- The repository has no runtime implementation. Documentation/schema CI validates this change; the specified adversarial process and Git tests are future implementation release gates, not evidence that isolation has been tested.

## Where it is applied

- [Functional requirements](../requirements/functional.md): FR-5.2, FR-8.1, FR-8.2 and FR-8.8.
- [Non-functional requirements](../requirements/non-functional.md): NFR-4.1 and NFR-4.7.
- [Runtime/VCS contracts and conformance](../../knowledge/specs/protocols.md): sections 2, 4 and 13.
- [Recovery](../../knowledge/specs/scheduler.md), [publication provenance](../../knowledge/specs/data-model.md), [public progress/error reporting](../../knowledge/specs/api-and-agent-surface.md).
- [Detailed architecture](../../knowledge/architecture/detailed.md), [contributor introduction](../../knowledge/architecture/internals.md) and [console](../console.md).
