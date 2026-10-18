# ADR-0025: Notifications through requested reviewers and durable outbound webhooks

- Status: direction and implementation plan accepted, 2026-10-18; the acceptance was given by the project owner and relayed by a contributor; document wording pending owner review
- Deciders: project owner
- Resolves: issue #44
- Amends: requirements MVP definition (one-way chat integrations), FR-7.4, [ADR-0008](0008-content-bound-approvals.md) (requested reviewers are not authorised approvers)
- Related: ADR-0005, ADR-0011, ADR-0015, ADR-0021

## Context

kora records every fact a person needs to act on in the append-only `event` table and delivers none of them to a person. The `cli` channel pushes nothing. The `vcs` channel posts the `ReviewBundle` on a change request authored by kora's bot but requests no reviewers, so the code host notifies nobody. The only consumer of the event table is the observability exporter, which may drop events under load. A background run whose approver does not watch the console stalls silently.

Event ids are creation-ordered, not commit-ordered: a transaction can create an event and commit after a later one. A cursor that advances over event ids can skip that event.

## Decision

**Requested reviewers.**

- The `vcs` channel asks the VCS integration to request reviewers after it opens the change request, through a new operation, `request_reviewers`, and the capability flag `reviewer_requests`.
- Recipients are provider-specific opaque identifiers that the provider validates, such as a GitHub login or team slug.
- An empty list sends no request and leaves the host's own routing unchanged.
- A provider without the capability, or a failed request, is recorded as an event and never fails the node.
- Requested reviewers grant nothing. ADR-0008, the approver allowlist and the principal checks decide what counts.

**Outbound webhooks.**

- **Destinations.** Three kinds: `json`, signed in the Standard Webhooks shape with a separate signing-key secret reference; `slack`, an incoming-webhook URL or a bot token and channel; `telegram`, a bot token and chat id.
- **Content.** A short summary with links to the console, built from the deployment's `public_url`, and to the change request where one exists.
- **What a notification never does or carries.** It never approves. It carries no secret value, transcript or artifact payload.
- **Scope.** Slack and Telegram are one-way notification channels in MVP. Chat-based approval stays post-MVP.

**Delivery records written with the event.** When an event of a selected kind is inserted, the same transaction inserts one delivery record per enabled subscription that selects the kind. Delivery never scans the event table with a cursor, so commit order cannot skip an event.

**Short claims, calls outside transactions, token-checked acknowledgements.** A bounded background task in the scheduler process works in three steps:

1. It claims due records in a short transaction with a fresh token.
2. It calls the receiver with no transaction, connection or lock held, under ADR-0021's credit and deadline rules.
3. It acknowledges the outcome in another short transaction that checks the token.

A crash leaves claims to expire, so another sender reclaims them. A stale acknowledgement changes nothing. Delivery is at least once. Duplicate messages are possible, and each message carries the event id as its `webhook-id`. A slow receiver never blocks scheduling or SQLite's single writer.

**Subscriptions have a lifecycle.** A subscription is a namespace resource with a stable id, managed with the `platform` flag.

- **Selection.** It lists event kinds explicitly from an allowed set of actionable states: `approval.requested`, `approval.invalidated`, `approval.expired`, `run.failed`, `run.succeeded`, `attempt.lost` and `vcs.publish_repeated`.
- **Start point.** A new subscription receives events whose transaction commits after it.
- **Changes.** Changing the selection affects later events. Changing the destination advances its revision: pending records go to the current destination, each recording the revision it used.
- **Disabling and deleting.** Disabling suspends pending records and enabling resumes them. Deleting drops them.
- **Failure.** A persistently failing subscription is marked `failing` with its last error and keeps retrying with capped back-off. It never blocks other subscriptions or graph execution.

**Destinations are allow-listed.** The deployment configuration lists permitted hosts. A destination is checked when a subscription is saved and again at every send, after its secret is resolved. Redirects are not followed.

## Alternatives considered

- **A cursor over event ids.** Rejected: event ids are not commit-ordered, so a late-committing event would be skipped.
- **Exporter semantics.** Rejected: a bounded queue that drops on overflow loses exactly the approval notifications that matter.
- **A separate service or message broker.** Rejected for MVP: a bounded task in the scheduler process with durable records meets the requirement.
- **A twelfth protocol for notifiers.** Not needed now. The destinations are fixed kinds, and ADR-0005's eleven protocols stay unchanged.
- **Chat approvals in MVP.** Deferred under FR-7.4. One-way messages are a smaller and separate contract.
- **Email.** Deferred: kora knows tokens, not addresses.

## Consequences

- Two records are added, `notification_subscription` and `notification_delivery`. One record is written per selected event per subscription, a write amplification bounded by explicit selection.
- The VCS integration gains one operation and one capability flag. It uses the `api` credential purpose proposed in issue #43.
- Chat bot credentials are long-lived secrets of the installation. They stay behind secret references and never reach a workload.

## Where it is applied

- [docs/requirements/functional.md](../requirements/functional.md): the MVP definition, FR-7.6 and FR-7.4.
- [data-model.md](../../knowledge/specs/data-model.md): subscription and delivery records.
- [scheduler.md](../../knowledge/specs/scheduler.md): the delivery task.
- [protocols.md §4, §5, §13](../../knowledge/specs/protocols.md): `request_reviewers` and the conformance contracts.
- [api-and-agent-surface.md](../../knowledge/specs/api-and-agent-surface.md): the subscription resource, `public_url` and the destination allow-list.
- [states.md](../../knowledge/design/console/states.md): which states notify.
