# Original attempt outcome storage foundation

Date: 2026-11-01. Implementation owner: [#575](https://github.com/realkarych/kora/issues/575). Depends on the durable [#545 formats](../specs/data-model.md#16-original-outcome-and-full-deadline-persistence-545) in PR #577. The owner approved the complete plan and the exact threat-model wording separately; the source representation and bounded read-scope clarification retain actual independent design results. This record does not activate a writer, read route or retention operation.

## Delivered source boundary

`engine.outcome_records` prepares detached immutable receipt metadata and bounded escaped canonical UTF-8 JSON error text from already validated protocol values. The canonical fingerprint binds namespace, attempt, complete reporting grant and complete original report/evidence. Its fields, integer/float/signed-zero/array distinctions and full process record are separate from process-exit equality and dispatcher deduplication. The saved response is independent of the attempt's later applied status. The selected historical core does not recreate original ClosureEvidence.

Error text preserves admitted U+0000 keys/values, literal escapes, Unicode and exact numbers without changing JsonDocument. An admitted 16-KiB error plus fixed wrapper fits the 17,408-byte body envelope. Full report admission derives from VcsRef, dispatcher-envelope, error and fixed wrapper/grant limits; it does not impose an unrelated 1-MiB root limit. Only bounded immutable receipt/body values escape preparation. No provider, file, process, background task or logger is introduced.

`OutcomePayloadText` guards SQL TEXT byte size, UTF-8 and absence of an actual SQL-string null character. The separate strict codec checks canonical JSON, exact digest/size, closed version/shape and body/availability consistency. Nullable outcome/body/state columns and their cross-column presence check preserve old rows and omitted old-writer inserts as unrecorded. The check explicitly requires a non-null state for a recorded receipt, avoiding SQL CHECK's unknown-value acceptance. No default, historical reconstruction, index or dependency is added.

Migration 0005 preserves 0001–0004. PostgreSQL verifies exact compatible nullable objects, stages the NOT VALID presence check, releases preparation DDL locks at the existing checkpoint while retaining the dedicated advisory lease, validates and only then advances Alembic. SQLite adds all three columns/check inside its existing owned transaction. Incompatible objects or invalid retained combinations refuse without history repair; compatible committed preparation resumes. Acquisition timeouts are unchanged and do not bound total validation duration.

## One verification plan

| Boundary | Owner | Blocking evidence | Limits |
|---|---|---|---|
| Protocol report/grant -> detached receipt/error body | Engine foundation | Maximum admitted Unicode/NUL/escape/depth/error cases; typed numeric/order identity; changed grant/evidence; complete report larger than one component; unknown/corrupt closed records and content-free refusal | Pure comparison is not authenticated acceptance or trustworthy evidence |
| Receipt/body/state -> real DB -> explicit unavailable body | Metadata foundation | Exact SQLite/PostgreSQL round-trip, immutable receipt after body expiry/applied-status change, invalid presence combinations and rollback | Tests arrange state directly; no production transition, sweeper or selected read is active |
| Populated predecessor -> 0005 | Metadata foundation | Old values/FKs/indexes/defaults/omitted inserts/noop/schema equivalence; incompatible shapes; preparation/validation cancellation, shutdown, compatible resume and actual lease/connection/writer release | Existing validation occupancy and numerical methods remain; no duration/RSS guarantee |
| Failure -> metadata diagnostics/CLI | Existing metadata logger and upgrade entry point | Actual private-content canary process refusal, safe failure event and zero retained lease | No new diagnostic field/event or body/digest/SQL-parameter logging |

The existing real UpgradeStage observer accepts explicit column/constraint names for the new migration, with its original cancellation-cause defaults unchanged. Hooks observe actual SQL through injected Database construction; no module/attribute/transport behavior is patched. PostgreSQL checks actual locks and native validation waiting; SQLite cancellation observes actual before/after DDL.

Focused local evidence: 263 SQLite codec/database/diagnostic/architecture regressions passed in 32.01 s; after changing the full-report byte-budget constant's import to its owning VcsRef module and removing unused constants, all 28 codec cases, including later cancelled/lost response coverage, passed again in 0.06 s. The numeric value and all DB/migration/resource inputs remained unchanged. These XMLs have no embedded revision; actual commands/input snapshots are coordinator-matched, not relabeled native evidence. Strict Ruff and Pyright passed the implemented source/tests. Initial codec fixture calculation omitted the extra NUL-key member and produced five pre-codec protocol refusals; its corrected exact 16-KiB calculation passes, with the failed XML retained. Initial formatting/type issues were fixed without suppression or relaxed checks.

Actual implementation review and final-head Linux/macOS CI, including real PostgreSQL, remain pending at this source checkpoint. Full original grant/process provenance, current authentication, locked report acceptance, first-write/event/marker ordering, #216 holds/deletion and current selected-read access remain with their actual implementing consumers. No codec return certifies closure, releases resources or permits runtime effects.

Latest applicable master audit 41189316843 is partial/budget on 2b468c8 with restored continuation and pricing/CEL scopes only. No applicable outcome finding or PR mutation campaign is inferred.
