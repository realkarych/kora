# Complete attempt deadline storage foundation

Date: 2026-11-01. Implementation owner: [#576](https://github.com/realkarych/kora/issues/576), following outcome storage #575 and the accepted [#545 formats](../specs/data-model.md#163-complete-deadline-envelope-and-legacy-projection). This foundation adds representation, not executing-claim writes or deadline enforcement.

## Delivered boundary

`engine.deadline_records` adapts the existing ExecutionDeadline into a closed version-1 envelope. SQL NULL remains an explicit unrecorded result carrying the legacy scalar, distinct from a recorded unlimited value. A complete value preserves UTC microseconds, its concrete scope identifier, exact nonnegative decimal-string uncertainty through timedelta.max and fixed 30-second grace. Integer arithmetic never rounds through seconds represented as floats. The saved scalar must agree exactly with a recorded binding. Unknown or malformed recorded values raise one content-free incompatibility; preparation reads no clock and generates no new timer, grace or authority.

The nullable JsonDocument column has no default or historical reconstruction. Its root-object check distinguishes physical SQL NULL from JSON null: the PostgreSQL driver would otherwise decode both as Python None. The portable check permits an object after JSON whitespace and rejects raw null, scalar and array roots. The strict codec owns closed fields, version, timing and projection validation. Existing JsonDocument behavior is unchanged.

Migration 0006 preserves its five predecessors. PostgreSQL verifies compatible nullable objects, adds the root check NOT VALID, releases preparation DDL locks at the existing owned checkpoint while retaining the advisory lease, and validates before advancing the revision. SQLite adds the column/check atomically in the owned transaction. Compatible preparation resumes; incompatible objects and invalid retained roots refuse without repair. Existing scalar deadlines, attempt starts, sticky expiry latches, outcomes and cancel causes retain their exact values. There is no new table, index, task, watchdog, dependency or logging field.

## One verification plan

| Integration boundary | Owner | Blocking checks | Remaining limits |
|---|---|---|---|
| ExecutionDeadline -> prepared document -> decoded binding | Engine foundation | Exact zero/microsecond/large/full-domain uncertainty and threshold equality; unrecorded/unlimited distinction; strict version/scope/time/grace; known projection mismatch refusal | No first-claim transaction, request sealing or registry adoption |
| Physical JSON -> real metadata database -> codec | Metadata foundation | Bound/unlimited round-trip; SQL NULL history; direct JSON-null/scalar/array refusal with rollback; valid whitespace-root object | Raw database objects still require strict codec validation before use |
| Populated 0005 -> 0006 | Metadata foundation | Original history/FKs/indexes/start/scalar/latches/defaults/omitted old-writer fields/noop/schema equivalence; incompatible objects; cancellation and shutdown; real lease/connection/writer release and compatible resume | No validation-duration or process-wide RSS guarantee; existing methods remain |
| Migration refusal -> operational diagnostics/CLI | Existing metadata logger and upgrade entry point | Actual child-process private-content canary; nonzero refusal; safe failure event; unchanged predecessor and zero lease | No deadline envelope, scope, timestamp, SQL parameter or retained payload is logged |

The existing UpgradeStage observer selects this migration's column/constraint and uses actual SQL barriers. PostgreSQL checks native validation lock waiting, continued permitted DML, released preparation-exclusive locks and retained advisory ownership; SQLite observes before/after actual DDL. No behavior is patched and no new resource-measurement campaign is introduced. Exact head/count assertions advance to six migrations; existing deadlines and behavioral assertions remain.

Initial codec collection failed on a Pydantic regex end-anchor spelling; the corrected supported strict anchor passed all 32 cases. An initial database-test import lacked the named metadata check constant and was corrected before execution. Focused codec/SQLite migration checks then passed 48 cases. The broader local run passed 313 cases and failed one exact CLI migration-count assertion still expecting five; the assertion is advanced to six without changing any gate. Failed XMLs remain failed evidence. Final focused delta, strict checks, independent source results and final-head native Linux/macOS/PostgreSQL evidence are recorded with the PR; local observations are not relabeled as native passes.

Latest applicable master audit 41189316843 is partial/budget on 2b468c8 with pricing/CEL scopes and no applicable deadline finding. It adds no PR mutation campaign or merge hold.

## Activation ownership

The shared engine transition owns the future atomic first executing claim: start, complete binding and matching scalar before request preparation or effects. Scheduler ownership includes due-scope discovery and fresh persistent ancestors; worker/runtime ownership includes unchanged binding relay and real stop/escalation. Verified original-request adoption needs its own approved compatibility procedure. This foundation authorizes none of those consumers and establishes no disconnected-process stopping, publication closure or active effect denial.
