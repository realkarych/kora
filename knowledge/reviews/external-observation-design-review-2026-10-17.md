# External observation ownership and delivery review

Date: 2026-10-17. Scope: issue #56; [ADR-0021](../../docs/decisions/0021-external-observations-outside-transactions.md), scheduler/database boundaries, provider failure model, approval transport and existing operator surfaces. Method: independent static analyses of identity, failure containment and MVP alternatives, followed by a skeptical architecture/builder review. No application runtime exists; no runtime test result is claimed.

## Chosen direction

Use existing durable wait/start-binding/approval owners and a bounded cooperative async dispatcher inside the scheduler. External content/credential/provider work happens outside every DB transaction/connection/run/scope lock. Apply through existing transitions only after exact current-owner/token comparison. Keep runtime workers, saved execution contracts, target-source acceptance and immutable approval evidence unchanged.

## Material findings and disposition

| finding | disposition and required contract |
|---|---|
| An approved audit row can survive clear and resemble a new waiting activation | Verify the original current open wait; VCS-bound approval retains it through a newly claimed post-approval head check. Generic forms need no fabricated VCS read |
| Async timeout does not contain arbitrary blocking Python | Explicit trusted cooperative lifecycle, including import/hooks/decoding/cancellation; credits retained until settlement; process supervision deferred |
| Approval can be approved/rejected/cancelled while external creation is in flight | Separate desired open/closed transport intent from audit and gate states; resolved notification; save every learned id on the original request and close it |
| A terminal revision cannot atomically fence a later remote send | Commit authorization before dispatch; terminal intent blocks new authorizations/retries. Preterminal authorized calls may send after cancellation, including a pre-send pause, and remain reconcilable |
| Ref/delivery ids do not identify remote request chronology | Freeze local recipient ids before asynchronous handling. Preserve conservative changed-push failure; explicitly accept false failure from a remotely old push first received during a new request |
| Binding-first start application can deadlock or miss pause/deployment | Prospective run lock, ordered global/namespace/graph scopes, then binding CAS; compatible binding mutations and one start-outcome/cursor commit |
| Unknown terminal create can retain payload forever or be abandoned silently | Read-only paginated lookup, all duplicate ids, compact original-resource tombstone, no form/context hold and no abandonment shortcut or execution reservation |
| Repairing credentials under the same reference may never wake work | Capped authorized probes/local-policy rechecks and existing binding pause/resume; failed nodes remain explicit manual-retry operations |
| Wrapper task limits can hide parallel nested requests | Declare the full dependency-resource set; serial composite invocation with one active outbound request, shared alias accounting, no nested reacquisition |

The revised design had no remaining Important implementation blocker within its explicitly cooperative fault model. This is a design assessment, not a claim that arbitrary provider code is contained or that remote effects are exactly once.

## Future verification and delivery

The [data-model acceptance matrix](../specs/data-model.md#144-future-acceptance-contracts) names the required lock responsiveness, stale result, timeout, duplicate, approval ordering, discovery/restart, terminal delivery and start-binding cases. Database cases run on PostgreSQL and SQLite with configured conforming fakes, injected clocks/randomness and explicit barriers. Slow local services exercise the supported model; mocks, patched transports and synchronization sleeps are forbidden.

The initial deliverable is documentation. Verification must report actual Markdown/link/Mermaid/schema/template CI and whole-branch consistency review separately from those future runtime gates. Direction and plan were approved by the project owner on this date; exact `docs/` wording remains subject to line-by-line PR review. Matching ownership and timeout precedence remain separate work.
