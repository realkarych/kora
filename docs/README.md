# kora documentation

This folder is written for people. It holds what kora is for, what it must do, how it is built and why. The long reference material that an engineer or a coding agent consults section by section (normative specifications, detailed diagrams, research, analyses, review records) lives in [`knowledge/`](../knowledge/README.md) and is linked from here wherever a decision depends on it. Reviewed by the project owner on 2026-10-16.

Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

Scaffold amendment dated 2026-10-18: scope and implementation plan approved under [issue #86](https://github.com/realkarych/kora/issues/86); wording pending line-by-line owner review.

Threat-model navigation amendment: scope and implementation plan approved under [issue #90](https://github.com/realkarych/kora/issues/90); wording accepted for merge by the project owner in [PR #92](https://github.com/realkarych/kora/pull/92) on 2026-10-18.

Implementation-plan navigation amendment dated 2026-10-19: the reading-order entry for the implementation plan, approved by the project owner under [issue #127](https://github.com/realkarych/kora/issues/127); wording pending line-by-line owner review.

## Reading order

1. [positioning.md](positioning.md): the one-liner, the problem, who kora is for, the primary contrast (Archon), the canonical use case, non-goals, evidence and hypotheses.
   [archon-comparison.md](archon-comparison.md): kora and Archon, the closest open-source system, compared point by point with the stronger side named on each.
2. [technical-introduction.md](technical-introduction.md): the core components, what they exchange, one run through the system, and the reasons for its boundaries. Start here for a technical orientation.
   [threat-model.md](threat-model.md): assets, authority, trust boundaries, attacker scenarios, deployment conditions and the evidence required for security claims.
3. [requirements/functional.md](requirements/functional.md) and [requirements/non-functional.md](requirements/non-functional.md): what the product must do and under which constraints; every requirement has a stable identifier (`FR-n.m`, `NFR-n.m`) and an MVP or post-MVP tag.
4. [console.md](console.md): the design of the web console: who it is for, the entry, the graph page, the sections, states and colours, the actions and the visual rules.
5. [decisions/](decisions/README.md): the architecture decision records, one per significant decision, with context, alternatives and consequences.
6. [proposals/](proposals/README.md): changes that are designed but deferred, in a fixed template with a status.
7. [roadmap.md](roadmap.md): where the project stands, what only code can settle, and the next steps in order.
   [implementation-plan.md](implementation-plan.md): the order of work for the first implementation slice, the order in which shared contracts freeze and the split between two developers.

Technical-introduction amendment dated 2026-10-17: scope and implementation plan approved; the introduction and navigation wording remain pending line-by-line owner review under [issue #67](https://github.com/realkarych/kora/issues/67).

External observations and approval-channel recovery are governed by [ADR-0021](decisions/0021-external-observations-outside-transactions.md). Its direction and plan were accepted on 2026-10-17; the changed document wording remains pending line-by-line owner review.

For contributions and local checks, use [development.md](development.md): tool setup, the commands CI runs, packaging and frontend development.

## Conventions

- Everything in the repository is in English and must be understandable without the conversation that produced it; identifiers that resolve inside the repository (`FR-6.3`, `NFR-5.5`, `ADR-0007`, `DD-12`) are fine, labels that exist only in a session are not.
- Decisions are made by the project owner and recorded here; the specifications in `knowledge/specs/` apply them and never contradict them. When the two disagree, the decision record wins and the specification is fixed.
- Every statement in this folder has been reviewed and accepted by the project owner; each file says when. A change lands only through a pull request the owner reviews line by line. Material produced by agents without that review (research, analyses, review records, drafts) belongs in [`knowledge/`](../knowledge/README.md).
- Volatile external facts (versions, popularity, prices, release dates) live in `knowledge/research/` with the date they were checked; this folder states them qualitatively and links there.
- Research and analyses are evidence, not decisions. They are cited from a decision record or from positioning, never the other way round.

Inherited execution deadlines are governed by [ADR-0022](decisions/0022-inherited-execution-deadlines.md). Direction and implementation plan were accepted on 2026-10-17; the changed document wording remains pending line-by-line owner review.
