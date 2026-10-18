# AI development host support

Checked: 2026-10-18. This record concerns contributor sessions, not supported versions of kora's product harness adapters.

## Official interfaces

- [Codex skills](https://learn.chatgpt.com/docs/build-skills) describes repository skill discovery and symlink support.
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) describes native delegation; available APIs depend on the host surface.
- [Claude Code skills](https://code.claude.com/docs/en/skills) describes `.claude/skills` discovery.
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) distinguishes fresh subagents from inherited-conversation forks. A fresh child must explicitly read its assigned repository profile.
- [Codex in GitHub](https://learn.chatgpt.com/docs/third-party/github) documents `Code Review Rules` in `AGENTS.md`, `@codex review`, repository automatic-review settings and the native review's severity threshold.
- [Codex best practices](https://learn.chatgpt.com/docs/learn/best-practices) describes referencing shared review guidance from `AGENTS.md`.

The canonical skill and relative Codex symlink share one instruction source. No custom agent definitions or global plugin changes are part of the setup. Official documentation establishes supported interfaces; the acceptance observations below establish what was actually exercised.

## Acceptance observations

Initial acceptance used instruction snapshot `4d65cb87b6a0d58eb6da35c2ac3a785856d4ce9d`; targeted routing/read-order retests used `b7eeadf0aaea2d3139eadbc539e640e9b7ba83b4`. Separate clean disposable clones added a one-line editorial fixture to `knowledge/README.md`, changing “engineers” to “contributors”. No product code or production data was involved. Requests asked to review the last commit using the repository's normal process, without naming the skill. They authorized local independent review, prohibited edits/publication and explicitly excluded CI/readiness claims.

| Host/surface | Configuration | Observed result |
|---|---|---|
| Codex CLI `0.162.1`, macOS, `codex exec` | User config ignored; plugins/hooks disabled for the session; native multi-agent enabled, maximum four threads requested; read-only sandbox; default selection resolved to `gpt-6.1-sol` | Passed discovery and fresh correctness dispatch. The startup skill catalog included `kora-verify` through `.agents/skills`; the parent read the canonical skill, spawned a child with `fork_turns: "none"`, and the child read `AGENTS.md`, `references/correctness.md` and the fixture diff. Both finished without edits. |
| Claude Code CLI `2.1.302`, macOS, print mode, `claude-opus-5-5` | Project settings only; empty strict MCP configuration; `dontAsk` with read/delegation tools allowed; no session persistence; no Chrome | Passed discovery and fresh correctness dispatch. Startup listed `kora-verify`, the parent invoked `Skill` with that name and `Agent` with `subagent_type: "general-purpose"`. Forwarded child events showed the assigned profile and fixture diff being read. One child completed, with no recursive children or permission denials. |

The Codex acceptance was repeated with session persistence because its `--json` stream included waits but omitted the underlying spawn call. Inspection of the parent and child rollout records established the actual call and child reads; a final answer claiming an independent review was not accepted as proof by itself. The child's metadata identified the observed parent and a fresh spawn. Relevant redacted observations are:

```text
startup skill catalog: kora-verify -> repository skill path
parent read: .claude/skills/kora-verify/SKILL.md
parent call: collaboration.spawn_agent, fork_turns = "none"
child source: subagent/thread_spawn, depth = 1, forked_from_id = null
child read: AGENTS.md .claude/skills/kora-verify/references/correctness.md
child inspected: fixture HEAD^..HEAD and knowledge/README.md
review result: no findings; CI explicitly not run; readiness not claimed
```

The Codex configuration still exposed personal skills; one sample additionally read the installed `gstack-review` skill. The setup does not require or modify it. Read-only Git commands produced macOS cache-permission diagnostics but exited zero with the requested diff; those diagnostics are retained as an environment limitation, not hidden as clean execution. No application/browser diagnostic check was being run.

Claude's startup event reported only built-in plugins (`cc-plugin-agents-md`, `cc-plugin-telemetry`, `cc-plugin-plugin-authoring`) and no MCP servers. The same cold ordinary-prompt exercise initially stopped on CLI `2.1.299` before inference because OAuth had expired. After the operator authenticated, the installed CLI reported `2.1.302` and execution succeeded. The initial zero-inference run established discovery only; the later forwarded tool events establish dispatch/profile loading. Authentication was restored by the operator, without copying credentials or changing global settings in this setup.

## Routing exercises

The Codex host also classified the following hypothetical requests using the installed instructions. These are tabletop classifications, not executed product tests or simulated proof that those components exist.

| Case | Observed classification |
|---|---|
| Editorial wording fixture | Actual independent correctness review; other specialists not applicable; CI explicitly excluded by the exercise |
| IR/schema contract | Correctness before and after; performance/security by complexity and hostile-input effects; schema/example checks now, runtime checks upon implementation |
| Executable backend without console | Correctness, separate backend performance and applicable security; parity/integration/both databases; browser pending runnable console |
| Frontend behavior with runnable shell | Correctness, browser and separate performance; static/build, common smoke plus changed journeys, negative cases and diagnostics |
| Authentication | Correctness/security before and after; applicable browser/performance; real denied-access integration and login/denial journeys |
| Specification-only secrets boundary | Correctness/security design review and documentation checks; unimplemented runtime checks pending |
| Third-party provider installation | Correctness/security, installed fixture discovery/metadata/configuration/listing/external conformance; protection claims need real tests |
| Renamed/deleted files and untracked tests | Both rename paths/deletions/nonignored untracked content; semantic component inheritance; final committed evidence required |
| Unmapped implementation | Block until assigned component, contracts, reviews and checks |
| Change after passed review | Recompute scope, invalidate affected conclusions, repeat affected review/checks and final-revision CI |
| Missing independent delegation | Required review blocked; self-review cannot substitute |

Attempted Codex negative exercises using `--disable multi_agent` and, separately, `agents.max_threads=1` did not prevent native delegation on this host: a review still ran in each. These configuration attempts are not evidence of an unavailable tool or enforced concurrency limits. The skill's concurrency cap is a coordinator obligation, not a demonstrated host setting guarantee.

A separate actual Claude exercise omitted delegation from `--tools`, exposing only Bash, Glob, Grep, Read and Skill. With the same editorial review request, it reported the required correctness review as `blocked`, explicitly rejected its own reading as independent evidence and withheld readiness. The event summary recorded zero children. This is observed handling of unavailable delegation, distinct from the tabletop classification.

Independent examination of the initial authenticated Claude output found that its authentication classification omitted the additive frontend/backend checks, and its backend-without-console classification called browser checks not applicable. Its child read the profile but only searched `AGENTS.md` for terminology; a complete explicit read was unproved. The skill was clarified to combine matching review rows, distinguish pending capabilities, and require complete policy/profile reads before the diff. Cold ordinary-prompt retests on both hosts then selected separate backend performance for executable authentication/provider changes, added browser coverage when the console exists, and marked absent console checks pending. Both children's actual tool events showed complete policy/profile reads before the diff. Only correctness was actually dispatched in these fixtures; specialist execution remains conditional on a real applicable change.

Claude's parent could not inspect its child's tool log in the tested print surface, although the operator could inspect forwarded child events. An attempted requirement that every parent verify those logs was removed: it added a host-specific runtime obstacle beyond the approved workflow. The complete-read instruction remains, ordinary reviews disclose trace visibility limits, and host acceptance inspects actual events. Parent access to a child's transcript is not assumed or guaranteed. The initial Claude outputs also suggested commit-message issue links and editorial alignment not required by repository policy; these suggestions did not become new requirements.

Cold editorial smoke was repeated on the final instruction snapshot `9116ffe730d175b95e2453850fec30ce6467378a`. Both hosts again demonstrated skill/profile loading and fresh independent dispatch, disclosed unavailable parent traces and withheld readiness without CI. Codex explicitly read both complete policy/profile files before the diff. Claude explicitly read its profile but did not issue a separate `AGENTS.md` file read, despite the parent's instruction; its reported full policy read is not observed proof, and native startup injection was not independently established by those events. The earlier complete-read retest remains valid, but this sample limits any claim of consistent adherence. No redundant-I/O rule or host wrapper was added to conceal that limitation. Subsequent changes to this record do not alter the tested instruction files.

Current repository checks are Markdown lint, local links/anchors, Mermaid parsing, IR example validation and GitHub-template schemas. Their final-revision run links belong in the setup PR. Neither host smoke was a product CI run, and no application checks are marked passing.

## Native GitHub Codex Review

The integration uses documented repository review rules to read the canonical skill as shared guidance. It does not assume native cloud skill discovery, CLI subagent APIs, browser access or runtime measurement support. The GitHub mode inspects applicable profile criteria and available revision-matched evidence in one read-only pass; it does not replace the coordinator's independent specialists or executable gates. The official GitHub guide currently describes reporting P0/P1 findings, so absence of a native finding cannot certify lower-severity issues or completion evidence.

Automatic review is a separate repository setting at [Codex review settings](https://chatgpt.com/settings/code-review). The manual trigger is a PR comment containing `@codex review`, without a skill name. Repository instructions do not create a GitHub label trigger. On 2026-10-18, the owner confirmed that automatic review was already enabled. The settings page opened in the available in-app browser showed a signed-out ChatGPT page, so the setting was not independently inspected or changed.

A cold local Codex CLI exercise used a disposable checkout at `c1b17b7`, containing the GitHub instruction amendment over `d029645`. The request identified a GitHub PR review of `HEAD^..HEAD` without naming the skill. Actual command events showed reads of the canonical skill, complete `AGENTS.md`, correctness and security criteria, the full diff and base policy. The result applied GitHub reviewer mode, reported no actionable findings and explicitly withheld CI/readiness and native-activation claims. The observed event stream contained only read operations, with no delegation or project-command execution. This establishes local entry-point behavior, not native cloud execution; the earlier host tests establish local coordinator behavior at their stated snapshots. MacOS Git cache diagnostics were present despite successful read commands.

Local coordinator regression exercises then used editorial fixture `ef510b9` over instruction snapshot `b9b8d7d`. Both Codex and Claude invoked the canonical skill and fresh correctness children, read the profile and withheld readiness without CI. Codex parent/child rollout events showed `fork_turns: "none"`, a matching parent identity with no forked conversation, and complete child policy/profile reads before the diff. Claude forwarded events showed `Skill`, `Agent` with `general-purpose` and the child profile read; a separate child `AGENTS.md` read was again not observed. That existing visibility/adherence limitation remains. These ordinary local requests did not enter the GitHub single-reviewer mode.

Native service activation was observed separately on [PR #88](https://github.com/realkarych/kora/pull/88): the plain [manual trigger](https://github.com/realkarych/kora/pull/88#issuecomment-6250638052) received the bot's eyes reaction and a running status for `b9b8d7d`. The [completed native review](https://github.com/realkarych/kora/pull/88#pullrequestreview-5563239815) cited the new `AGENTS.md` rules and flagged evidence still referring to the earlier revision. New-revision CI and independent review results were available; their final links and the finding's disposition belong in the PR. This proves native invocation and an output referencing the new rules, not internal skill/profile loading or automatic-trigger execution. The service exposes no loading trace in those GitHub results. Subsequent evidence-only edits do not change the tested instructions.

## Enforcement limitations

On 2026-10-18, the repository's `master` protection required Markdown lint, Links and anchors, Mermaid diagrams and Example graph documents. The CI job GitHub templates was not a required merge status. Protection did not enforce administrators and did not require the branch to be up to date. This setup does not change repository administration; the PR reports the missing required status for owner/admin configuration.

Product checks remain pending component implementation. Live host acceptance uses model services as an operator setup check; it is not part of deterministic product CI. No host sample proves automatic routing for every future task or sandbox isolation from a reviewer prompt.
