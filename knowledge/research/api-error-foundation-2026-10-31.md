# Transport-free API error foundation

Implementation scope under [issue #523](https://github.com/realkarych/kora/issues/523), checked on 2026-10-31, based on the accepted [API error contract](../specs/api-and-agent-surface.md#1-conventions), [shared diagnostics](../specs/diagnostics.md) and ADR-0002/0011/0026. This record is implementation evidence, not a new contract.

## Delivered boundary

`kora.models.errors` contains frozen, closed producer DTOs for the six-field API error, its one-field envelope, bounded validation details and the five-field validation result. `kora.api.errors` owns fixed safe construction, one status per API001–011 and immutable response metadata. API009 metadata contains exactly `WWW-Authenticate: Bearer`.

Fixed API009/010/011 messages and details cannot be supplied as arbitrary constructor payloads. API011 currently has empty details; future operation owners add their accepted closed reason variants when needed. Empty, lock-timeout and validation details form an initial subset, not an inventory of every operation-specific refusal. Future routes must represent their accepted details explicitly rather than discard them to fit this subset. In particular, `run_backlog_full` requires its complete admission fields and has no builder in this foundation.

Request-level shape/JSON/encoding/byte-limit refusal uses fixed API003 without enumerating or formatting a Pydantic exception. `validation_failure` converts provider faults, including `diagnostics_limit`, and aggregate `DiagnosticCollectionError` into fixed nonretryable API010, without accepting a partial collection. It never interprets a provider's retryable flag as operation safety. This is not a generic exception/transient classifier.

Completed collections copy the shared values, preserve findings and omission counts, and enforce the existing count/compact-UTF-8 bounds. Validity includes omitted errors. Structural copying cannot make arbitrary producer prose safe: reviewed messages, safe metadata, rebase and provenance remain with the validator/provider caller. No whole-document raw Pydantic conversion or invented IR code is added.

The credential audit-commit API010 exception has its own builder. Dependency failures require an explicit read, proven pre-effect, existing operation-safe retry or unknown-outcome context. The route owner must establish that context; an exception alone supplies no proof. These helpers perform no retry, mutation, credential issuance, discard, storage or idempotency operation. The future credential route must withhold/discard the issued value and recheck authority/reacquire on retry. A future uncertain-mutation route first recovers its authoritative stored result; this helper cannot delete or overwrite it.

The trusted exchange owner supplies `request_id` unchanged. Wire round trips preserve a saved error's identity. No identity generation, fresh transport header, log binding or persisted replay is implemented here. Closed producer DTOs do not establish a client's additive-field or unknown-code reader compatibility.

## Verification and ownership

The focused boundary suite is `tests/api/test_errors.py`: exhaustive statuses, all six ordinary invalid-credential categories, fixed body/header, poisoned real Pydantic/provider exceptions, incomplete collection failure, omitted findings, count/byte limits, explicit retry distinctions, closed DTOs and saved identity. Existing `tests/ir/test_diagnostic_collection.py` retains collection algorithm ownership. `tests/architecture/` enforces package permissions and no suppression.

The implementation introduces no dependency, endpoint, framework, database, migration, cache, task, process, socket or output sink. Its values belong to the caller and it emits no lifecycle event. Unexpected programming errors propagate to that caller. The API row in [observability section 7](../specs/observability.md#7-component-coverage-and-activation) activates with the first actual API operation under work item 11, including request correlation, authorized context, real replay, safe logging and blocked/broken-sink evidence. The foundation does not satisfy those pending handler obligations or API/CLI/MCP parity.

Independent correctness and security contexts inspected the design before implementation and inspect the actual complete diff before readiness. The issue and PR retain actual reviewer identity, inspected revisions, findings and command/CI provenance. Local checks target this boundary; one complete successful final-head CI supplies supported Linux/macOS, both metadata databases and existing full strict/scanner/browser/resource gates. No performance measurement or new numerical guarantee is claimed; retained diagnostic limits reuse the existing collection method. Existing resource measurements and activation gates remain unchanged.
