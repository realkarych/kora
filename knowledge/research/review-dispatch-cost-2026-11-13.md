# Review dispatch cost, 2026-11-13

Checked on 2026-11-13 for [issue #1505](https://github.com/realkarych/kora/issues/1505). The numbers are observations of one contributor's coding-agent sessions on this repository. They are not a benchmark, set no numerical gate and do not describe Codex sessions or native GitHub review.

## Owner decision

On 2026-11-13 the project owner approved four changes to how [kora-verify](../../.claude/skills/kora-verify/SKILL.md) dispatches independent review:

- one independent context may apply both the correctness and security profiles when security is selected only for changed CI, verification policy or agent configuration;
- Claude Code reviewers are dispatched through one tool-limited agent definition, kept only if it lowers the measured first-turn context by at least 30%;
- the coordinator saves the complete diff once and reviewers send independent reads and searches together;
- a reassessment goes to a fresh reviewer when the earlier reviewer's context is 150k tokens or more, or has been idle longer than the host's prompt-cache lifetime.

No profile, trigger, gate or required check is removed, and the reviewer model is unchanged.

## Method

The source is the usage record that the Claude Code desktop application stores with every model request of a session and of each subagent it starts. The sessions ran from 2026-11-10 to 2026-11-13 in this repository on Claude Opus 5.5 and Claude Fable 5.2.

- A request is counted once, by its message identifier, with the last usage record stored for it.
- The context of a turn is the sum of its uncached input, cache-write and cache-read tokens.
- A reviewer dispatch is a subagent whose brief opens by naming it an independent reviewer. 40 were dispatched; one never started and is excluded, leaving 39.
- Costs are compared in input-token equivalents: a cache-read token counts 0.1, a cache-write or uncached input token 1.25 and an output token 5. These weights are an assumption about relative price, not a bill.
- Tool output is measured in characters of tool results and attributed by the command or file that produced it.

## Baseline

| Part of the work | Input-token equivalents | Share |
|---|---|---|
| Coordinator sessions | about 165M | 44% |
| Reviewer subagents | about 70M | 19% |
| Other subagents (implementation, triage, research) | about 141M | 37% |

The 39 reviewers:

| Quantity per reviewer | Median | Range |
|---|---|---|
| Turns | 48 | 10 to 141 |
| Context of the first turn | 58.9k tokens | 58.2k to 62.3k |
| Largest context | 223k tokens | 103k to 412k |
| Cache-read tokens | 7.4M | 0.7M to 32.4M |
| Cache-write and uncached input tokens | 311k | 66k to 1.8M |
| Output tokens | 46k | 15k to 135k |
| Input-token equivalents | 1.6M | |

- Cache reads are 52% of the reviewer cost, cache writes 33% and output 15%.
- The first-turn context is read again on every turn. Multiplied by each reviewer's turns it is 34% of all reviewer cache reads.
- 16 of the 39 reviewers applied only the security profile and cost 38% of the reviewer total. The one reviewer that applied correctness and security together, for #1428, took 20 turns and 1.8M cache-read tokens.
- Of the tool output reviewers consumed, 35% came from `git diff` and `git show`, 31% from other shell reads and searches, 6% from `gh`, 5% from test files read directly and 4.5% from the threat model.
- The most expensive reviewers were those that grew past 300k tokens of context: ten of them account for 53% of the reviewer cache reads.

## Candidates measured and not adopted

- **A shorter report to the coordinator.** The median final report is 8.1k characters, about 2k tokens. Carrying it in the coordinator's context costs a few percent of the reviewer that wrote it.
- **Splitting the skill into more reference files.** The skill text is about 4k tokens; its share of any session's cost is below one percent.

## Agent definition measurement

`.claude/agents/kora-reviewer.md` limits a Claude Code reviewer to the file reading, search and shell tools. A generic subagent inherits every tool of its session, including those of connected servers, and their descriptions are part of its first-turn context.

The measurement is one dispatch of each kind with the same brief, which asks for a one-word reply and no tool call, in one session at the same revision.

| Subagent | Host | Revision | Tokens used by the single turn |
|---|---|---|---|
| Generic | Claude Code desktop application | branch base `fbdfd8e` plus this change | 60,119 |
| `kora-reviewer` | not yet measured | | |

A session lists agent definitions when it starts, so the session that wrote the definition could not dispatch it, and the headless command-line surface was not available on 2026-11-13. Until the second row is measured, the 30% criterion is unverified and the definition is unproven.

## Limits and revisit trigger

- The sample is one contributor, one host and four days. Shares differ for other work mixes.
- The coordinator sessions are the largest part of the cost. Their median context was 440k to 565k tokens per turn and one session ran 1,082 turns over four changes. Nothing in this change addresses that; working one issue per session does.
- The 150k-token bound for continuing a reviewer is a judgment from the range above, not a measured optimum.
- The combined assignment removes the independence of the correctness and security conclusions from each other for the changes it covers. It was observed once before this change.
- Revisit trigger: after the next ten reviewer dispatches following the merge, compare their median input-token equivalents, turns and findings per review with the baseline. A higher median cost, or combined reviews that return no security conclusion of their own, reopens the affected rule.
