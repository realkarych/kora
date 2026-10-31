# Attempt cancellation-cause storage

Date: 2026-11-01. Implementation owner: [issue #572](https://github.com/realkarych/kora/issues/572), the independently mergeable cause-only child of [#545](https://github.com/realkarych/kora/issues/545). The [actual direct owner answer](https://github.com/realkarych/kora/issues/545#issuecomment-6549472932) explicitly approves this foundation without backfill. The frozen proposal has SHA-256 `d4277cd25a4797484a847161b5a7b050a9f57c5cbf5a288f67e523361df7e15d`; its [actual independent correctness, security and performance design results](https://github.com/realkarych/kora/issues/545#issuecomment-6537013351) remain supported by unchanged relevant database, migration, fixture and contract inputs through base `239a455ef64f6d2cfeb5d33a14dd004fa23dda5e`.

## Delivered storage boundary

Table metadata and forward revision `0004` add nullable collated text `attempt.cancel_cause`, constrained to SQL NULL or exactly `cancel`, `clear`, `supersede`, `timeout` and `stop`. Revisions `0001` through `0003` remain unchanged. Existing values, row identities, foreign keys, indexes, deadlines, latches, retained dispatcher/hold state and timestamps are preserved. There is no default, backfill, synthesized cause, index, payload copy, new dependency or DB-to-protocol import.

SQL NULL means unrecorded cause. It does not establish first-cause order, that no stop happened, resumption, timeout classification or replacement eligibility. The domain constraint does not enforce write-once behavior across updates. The future locked engine transition owns first-write ordering, event and scheduling-marker effects. No execution writer, report acceptance, completion, expiry enforcement, notification or runtime behavior is activated. Original-outcome encoding, replay identity and full-deadline representation remain separate unresolved prerequisites.

## Upgrade and resource ownership

PostgreSQL verifies the exact nullable type, absence of a default and the domain definition. Preparation adds the column and `NOT VALID` constraint, then commits before validation. The existing dedicated advisory lease spans both stages; preparation DDL locks do not span history validation. Alembic stays at the predecessor until validation and final checks complete. A compatible committed preparation resumes; incompatible schema or invalid retained values refuse without repairing history. Recovery uses a compatible backup rather than downgrade.

SQLite performs the nullable column/check addition inside its existing owned transaction. Interruption before or after actual DDL rolls back that transaction; a completed upgrade reruns as a no-op. A preexisting incompatible column refuses rather than invoking a table-copy or schema-adoption framework. Validation can occupy the single writer while visiting history. Existing SQLite busy timeout and PostgreSQL lock timeout bound acquisition contention; they establish no fixed migration duration or uninterrupted-write guarantee.

The existing migration connection, SQLite writer, PostgreSQL advisory lease and database pool own release. No new task, thread, process or persistent resource owner is introduced. Existing migration/transaction diagnostics retain their content-free reporting owner; no new domain event or cause payload is logged.

## Verification and remaining limits

The [pre-edit implementation plan](https://github.com/realkarych/kora/issues/572#issuecomment-6549595175) maps acceptance to:

- `test_attempt_cancel_cause_upgrade_preserves_history`: populated real predecessor, all previous fields and affected schema objects, NULL/no-default/old omitted insert, final metadata and no-op replay.
- `test_attempt_cancel_cause_domain_is_exact`: six accepted values and real constraint refusals with no partial row effect.
- `test_attempt_cancel_cause_upgrade_recovers_compatible_partial_schema`: real preparation/validation interruption, committed PostgreSQL state versus atomic SQLite state, compatible resume and actual connection/lease/writer release, including shutdown.
- `test_attempt_cancel_cause_upgrade_refuses_incompatible_schema`: wrong type, nullability, default or domain with no revision advancement or history repair.
- `test_attempt_cancel_cause_upgrade_refusal_is_content_free`: real upgrade-process refusal without a private cause marker in diagnostics.

PostgreSQL tests observe actual catalog locks and native validation wait state, not elapsed-time comparisons. Real engine observation hooks are selected through the existing `open_database` injection and synchronize actual SQL with explicit events. SQLite cancellation occurs before or after actual DDL. Existing connection ledgers check actual closure; no module, attribute, transport or DB behavior is patched or replaced.

Existing migration, pricing, retention, process, commit-order, transaction/close, import-layer and no-suppression assertions remain active. Only current migration-head/step expectations advance. Numerical workload, sample, observer and acceptance methods are unchanged. Actual commands, revisions, dialect/platform results, independent implementation reviews and final CI evidence belong in the PR. Design results alone establish no executable readiness.

The available daily master audit `41189316843` is partial on `2b468c836e381e1b487effa5d455b5e74bb53d01`, with restored continuation, and audits pricing/CEL paths only. It has no applicable attempt-migration finding. No new mutation campaign, benchmark, score threshold or timing guarantee is added.
