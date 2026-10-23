# ADR-0023: kora's identity on code hosts and purpose-scoped credentials

- Status: direction and implementation plan accepted, 2026-10-18; the acceptance was given by the project owner and relayed by a contributor; document wording pending owner review
- Deciders: project owner
- Resolves: issue #43
- Amends: [ADR-0008](0008-content-bound-approvals.md) (automation and self-approval checks), [ADR-0016](0016-trusted-execution-and-publication.md) (the credential behind late authorization)
- Related: ADR-0005, ADR-0011, ADR-0013, ADR-0017, ADR-0018, ADR-0021

- First-slice amendment dated 2026-10-23: scope and implementation plan approved under [issue #140](https://github.com/realkarych/kora/issues/140); wording reviewed line by line and [approved by the project owner](https://github.com/realkarych/kora/pull/220#issuecomment-6361959908) on 2026-10-23.

## Context

kora checks out repositories, publishes prepared revisions, opens change requests, posts comments and reads review state on code hosts. Until now one credential from the VCS provider configuration did all of it, and nothing defined the identity kora acts under.

ADR-0016 already moved publication out of the candidate domain. A worker obtains a fresh publication credential only after inspection and late authorization. ADR-0016 left four questions open, and this record answers them:

- who issues that credential;
- what it is allowed to do;
- how long it lasts;
- which principal it represents.

Three facts shape the answer:

- A GitHub App installation token expires after one hour, so a credential resolved when the workload is submitted can be dead when a multi-hour session ends.
- Backends differ in what they enforce. A GitHub installation token can be narrowed to repositories and permissions but not to one push or branch. GitLab project tokens have date-based expiry and scope names of their own. A static service credential has whatever rights it was created with.
- A login, a token id or a configured alias is not an account. ADR-0008's rule that kora's own bot and the change-request author cannot approve needs a stable identifier that survives token replacement.

## First-slice amendment of 2026-10-23

A saved trusted binding may explicitly select `local_credential_free` for an existing local bare repository. Missing, empty or failed credentials, a local-looking URL and caller flags never select that mode. `PublicationAuthorization` remains mandatory; its required `credential` field and `IssuedCredential.value` accept explicit null only in this mode, with no omission default. Credential-bearing modes continue to require registered secret values.

The no-secret result has `rights=None`, `expires_at=None`, `replacement="not_applicable"` and `revocation="not_applicable"`. These values state that no upstream credential exists; they cannot satisfy a binding's minimum upstream credential-rights/expiry/revocation requirements, and null expiry never extends attempt authority. Contradictory combinations fail acquisition. The common model adds nullable rights and the two enum values without changing credential-bearing semantics.

`PrincipalRef` gains `kind="local_service"`. Its provider instance scopes an immutable configured service identifier, preserved across restart and binding revisions. It is not a username, UID, path, binding alias, Git author or invented code-host account. `PublicationAuthorization.principal` carries the complete trusted principal snapshot; equality compares `(provider_instance, stable_id)` without resolving a separate identifier. The sealed request and trusted checkpoint pin the immutable binding identity, revision and credential mode before preparation; acquisition and late authorization cannot initialize or replace them. CredentialRequest, IssuedCredential and PublicationAuthorization carry that required tuple without defaults, with the bounds defined in protocols.md. Acquisition validates the requested tuple against trusted binding state before secret/issuer work and returns it for exact comparison. The acquired principal is saved before dispatch. Current aliases or candidate data cannot replace this context; configuration edits advance the binding revision.

`Vcs.acquire_credential` remains in the first-slice protocol: checkout and publish return this no-secret metadata without resolving a secret or calling an issuer; API purpose is unsupported. `CheckoutPlan.credentials` is empty. Publish acquisition stays late, after inspection, and adoption reacquires only for the original saved binding tuple and operation. Changed or unavailable binding semantics refuse new publication without rewriting the checkpoint or settling unknown effects. Authorized read-only reconciliation and completed replay preserve the original destination and provenance; credential refresh under an unchanged binding remains allowed. Acquiring metadata grants no dispatch authority. After acquisition finishes, both credential modes require a fresh heartbeat carrying the saved binding/operation expectation and an exact successful current-authority echo before every effectful finalization or retry. The worker/API checks current lease, desired state, deadline and binding authorization, and the worker serializes response validation and dispatch with local gate closure, including closure during the wait; runtime/runner requires inspection, the saved checkpoint, current dispatcher, earlier dispatcher/publication closure and no retirement; the executor matches all identities, digests, principal/binding/mode and deadline before push. `PublishPermit` cannot replace the authorization. Mismatch fails `publication_authorization_mismatch` before publication; unknown outcomes retain reservations and recovery obligations.

Local mode emits no `vcs.credential_issued`. Publication-related `runtime.phase` records carry the scoped principal, binding identity/revision, mode and explicit phase/outcome. The runtime owns their authenticated source; worker/API promotion preserves append-only audit provenance and a compact checkpoint mirror. Authorization does not assert that a push occurred. The audit provenance survives checkpoint release and payload retention without storing secret values or secret-derived hashes.

The destination is an absolute POSIX path or `file:///absolute/path` with empty authority, naming an existing bare repository. Network/scp/helper/relative forms, URL authority/userinfo/query/fragment, malformed encodings and decoded traversal are refused. Normalize once and pin/recheck directory identity across checkout, head reads, publication and reconciliation; reject symlink/parent replacement and changed binding revisions. This restricts Git transport and does not prove a physically local filesystem. Fresh trusted Git metadata and a constructed environment exclude inherited config/includes/rewrites/pushurl, helpers, SSH/askpass, candidate hooks/filters and object alternates. Destination-side hooks/configuration remain operator-trusted under the cooperative local model, not suppressed by client cleanup.

The exact contract and conformance cases are in [protocols.md](../../knowledge/specs/protocols.md#credential-free-local-publication). Local authorization, principal and recovery evidence remain required now. Remote issuer, rotation, expiry, credential re-scan and code-host identity tests activate with their corresponding backend; the general MVP backend design below is not a first-slice implementation claim.

## Decision

**Credentials are acquired per purpose by trusted code, never resolved into the workload.** The VCS integration gains one operation, `acquire_credential`. Only the worker, its private worker-host executor and the control-plane providers call it, and never from the candidate domain. A request binds:

- the immutable deployment binding, its revision and credential mode, and the repository;
- one purpose: `checkout`, `publish` or `api`;
- for `publish`, the operation id, the prepared digest and the destination digest of the `PreparedPublication`.

Timing follows the purpose:

- `checkout` is acquired immediately before preparation.
- `publish` is acquired only at late authorization, after the supervisor has confirmed that the whole candidate domain has stopped and the handoff is sealed.
- `api` is acquired per call by the worker, and by the scheduler's approval-channel and observation providers.

A credential that expires or is rejected is reacquired for the same operation. An adopting worker always reacquires instead of reusing anything an earlier worker held. Values are redacted and never persisted: the issuance route is exempt from the API's idempotency replay, so a repeated request re-checks authorisation and acquires a fresh credential.

**The reference GitHub App backend issues in the control plane.** The App signing key stays with `kora api`. A worker obtains each credential through a worker-facing route bound to:

- the attempt;
- its current lease epoch;
- the purpose;
- for `publish`, the operation, prepared digest and destination digest.

The key therefore exists in one trusted place, issuance is fenced by the lease, and a worker in another network never holds it. Other backends may issue on the worker or the worker host. Where the parent credential lives is a declared property of the backend, not a core assumption.

**Backends report what they actually enforce.** An issued credential carries independent fields:

| field | values |
|---|---|
| `resource_scope` | `repository`, `repository_set`, `account` or `unknown` |
| `operations` | the native permissions in the provider's own terms, plus a coarse mapped set: `read`, `write_refs`, `change_requests`, `comments` |
| `expires_at` | a time, or null when the backend has no native expiry |
| `replacement` | `reissue`, `rotate` or `static` |
| `revocation` | `native`, `expiry_only` or `manual` |
| `principal` | the principal reference below |

A deployment binding declares the minimum it requires for each purpose. A backend that cannot meet it fails acquisition with `config_error`. kora never falls back silently to a broader credential. A static credential reports `replacement: static`; its `expires_at` is the configured or discovered expiry, null only when it has none, and it is never described as scoped or short-lived. Reports describe residual authority honestly: a repository-scoped write token is not a single-push grant.

**Principals are stable host identifiers.** A principal reference has four fields:

- the provider instance;
- the host's immutable account or app id;
- a kind: `app`, `bot_user`, `service_user` or `user`;
- a display name.

A login, token id or alias is never used as identity.

- Every publication records the principal that pushed and the principal that opened the change request.
- Every deployment binding revision keeps the set of automation principals kora has used for it. Token replacement and a change of bot principal add to the set.
- The `vcs` approval channel classifies a reviewer as automation by membership in that historical set, and as self-approval by equality with the recorded publication principals.
- A backend declares whether its rotation preserves the principal.

**The trusted publication boundary is ADR-0016's.** Publication runs only in fresh publication storage, against the prepared revision, after the candidate domain is confirmed stopped. Environment clean-up is not the boundary. On `local`, process groups give clean-up, not a hostile-code boundary, as ADR-0016 states. SSH agent sockets and keys are credentials too and never enter the candidate domain.

**Backends in MVP:**

- the GitHub App, as the first native backend;
- explicitly configured static service credentials;
- GitLab project or group tokens, reported honestly as static or rotating;
- adopter-supplied integrations.

Git transport uses the Git CLI with credential-helper and SSH mechanisms as plumbing. A non-Git system supplies its own VCS plans. A custom endpoint for a known host is not a proprietary provider.

**Portability is shown by a second adapter.** The VCS conformance suite runs the same identity and credential scenarios against two configurations:

- the GitHub App backend over a local Git repository with an in-process code-host fake;
- a fake proprietary VCS with opaque revisions, opaque change-request ids and its own issuance backend.

The second shows that the core assumes neither Git refs nor installation ids. It does not prove interoperability with any particular real system.

## Alternatives considered

- **Resolving credentials at submission.** Rejected: a one-hour token can expire before a long session ends, and a write credential would exist before inspection.
- **Issuing on every worker from a copied App key.** Simpler, but it copies the parent credential to every worker host. It remains acceptable for backends that declare it, not for the reference App backend.
- **A mandatory credential broker or secrets service.** Rejected for MVP: no examined tool removes provider-specific integration, and an operated service adds initialization, unsealing and plugin operations. Vault or OpenBao can back the parent credential through the existing secrets provider. The agent-facing broker of issue #42 stays post-MVP.
- **One `short_lived_credentials` flag.** Rejected: scope, expiry, replacement and revocation vary independently across backends.
- **`bot_login` as identity.** Rejected: logins change, tokens differ per bot, and one current value cannot recognise earlier automation.

## Consequences

- Signing the App's JWT adds one dependency, PyJWT with its `cryptography` backend, recorded in the stack table; no decided row of ADR-0013 changes.
- One worker-facing issuance route exists for the reference backend. It is excluded from CLI and MCP parity like the other worker routes.
- Bindings gain per-purpose minimum requirements, and binding revisions gain an automation-principal set.
- Change-request publication and review classification record principals, not logins.
- Token lifetimes, rotation semantics and principal stability are taken from provider documentation until each backend's conformance case runs.
- The model-provider credentials of harness adapters are a different identity, and this record does not change them.

## Where it is applied

- [protocols.md §2](../../knowledge/specs/protocols.md#2-runtime-fr-81): late authorization.
- [protocols.md §4](../../knowledge/specs/protocols.md#4-vcs-fr-52): `acquire_credential`, the credential types and the backends.
- [protocols.md §5](../../knowledge/specs/protocols.md#5-approval-channel-fr-71-fr-72a): principal-based checks.
- [protocols.md §13](../../knowledge/specs/protocols.md#13-conformance-suites-nfr-102): conformance scenarios.
- [data-model.md](../../knowledge/specs/data-model.md): publication principals, automation-principal sets, issuance events.
- [api-and-agent-surface.md](../../knowledge/specs/api-and-agent-surface.md): the worker-facing issuance route.
- [FR-5.2](../requirements/functional.md).
- The VCS row of the stack table in [detailed.md](../../knowledge/architecture/detailed.md).
