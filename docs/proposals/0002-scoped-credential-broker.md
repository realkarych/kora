# Proposal 0002: Scoped credential broker for agent sessions

- Status: deferred
- Reviewed: pending owner review of the pull request that adds it
- Author: kora contributors, from the model the project owner set out on issue #42
- Date: 2026-10-18
- Trigger: the first adopter that needs per-attempt downstream credentials beyond MCP-held secrets, after MVP
- Related: [ADR-0009](../decisions/0009-target-repository-trust-boundary.md) (amended 2026-10-18), the code-host credentials of issue #43, DD-30, FR-10.1, FR-10.2

## Motivation

In MVP an agent never holds a secret on an unrestricted network. A service token belongs to an MCP server that runs outside the agent, so the agent uses the tool but cannot read the token (ADR-0009). That still leaves two gaps:

- the token's rights are whatever the operator gave it;
- a tool server that needs broad rights holds them for the whole attempt.

Some adopters will want an agent to obtain narrow, temporary access to a specific resource and operation, granted by policy they control and attributable to one run and attempt. Nothing in MVP provides that, and MVP does not promise it.

## Proposal

A trusted service outside the agent's execution boundary holds the parent credential. The agent never receives it and cannot modify the access rules or the issuance policy.

- **Request.** The agent authenticates with an identity bound to its run and attempt, and requests temporary access to specific resources and operations.
- **Check.** The service checks the request against permissions defined by an administrator. The agent cannot grant itself broader access.
- **Grant.** The agent receives only the approved temporary credential, with a bounded lifetime and explicit scopes.
- **Attribution.** Issuance and use are attributable to the run and attempt. Expiry and supported revocation are handled by the service.
- **Enforcement.** A short lifetime alone is not enough. The target service, or a trusted gateway in front of it, must enforce the restricted permissions.

A token that authorises access to a vault is not the same as a credential the downstream service accepts. When the downstream service cannot issue restricted temporary credentials, storing its static key in a vault and returning it to the agent does not provide this model. Access then stays behind a trusted proxy or a tool service that enforces the allowed operations, as MCP-held secrets do in MVP.

What does not change: the MVP rules of ADR-0009, the code-host credentials of issue #43, and the model API key in the harness environment until a credential proxy exists (DD-30).

## Alternatives

Existing mechanisms must be evaluated before a custom issuer is designed. These are examples to evaluate, not a dependency choice:

- **Vault token policies and TTL controls.** Tokens carry attached policies and bounded lifetimes ([token creation](https://developer.hashicorp.com/vault/api-docs/auth/token#create-token)). They authorise access to Vault itself; they constrain a downstream service only through a secrets engine that issues that service's own restricted credentials.
- **AWS STS `AssumeRole` with session policies.** The resulting credentials cannot exceed the assumed role's permissions, and a session policy narrows them further ([API reference](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)). This applies only where the downstream service trusts AWS identities.
- **Keeping MCP-held secrets only.** This is the MVP answer. It needs no new service, but it gives no per-request narrowing.

## Plan

1. Evaluate the mechanisms above against the model and record the result as an analysis.
2. Define the broker as a provider protocol with a conformance suite, so an installation can plug in an existing service.
3. Deliver a reference implementation only if no evaluated mechanism fits the protocol.

Every slice is tested with fakes of the broker and of a downstream service that enforces scopes, without real cloud or vault services.

## Risks and open questions

- Which downstream services adopters need, and whether those services can issue restricted credentials natively.
- Whether a broker becomes a required operated service. It should stay optional, like Vault and OpenBao for the code-host credentials of issue #43.

## Decision

Not decided. Deferred until the trigger above occurs.
