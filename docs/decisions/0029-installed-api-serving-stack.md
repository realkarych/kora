# ADR-0029: Keep the installed API server optional

- Status: implementation plan accepted, 2026-11-22
- Reviewed: line by line and accepted by the project owner, 2026-11-22
- Decider: project owner
- Refines: [ADR-0013](0013-implementation-stack.md), serving dependencies
- Related: [ADR-0002](0002-ai-first-api-and-thin-clients.md), [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0026](0026-package-layers.md), [issue #1755](https://github.com/realkarych/kora/issues/1755)

## Context

Kora has an ASGI application and an installed CLI. The installed API command needs a server that can run the application and let Kora own startup, signals and database settlement. Only server installations need the serving dependency. Ordinary local SQLite serving and future qualified PostgreSQL have different deployment requirements.

## Decision

The single `kora` distribution exposes the optional `serve` extra. It selects the pinned Uvicorn server and the existing CEL extra required by ADR-0013. The base wheel retains its existing dependency requirements. The stack table and lockfile record the selected dependency versions.

The installed `kora api` command runs one foreground process, one selector event loop and one IPv4 loopback HTTP listener. Kora supplies the ASGI instance and requires lifespan startup. It owns signal handling and database settlement on that same event loop. If the drain deadline expires, the process records unfinished settlement and holds the same loop and original resource ownership until the work settles. It returns exit 70 only after that settlement.

Ordinary serving checks the operator-prepared SQLite schema before binding. It serves the public health route and protects the OpenAPI route with the installed static-token provider. Migration and token provisioning remain explicit setup. This SQLite path applies on supported Linux and macOS without a native process barrier.

The PostgreSQL preflight has a separate native Linux x86_64 process boundary under the [qualified PostgreSQL launch profile](../../knowledge/specs/execution-custody-recovery-priority.md#qualified-postgresql-api-launch-profile). It checks the supported profile before using the standard library to install the kernel barrier. This preflight refuses before any database connection or HTTP bind. The shared pool, boot and recovery authority, original-writer evidence and deployment qualification independently gate future PostgreSQL serving.

### Private Unix HTTP for installed selection evidence

The [catalog architecture approved under #1640](https://github.com/realkarych/kora/issues/1640#issuecomment-7082305733) specifies one additional private Unix-domain HTTP listener for future SQLite installed selection evidence. Ordinary SQLite serving binds only the existing IPv4 loopback listener and retains its behavior. A trusted, explicitly provisioned installation may also bind the private path after separate activation approval and startup verification. The original foreground serving owner owns both paths on the same selector loop. The private listener uses the same Worker HTTP operations and service authorization, with one request per accepted connection. The API binds the native peer observation on that connection to the independently verified original Worker launch and to the exact request.

The [installed selection specification](../../knowledge/specs/installed-execution-selection.md#bind-the-actual-worker-and-the-original-claim) governs the bounded private connection, proof before payload and actual closure. Implementation and activation of the private listener require separate approval. The original Writer and physical qualification keep their existing gates.

## Alternatives considered

- A mandatory Uvicorn dependency would add the server to every CLI and client installation.
- A generic ASGI launcher would give another process ownership of the listener and shutdown lifecycle.
- Applying native Linux controls to ordinary SQLite would restrict its supported deployments to Linux without a qualified PostgreSQL resource to protect.

## Consequences

Operators install `kora[serve]` to serve a prepared local database. Existing CLI and client installations keep their dependency baseline. A future PostgreSQL rollout requires the runtime and qualification evidence named by the custody specifications.

## Where it is applied

- [Installed API architecture](../../knowledge/architecture/current.md#http-api)
- [HTTP API stack](../../knowledge/architecture/detailed.md#stack-per-component)
- [Initial ASGI application contract](../../knowledge/specs/api-and-agent-surface.md#initial-asgi-application-issue-541)
