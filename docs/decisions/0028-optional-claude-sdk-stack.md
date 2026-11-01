# ADR-0028: Keep the Claude vendor SDK stack optional and separate from kora's HTTP client

- Status: accepted, 2026-11-02
- Reviewed: by the project owner, 2026-11-02
- Decider: project owner
- Related: ADR-0004, ADR-0013, ADR-0026

## Context

The Claude adapter requires the vendor Python SDK. Its MCP dependency brings a second HTTP client implementation, while kora's shared API client uses HTTPX. Installation and import boundaries must remain explicit.

## Decision

The single `kora` distribution exposes an optional `claude` extra for the pinned Claude SDK and its reviewed dependency closure. Server installations that only inspect native configuration do not require this extra.

The vendor SDK may use its own MCP and HTTP client dependencies. kora's shared API client continues to use HTTPX; kora does not route API traffic through the vendor client or wrap a second general-purpose HTTP client.

Pure provider-owned configuration and declaration modules do not import the operational SDK. The harness execution integration imports and uses it. Installing both dependency stacks in one environment does not establish process isolation or effect-free vendor imports.

Exact versions, dependency purposes and compatibility evidence belong in the lockfile, stack table and dated research. A change to the approved dependency closure requires review; this decision does not authorize future package upgrades or a new server-side MCP composition.

The native environment, private launcher, version and integrity checks, policy interception, resource release and provider conformance retain their existing owners and acceptance gates. Neither installing the extra nor constructing SDK options establishes complete harness facts or enables graph validation or execution.

## Consequences

Claude-capable installations carry the vendor dependency stack in addition to any shared-client dependencies. Pure configuration inspection remains independent of that operational stack. The implementation must test installed-extra compatibility and the import boundary on supported platforms before adoption lands.
