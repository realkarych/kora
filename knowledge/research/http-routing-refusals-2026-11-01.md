# HTTP routing refusals and producer boundary

Date: 2026-11-01. Implementing issue: [#538](https://github.com/realkarych/kora/issues/538). The project owner selected variant A and its shared authentication, HEAD and trusted Allow rules; the [selected implementation plan](https://github.com/realkarych/kora/issues/538#issuecomment-6562969401) records the accepted scope. Inspected implementation base: `d2f14e3aa83421a0c1261c808d49d1e281d14f73`.

## Accepted contract and delivered producers

The [normative API contract](../specs/api-and-agent-surface.md#http-method-and-unmatched-path-refusals-issue-538) distinguishes a recognized method unavailable at an active path, API012/405, from a method the origin does not implement, API013/501. Its finite case-sensitive recognition domain grants no successful handler authority. Unimplemented valid tokens receive 501 even on unknown paths; recognized methods on unknown paths receive content-free API001/404. Protected active paths authenticate and check principal kind before method refusal. Anonymous health and unmatched paths consult no authenticator or scoped metadata.

The existing closed error model and definitive producer now represent the two fixed messages, empty details, null location, false retryability and supplied request identity. Both statuses join the exhaustive status registry. API013 metadata contains no Allow. API012 metadata access raises a fixed ValueError until the real active-catalog adapter owns Allow derivation; a generic producer cannot fabricate that authority or return incomplete 405 metadata. No method classifier, catalog substitute or new header-authority type is introduced.

HEAD error delivery carries the status and permitted headers without a body, hidden JSON Content-Length or application Transfer-Encoding. Its current request identity remains in the header, internal safe error and diagnostic. The additive FR-0.4 and ADR-0002 wording makes this delivery exception visible without changing structured internal errors or existing local-client failures.

## Evidence and remaining activation

`test_every_error_code_has_one_status` checks the complete API001–013 registry. `test_method_refusal_bodies_preserve_closed_fields_and_identity` covers current/saved supplied identities and exact wire round trips. `test_method_refusals_reject_untrusted_fields` rejects arbitrary message/details/location, retryability and additional fields, including valid detail families belonging to other codes. `test_method_refusal_metadata_requires_trusted_active_catalog` prevents a header-incomplete 405. Existing definitive-error metadata assertions cover API013 without Allow; existing API001–011, poisoned provider/framework values, diagnostics and retry checks remain active.

These are real value/producer checks, not request execution. The ASGI application remains owned by [#541](https://github.com/realkarych/kora/issues/541), including its separately approved dependencies and exact active handler catalog. That implementation must supply the named trusted-Allow, auth-before-method, HEAD framing, unimplemented-method, unknown-path, handler/OpenAPI inventory and request/error/log tests in the normative contract. Current authentication on SQLite/PostgreSQL, provider failures, anonymous probe behavior and actual header/body delivery are not established by these producers. No HTTP operation, API/CLI/MCP parity, listening server, console, deployment or runtime capability is activated.

The inspected master mutation audit [41297342266](https://github.com/realkarych/kora/actions/runs/41297342266), source `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`, was partial due to budget: 113 executed entries, none reused, pricing and CEL-path scopes only. It supplies no applicable API producer finding and adds no PR mutation gate.

## Security, observability and limits

Existing TM-01 covers current authentication/principal precedence; TM-06/TM-07 cover fixed content-free output, safe identity and HEAD delivery; TM-12 covers closed interpretation and evidence scope. Protected active-path 401 versus unknown-path 404/501 reveals static handler presence and the fixed method policy. That explicitly accepted residual reveals no namespace/object state and grants no authority. Unknown paths perform no scoped lookup; raw methods, paths, query/body values, credentials and framework exception prose cannot enter refusal messages or diagnostics.

The slice adds no actor, credential, trust domain, dependency, persisted format, resource owner, diagnostic sink/event, buffer, task or numerical method. The existing threat model therefore needs no new entry. Runtime log/correlation and framing evidence stays with #541. Required strict checks, independent correctness/security result review and one successful complete final-head CI remain the readiness evidence; there is no timing, memory, transport or hostile-process guarantee from fixed producer checks.
