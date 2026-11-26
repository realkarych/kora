# ADR-0030: Run the Pi SDK in a managed candidate process

- Status: design and implementation plan accepted 2026-11-27 under [issue #1930](https://github.com/realkarych/kora/issues/1930#issuecomment-7205065845). Wording pending line-by-line owner review.
- Decider: project owner
- Amends: [ADR-0004](0004-harness-adapters-over-vendor-sdks.md) and [ADR-0013](0013-implementation-stack.md)
- Related: [ADR-0001](0001-own-control-plane.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0009](0009-target-repository-trust-boundary.md), [ADR-0016](0016-trusted-execution-and-publication.md), [ADR-0017](0017-execution-uncertainty-and-safe-replacement.md), [ADR-0022](0022-inherited-execution-deadlines.md)

## Context

MVP supports running a Pi coding-agent session with the same guarantees as the first local workflow. Pi offers both a programmatic SDK and a CLI protocol. The SDK supports explicit model, settings, tools, resources and session state. At the inspected revision, CLI startup can run repository migrations before project trust is resolved, and the native shell tool detaches its process group. Those behaviors conflict with the intended first local profile's repository trust and process ownership. The [dated source record](../../knowledge/research/pi-sdk-profile-2026-11-27.md) separates upstream interface facts from pending qualification.

## Decision

Add the direct `pi` harness using the official Pi Coding Agent SDK in an optional Node.js candidate process. The Python adapter remains in the session runner and delegates to one installed child over bounded private stdio. The existing runtime launcher owns processes and release. The existing runner owns Policy, limits and trusted event normalization. The child receives only the configuration and authority for its selected attempt and may run only the approved tool definitions. The runtime starts and accounts for shell commands in the candidate domain.

Pin the SDK, its resolved dependency closure, Node runtime and packaged bridge in the selected execution installation. The operator's installed pin and the observed runtime identities must agree before a session can start. Build and provision them through the existing locked dependency and trusted installation workflow. The Python distribution, server and public API have no dependency on a running Pi process. An installation without the optional Pi profile can still serve or run otherwise supported graphs.

The initial profile uses a fixed Anthropic model, the pinned catalog destination, and one dedicated installation key resolved through the file secret provider. The launcher delivers the key to the child in memory over the owned channel after placement verification. The key stays out of argv, environment, files, prompts, tool subprocesses and diagnostics. The local runtime retains its existing trusted-development threat boundary. [Execution profile](../../knowledge/specs/pi-execution-profile.md)

## Alternatives considered

- Pi CLI protocol mode would avoid a kora-owned process bridge but shares CLI startup migrations and default resource discovery. Its normal Bash tool starts a separate process group whose lifetime the local attempt must account for.
- Loading the SDK into the API or supervisor stack would broaden the dependency and authority boundary and make the SDK process lifetime inseparable from its host.
- The generic ACP provider remains a post-MVP decision under ADR-0004. A direct profile can be qualified against the selected Pi SDK and tool callbacks.

## Consequences

Pi package upgrades need version, input-integrity, real-interface and release evidence for the exact configured installation. The independent Pi qualification is part of full MVP support and uses the existing local first-slice workflow. The baseline milestone gate and sequencing remain governed by ADR-0001. Installed selection and execution depend on their own evidence.

## Where it is applied

- [Pi execution profile and activation](../../knowledge/specs/pi-execution-profile.md)
- [Harness, Policy and conformance](../../knowledge/specs/protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27)
- [Trusted declarations](../../knowledge/specs/validation-facts.md)
- [First-slice contract](../../knowledge/specs/first-slice.md#pi-qualification-within-mvp) and [threat model](../threat-model.md#pi-sdk-profile)
