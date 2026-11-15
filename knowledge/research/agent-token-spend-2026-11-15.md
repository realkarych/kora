# Agent token spend, 2026-11-14 to 2026-11-15

Checked on 2026-11-15 for the window 2026-11-14T00:00Z to 2026-11-15T01:00Z. This is an agent-produced triage record. It sets no rule, gate or decision. Every change proposed below needs an issue and, where it touches verification policy, CI or agent configuration, an explicit owner decision under [AGENTS.md](../../AGENTS.md#development-verification).

## Method and limits

- Session transcripts and per-request usage records were not available to the analysing session. The figures below therefore measure text and activity that the agents left on GitHub: pull request and issue comments, body sizes, commits and CI runs. Text an agent writes to GitHub is also text it composed, and usually text it and later sessions read again, so it serves as a proxy for token spend. It is not a bill and does not capture tool output read inside a session.
- Comment character counts were measured for issues #1446, #1562, #1450 and #1452 and for pull requests #1501, #1531, #1548 and #1561. The other pull request comment totals are estimated at about 2.6k characters per comment.
- Codex sessions post under the owner's account, so authorship separates agents only by branch prefix: `codex/*` for Codex, `claude/*` for Claude Code, and the external contributor's fork branches.
- [The 2026-11-13 review-dispatch record](review-dispatch-cost-2026-11-13.md) asks for a comparison after ten reviewer dispatches. That comparison needs usage records and is not made here.

## Totals in the window

| Quantity | Value |
|---|---|
| CI workflow runs | 95: 27 on `master` (20 to 62 min each), 58 on owner/Codex branches (33 failed or cancelled), 10 on fork branches (4 failed) |
| Pull request comments, 38 PRs, lifetime | 126, about 330k characters, all posted by the owner account; no GitHub reviews or inline threads |
| Pull request bodies, same 38 PRs | about 476k characters, about 12.5k on average |
| Issue comments in the window | #1446: 32 (121k characters), #1562: 25 (68.6k), #1450: 10 (46.5k), #1452: 11 (14.9k) |
| Rough share | about 90% Codex, about 10% the fork contributor, about 0% Claude Code (no `claude/*` work in the window) |

## Where the effort went

1. **Stacked integration narration without merged output.** The scheduler issue #1446 and the worker API stack (#1508, #1450, #1523, #1451, #1535, #1452; PRs #1514, #1518, #1524, #1536, #1539) received a "rebase onto X, compare blobs, reuse conclusions" comment after almost every upstream merge. Migration revision numbers moved 0028 → 0029 → 0030 → 0031 on one branch. The open stacked PRs carry 31 to 35 commits and 15k to 20k lines of diff once their prerequisites are counted. Nothing in this stack merged in the window.
2. **One test failure became a multi-PR diagnostic campaign.** Issue #1562 (one admission preparation test exceeding its ten-second deadline) took 25 comments, three PRs (#1565, #1574, #1576), a child issue and a discarded local experiment. Each step ran a plan, an independent plan review, an owner approval, the implementation, a result review and CI. Three profiles (correctness, security and performance) reviewed the plan for a 20 to 35 line test helper with zero production lines. The issue body grew to about 12k characters of dated sections appended one after another.
3. **Duration table refreshed four times in one day.** PRs #1529, #1534, #1567 (superseded) and #1565 each rewrote `scripts/ci/lane_durations.json` with about 3.3k changed lines. Each came with its own issue, its own dated research record and a 5k to 15k character body. The trigger was a new test module without a recorded duration, which then pushed a lane against its deadline. #1531 needed three repairs and an owner escalation under the two-unsuccessful-repairs rule, and #1548 needed a module split.
4. **Long verification records rewritten on every push.** Records included reviewer context paths, SHA-256 digests of plans, scripts, JSONL outputs and jobs metadata, and full lists of specification sections read. The docs-only PR #1561, with about two changed lines at its head and a 22k-character body, sent two fresh correctness and security reviewers to confirm a one-line wording receipt.
5. **Boilerplate that restates standing constraints.** Almost every #1562 comment repeated that historical failures remain failures, that no rerun or deadline change was made and that the result does not prove the cause. AGENTS.md already guarantees all of these.
6. **Classification essays for kora failures.** CI failures that could never qualify for a rerun were still classified job by job in tables, followed by separate "merged as squash" and "postmerge verification complete" comments (#1501, #1531, #1548).
7. **Bodies far larger than their diffs.** #1546 changed 11 lines with a 7.9k body; the fork PRs #1559 (3 lines, 4.2k), #1550 (4 lines, 3.2k) and #1551 (7 lines, 9.3k) followed the same pattern.
8. **Fixed context per session.** AGENTS.md is 36.6k bytes, about 9k tokens. The Development verification section is 10.9k bytes, and its CI rerun classification about 4k. Every coordinator loads the whole file and every reviewer is required to read it. Against the 17.8k-token first turn of the `kora-reviewer` agent measured on 2026-11-13, that is a large share, although the share of a 500k-token coordinator context is small.

## Candidate optimizations

Ordered by expected saving. A rule change in the "Needs" column requires an owner decision first.

| # | Change | Where | Addresses | Needs |
|---|---|---|---|---|
| 1 | Limit stacking: open a dependent implementation PR only when its prerequisite is approved for merge. Record rebases as one current line in the PR's Verification section, not as a new comment. | AGENTS.md, Issues and pull requests | 1 | owner decision (contributor workflow) |
| 2 | Proportional review for test-only changes: when production code is unchanged and no gate, deadline or assertion strength changes, a correctness result review is enough. Skip plan review, and do not select security or performance because a test observes timing or diagnostics. | kora-verify, Select independent reviews | 2, 4 | owner decision (verification policy) |
| 3 | Pre-approved diagnostic budget: let the owner approve one budget for a failing test once (for example up to two test-only diagnostic PRs), instead of a stop, proposal and approval cycle at every step. | AGENTS.md, Escalate after two unsuccessful repairs | 2 | owner decision (verification policy) |
| 4 | Make the duration refresh cheap: a scheduled or manual workflow regenerates the table from a green `master` run and opens one PR. Refresh PRs get no new research record. Mark `scripts/ci/lane_durations.json` `linguist-generated` so that GitHub and native review collapse it, and exclude it from the reviewer diff in favour of re-running the refresh command. Alternatively, let the planner estimate an unrecorded module from a test-count-based estimate with headroom, so a new module does not need a refresh before it merges. | CI, docs/development.md, kora-verify Dispatch | 3 | owner decision (CI) |
| 5 | Delta-only records: comments and Verification sections state what changed and what was observed. They do not restate constraints that AGENTS.md already guarantees. | AGENTS.md, Documentation or Issues and pull requests | 4, 5 | owner decision (contributor workflow) |
| 6 | Limit digests to the reviewer diff and the CI artifacts cited as evidence. Plans, local scripts, metadata listings and reviewer paths need none. Keep the sections a reviewer read in its result, and link that result instead of copying it. | kora-verify, Dispatch and collect evidence | 4 | owner decision (verification policy) |
| 7 | Keep issue bodies current like PR Verification sections: problem, status and acceptance stay in the body, and dated history moves to comments. | AGENTS.md, The issue body | 2 | owner decision (contributor workflow) |
| 8 | A kora CI failure gets one line (run, job, test, owning issue). A job-by-job classification is written only when a rerun is being considered. Show this one-line form in the PR template. | PR template, AGENTS.md | 6 | editorial if limited to the template example; otherwise owner decision |
| 9 | A delta that changes only PR description or evidence wording reopens no reviewer conclusion. | kora-verify, Dispatch and collect evidence | 4 | owner decision (verification policy) |
| 10 | Move the CI rerun classification and the two dated file-specific exceptions into docs/development.md, leaving short pointers. This saves about 6k bytes per load of AGENTS.md. Measure the reviewer first-turn context before and after, with the 2026-11-13 method. | AGENTS.md, docs/development.md | 8 | owner decision (moves policy text) |
| 11 | A short PR body form for standing-authorization fixes and test-only repairs under 50 changed lines: Impact, Closes, why it qualifies, checks. | PR template | 7 | owner decision (template structure) |

## Revisit trigger

After the changes the owner accepts are merged, repeat this count over one comparable day: comment characters per merged PR, CI runs per merged PR and the share of failed or cancelled runs. Where session usage records are available, repeat the 2026-11-13 per-reviewer measurement as well.
