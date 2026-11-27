# ADR-0031: Use one maintenance window to prepare run-event positions

- Status: design and implementation plan accepted 2026-11-27 under [issue #1992](https://github.com/realkarych/kora/issues/1992#issuecomment-7227091941)
- Reviewed: project owner accepted the wording line by line on 2026-11-27
- Decider: project owner
- Amends: [NFR-7.2](../requirements/non-functional.md#7-operability)
- Related: [ADR-0018](0018-immutable-attempt-history.md), [ADR-0029](0029-installed-api-serving-stack.md), [run-event follow](../../knowledge/specs/run-event-follow.md)

## Context

Durable run-event positions must include every saved event from the previous writer, including events that commit late with a lower identifier. Installed SQLite serving requires its exact Alembic head, and first Claim admits specific revisions and their native schemas. An additive successor by itself does not keep the current binaries serving.

## Decision

Allow one operator-controlled maintenance window per existing ordinary installation for the exact transition from genuine `run_event_metadata_0034` to the first position revision, proposed as `run_event_position_0035`. Ingress and original writes may be unavailable while the installation is held. The duration depends on the saved history. The ordinary migration and compatibility rules govern all other revision pairs. Qualified custody uses its separately qualified whole-image procedure.

Before changing the schema, verify the artifacts and target database, fence old admission and restart, and observe settlement of the original writers and affected external work. Keep the fence through completion or restoration. After the last original transaction settles and before DDL, capture a matching metadata and artifact restore image and verify it with the exact prior artifact in isolation. Restore may reopen that artifact only while successor production writes and external effects remain unadmitted.

Ship the guarded migration, resumable preparation, startup checks and atomic positioned writers in one releasable artifact before the migration target can advance. For an existing installation, bare `kora db upgrade` refuses this transition before advancing the revision. Preserve that command for a proven empty intended metadata catalog on either database and for a verified completed installation where it is a no-op. The runtime delivery will specify the explicit maintenance entry point and its authority.

Reopen only after the qualified successor is ready. A failed or interrupted preparation stays within the same maintenance window. The operator may resume under the same verified identity and fence, or verify a restore to the matching pre-DDL state before reopening the prior artifact. The [transition procedure](../../knowledge/specs/run-event-follow.md#legacy-prefix-and-activation) defines the evidence for both databases.

## Alternatives considered

- A compatible bridge release could preserve rolling operation by teaching the current serving and Claim gates to admit the successor and ensuring all writers participate at cutover. It would add a release and a separate compatibility qualification.
- A one-shot history rewrite would simplify restart bookkeeping. Its transaction and lock lifetime would grow with saved history.

## Consequences

Backfill uses bounded, restartable batches while the old installation stays fenced. Finite history and first Claim retain their saved identities and supported behavior after reopening. The positioned reader follows as a separate delivery over the prepared data.

This decision changes the migration policy for the specified future delivery. The current migration target and installed PostgreSQL serving refusal remain in force.

## Where it is applied

- [Migration policy](../../knowledge/specs/scheduler.md#14-migrations-nfr-72)
- [Run-event legacy prefix and activation](../../knowledge/specs/run-event-follow.md#legacy-prefix-and-activation)
