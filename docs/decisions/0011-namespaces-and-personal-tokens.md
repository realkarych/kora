# ADR-0011: Namespaces and personal tokens with two flags; no roles in the MVP

- Status: accepted, 2026-10-15
- Reviewed: by the project owner, 2026-10-16
- Deciders: project owner, after the design review of 2026-10-15 (both cycles)
- Absorbs: requirements decision 15 (its "single token" part is superseded by requirements decision 20), DD-32, DD-19, DD-6
- Related: [0002-ai-first-api-and-thin-clients.md](0002-ai-first-api-and-thin-clients.md), [0003-scheduler-and-workers-over-http.md](0003-scheduler-and-workers-over-http.md), [0008-content-bound-approvals.md](0008-content-bound-approvals.md), [0009-target-repository-trust-boundary.md](0009-target-repository-trust-boundary.md)
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

## Context

kora's design centre is a platform instance shared by many teams on an organisation's own infrastructure (NFR-0.1). Namespaces group graphs, secret references, presets, tool policies and quotas (FR-12.4). Requirements decision 15 kept access control post-MVP: the MVP would ship namespaces and a single shared API token, with role-based access control and credential management arriving later through the authentication protocol.

The design review of 2026-10-15 ([review-2026-10-15.md](../../knowledge/reviews/review-2026-10-15.md)) changed this in two steps. The first cycle found that one token kind for everything gave workers the rights of users ([scoped tokens and secrets](../../knowledge/reviews/review-2026-10-15.md#scoped-tokens-and-secrets)) and that keeping the API token itself in a browser cookie was unsafe ([server-side session cookie](../../knowledge/reviews/review-2026-10-15.md#server-side-session-cookie)). The second cycle's threat model found that a shared token cannot attribute an approval to a person or restrict who publishes presets and policies ([personal tokens](../../knowledge/reviews/review-2026-10-15.md#personal-tokens)), and that transcripts need their own access control ([transcript access, audit and supply chain](../../knowledge/reviews/review-2026-10-15.md#transcripts-audit-supply-chain)).

## Decision

Namespaces are the only partition. Every resource path starts with `/namespaces/{ns}`, and every by-id route joins on `namespace_id` against the caller's namespace list.

User tokens are personal from the MVP: each is bound to a person and a namespace list and carries exactly two flags.

- `approve`: may respond to approvals. The respond route, `kora approve`, `kora reject` and the MCP tool `kora_respond` require it; the approver recorded in the audit is the token owner.
- `platform`: may publish presets, namespace policies and tool policies. Preset and namespace writes and the MCP tool `kora_admin` require it. `kora compile` fails (`DSL013`) when a registry preset escalates above the namespace policy.

Reading transcripts and session segments is a per-namespace `transcripts` capability on the token's namespace list, not a flag. There are no roles and no groups. The MCP server's default token has no `approve`; `kora standalone` prints one personal token with both flags for the `default` namespace.

Workers hold service principals: a service token belongs to a worker, is bound to a runtime and a namespace list enforced at claim, and may only claim, heartbeat, upload and transition attempts whose lease it holds, checked by lease epoch. There are no tokens for agent nodes (ADR-0002).

Frontend login (amended 2026-10-16): `kora open` asks the API for a one-time login link with the user's token (`POST /auth/login-links`) and opens it in the browser; the link carries a single-use code valid for 60 seconds, and `GET /auth/session/{code}` exchanges it for an opaque server-side session cookie (`SameSite=Strict`, `Secure`, HTTP-only) with the token's namespaces and flags. Pasting the token into the login form (`POST /auth/session`) remains the fallback for a machine without the CLI. OIDC sign-in replaces both through the authentication protocol after MVP. Cookie-authenticated mutations carry CSRF protection, the single-page application ships a Content Security Policy, and cookies are never accepted on Bearer-only API paths. The approver recorded from the frontend is the token's owner.

There are no per-token rate limits in the MVP. The built-in authenticator is `static_token`; the authentication protocol returns a `Principal`, so OIDC, RBAC, groups and credential management can replace the token check after the MVP without changing routes.

## Alternatives considered

- **One shared API token** (requirements decision 15). Simplest to operate, but no approval can be attributed to a person, platform writes cannot be restricted, and a leaked token reaches every namespace and transcript. Rejected after the second review cycle.
- **Full RBAC in the MVP**: roles, groups and credential management from the start. Rejected as scope the MVP spine does not need; two flags and a namespace list cover attribution and the separation of platform duties, and the protocol boundary keeps RBAC implementable later (NFR-5.4).

## Consequences

- The `api_token` table carries `kind`, `owner`, `namespaces` (with `transcripts`), `approve`, `platform`, `runtime` and `expires_at`; approval requests record the owner, not the token.
- `API005` is the one forbidden code: namespace outside the list, missing `approve` or `platform`, missing `transcripts`.
- The `/auth/*` routes exist only for the frontend and its sign-in and are excluded from the parity coverage test; `kora open` is a convenience over them, not an API operation.

## Where it is applied

- [FR-7.2, FR-7.3, FR-12.4](../requirements/functional.md); [NFR-0.1, NFR-4.4, NFR-5.4](../requirements/non-functional.md)
- [api-and-agent-surface.md §1, §2, §4, §6, §8](../../knowledge/specs/api-and-agent-surface.md)
- [protocols.md §10](../../knowledge/specs/protocols.md); [data-model.md `api_token`, `approval_request`](../../knowledge/specs/data-model.md)
- [scheduler.md §10](../../knowledge/specs/scheduler.md); [dsl.md §2](../../knowledge/specs/dsl.md)
- [stack reference](../../knowledge/architecture/detailed.md#stack-per-component) (Authentication and Frontend rows)
- [design-decisions-2026-10-15.md, DD-6, DD-19, DD-32](../../knowledge/reviews/design-decisions-2026-10-15.md)
