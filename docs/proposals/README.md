# Proposals

A proposal describes a change to kora that is designed but not decided or not scheduled: a new runtime, a new backend, a new protocol, a change to the graph document format. It is the place to work a change out in the open before it becomes a decision record and a specification change. Reviewed by the project owner on 2026-10-16.

## Lifecycle

| status | meaning |
|---|---|
| `draft` | being written; may be incomplete |
| `proposed` | complete and ready for the owner's decision |
| `deferred` | complete, agreed in principle, waiting for a trigger (demand, a release, a dependency) stated in the proposal |
| `accepted` | decided; the resulting decision record and specification changes are linked from the proposal |
| `rejected` | decided against; the reason is recorded in the proposal |

A proposal is never deleted and never renumbered. When it is accepted, the decision becomes an `ADR` in [../decisions/](../decisions/README.md) and the proposal links to it.

## Writing one

Copy [template.md](template.md) to `NNNN-short-title.md` with the next free number and add a row below.

| proposal | status | trigger |
|---|---|---|
| [0001 Temporal-backed execution backend](0001-temporal-execution-backend.md) | deferred | demand from an adopter at a scale or reliability level the own scheduler does not meet, after MVP |
| [0002 Scoped credential broker for agent sessions](0002-scoped-credential-broker.md) | deferred | the first adopter that needs per-attempt downstream credentials beyond MCP-held secrets, after MVP |
