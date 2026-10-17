# kora documentation

This folder is written for people. It holds what kora is for, what it must do, how it is built and why. The long reference material that an engineer or a coding agent consults section by section (normative specifications, detailed diagrams, research, analyses, review records) lives in [`knowledge/`](../knowledge/README.md) and is linked from here wherever a decision depends on it. Reviewed by the project owner on 2026-10-16.

## Reading order

1. [positioning.md](positioning.md): the one-liner, the problem, who kora is for, the primary contrast (Archon), the canonical use case, non-goals, evidence and hypotheses.
   [archon-comparison.md](archon-comparison.md): kora and Archon, the closest open-source system, compared point by point with the stronger side named on each.
2. [requirements/functional.md](requirements/functional.md) and [requirements/non-functional.md](requirements/non-functional.md): what the product must do and under which constraints; every requirement has a stable identifier (`FR-n.m`, `NFR-n.m`) and an MVP or post-MVP tag.
3. [architecture.md](architecture.md): the two-page map of the system: context, containers, stack and the boundaries that must not blur.
4. [console.md](console.md): the design of the web console: who it is for, the entry, the graph page, the sections, states and colours, the actions and the visual rules.
5. [decisions/](decisions/README.md): the architecture decision records, one per significant decision, with context, alternatives and consequences.
6. [proposals/](proposals/README.md): changes that are designed but deferred, in a fixed template with a status.
7. [roadmap.md](roadmap.md): where the project stands, what only code can settle, and the next steps in order.

External observations and approval-channel recovery are governed by [ADR-0021](decisions/0021-external-observations-outside-transactions.md). Its direction and plan were accepted on 2026-10-17; the changed document wording remains pending line-by-line owner review.

## Conventions

- Everything in the repository is in English and must be understandable without the conversation that produced it; identifiers that resolve inside the repository (`FR-6.3`, `NFR-5.5`, `ADR-0007`, `DD-12`) are fine, labels that exist only in a session are not.
- Decisions are made by the project owner and recorded here; the specifications in `knowledge/specs/` apply them and never contradict them. When the two disagree, the decision record wins and the specification is fixed.
- Every statement in this folder has been reviewed and accepted by the project owner; each file says when. A change lands only through a pull request the owner reviews line by line. Material produced by agents without that review (research, analyses, review records, drafts) belongs in [`knowledge/`](../knowledge/README.md).
- Volatile external facts (versions, popularity, prices, release dates) live in `knowledge/research/` with the date they were checked; this folder states them qualitatively and links there.
- Research and analyses are evidence, not decisions. They are cited from a decision record or from positioning, never the other way round.

Inherited execution deadlines are governed by [ADR-0022](decisions/0022-inherited-execution-deadlines.md). Direction and implementation plan were accepted on 2026-10-17; the changed document wording remains pending line-by-line owner review.
