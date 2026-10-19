# States, colours and words

Status: design detail behind [docs/console.md](../../../docs/console.md), written 2026-10-16, revised after the design review of 2026-10-17. The internal state machines are in [data-model.md](../../specs/data-model.md) section 4; this file maps each of them to what the console shows.

## The five colours

| colour | glyph | meaning | whose move |
|---|---|---|---|
| gray | a dash; skipped a double chevron, upstream failed a gray cross, cancelled a slashed circle | not started, ready, skipped, upstream failed, cancelled | nobody's yet, or nobody's any more |
| blue | a filled circle | running, including every wait that is not a person's: timer, event, sensor poll, retry back-off, the queue for a worker, admission | the system's or the outside world's |
| amber | a ring | waiting for a person: an open approval request | the person's |
| green | a check | succeeded | done |
| red | a cross | failed | stopped |

Shape says what colour says. The glyph is drawn in the node's corner opposite the kind icon at every zoom level, in every segment of a strip at least 12 px wide (a narrower segment carries a hatch for failed and a dot for waiting for a person), in the traffic light of the Graphs list, in the strips of the runs list and the Targets list, and beside every state word in a panel; the graph reads without colour. Error classes never colour a node; they are words in the second line. The five colours are defined as light and dark pairs of a fill and a line, with their contrast ratios, in [tokens.md](tokens.md).

## Node instances

| `node_instance.status` | colour | word | second line (zoomed in) |
|---|---|---|---|
| `pending`, `ready` | gray | `pending` | nothing |
| `queued` | blue | `running` | "in queue · <since the attempt was created>" |
| `running` | blue | `running` | "<elapsed since the attempt started>" |
| `waiting`, wait kind `approval`, request pending | amber | `waiting for you` | "approval · <waiting for> of <deadline>"; integration/delivery gap visible in panel |
| `waiting`, VCS-bound request approved, current wait open | blue | `running` | "verifying approved commit" or integration retry/backoff; approval audit stays separate |
| `waiting`, wait kind `timer` | blue | `running` | "timer · <remaining>" |
| `waiting`, wait kind `event` or `poll` | blue | `running` | "waiting for <event name or sensor>" |
| `waiting`, wait kind `retry_backoff` | blue | `running` | "retry in <remaining> · attempt n of m" |
| `waiting`, wait kind `admission` | blue | `running` | "admission · concurrency limit", "admission · daily budget", "target held · <since>" or "execution recovery · <attempt>"; effective scope/name/used/limit and observed/recheck time, or all target holders/recovery blockers in the panel; sampled blocker lists explicitly incomplete |
| `succeeded` | green | `succeeded` | "in <duration>" |
| `failed` | red | `failed` | the error class in one word (table below) |
| `skipped` | gray, marked | `skipped` | "branch not taken" or "trigger rule" |
| `upstream_failed` | gray, marked | `upstream failed` | "<upstream node> failed" |
| `cancelled` | gray, marked | `cancelled` | "by <actor>" |

The wait kind, deadline, subscription and admission reason come from the API (ADR-0015/ADR-0020). Lifecycle colour does not imply execution occupancy: no-attempt waits reserve no execution-run slot, while outstanding acceptance and acquired target holds persist separately. A live parallel or historical unresolved attempt still reserves capacity. Approval success opens readiness, not immediate execution; retry saturation leaves the old state untouched. Scope counts/cap-source version and next action are observations, not a promised start time.

A node whose declared definition is materialised per fan-out element is one rectangle: its colour is the worst state among its instances in the order red, amber, blue, green, gray, and the strip inside it carries one segment per element in that element's colour; the counts of elements per state are always words beside the strip, whatever the colour, so that a group with two elements done and two not started never reads as finished.

A waiting node's last CEL evaluation error (`match` or `until`) is a read-only reason in the node panel beside the still-open wait: kind, field and time, never values, separate from integration health. A sensor trigger binding shows the same on Schedules ([ir.md section 7.7](../../specs/ir.md#77-evaluation-errors)).

## Node text

A node carries two lines and nothing else. The first line is the node's name, truncated at the end with an ellipsis when it exceeds the node's width, the full name on hover and in the panel. The second line is at most 32 characters in the grammar "<state or reason word> · <duration>": "running · 48 min", "in queue · 12 min", "target held · 3 h", "failed · command failed", "infrastructure · branch left". Identifiers (run ids, branch names, worker names, event names longer than a word) never appear in a node; they are in the panel. The selected node has a 2 px outline in both themes at every zoom level, distinct from the focus ring.

## Error classes in the second line

| `error_class` | word |
|---|---|
| `harness_error` | harness error |
| `command_failed` | command failed · exit <code> |
| `timeout` | timeout |
| `events_exhausted` | event limit |
| `budget_exhausted` | budget exhausted |
| `artifact_invalid` | invalid output |
| `attempt_failed` | sub-graph attempt failed (the `else_reason` in the panel) |
| `approval_rejected` | rejected |
| `approval_expired` | approval expired |
| `candidate_moved` | candidate moved |
| `wait_expired` | wait expired |
| `stalled` | stalled |
| `protected_path_modified` | protected path modified |
| `secret_leak` | secret leak |
| `infrastructure` | infrastructure, with "· branch left" when a push preceded the failure; the branch name in the panel |
| `cancelled` | cancelled |
| `config_error` | configuration error |

A node enters the runtime's infrastructure retry policy only after affirmative runtime termination and closure of every dispatcher/platform publication (ADR-0017). Then it is blue with "confirmed worker loss · retry n of m" until the policy is exhausted, then red with "infrastructure". An expired lease alone is blue "execution unknown · last observed <time>" and retains capacity; it is not a retry or a new attempt.

## Attempts (in the node panel)

| `attempt.status` | shown as |
|---|---|
| `created`, `claimed` | waiting for a worker; the worker when claimed; the lease expiry |
| `submitted`, `running` | running on <worker>; last heartbeat; `desired_state` when `cancel` |
| `finishing` | collecting: final artifacts being validated and stored after reconciled runtime publication; prior reconciliation remains visible in the timeline |
| `succeeded`, `failed`, `cancelled` | the terminal word with the error class |
| `lost` | confirmed loss: original-domain retirement and every dispatcher/publication closure established; evidence provenance and the infrastructure retry that followed |
| any non-terminal status with `execution_state=unknown` | original domain, recovery version/reason, last observation/time, every dispatcher grant, publication uncertainty, retained scopes and permitted actions; no replacement by time alone |

## Artifact result states

These labels are separate from node state and payload availability (ADR-0019). The declared target source stages its complete output set; ordinary nodes retain existing upload behavior.

| artifact status | label and meaning |
|---|---|
| `staged` | tentative source result; not available to graph consumers and not accepted success |
| `current` | accepted result available to graph consumers |
| `superseded` | historical result, including valid companions of a rejected/cleared source |
| `invalid` | rejected/invalid output, with its structured reason |

A target identity conflict stays red `failed` with class `config_error`; its panel shows `target_identity_conflict`, bound/proposed keys and the source artifact. No automatic retry is shown. For a scheduler-only source the node error comes from its durable failure event, not an invented attempt. Source results must be accepted together, and replay cannot change the state back to staged/current. Content expiry/forbidden access remains independently visible.

## Runs

The run's colour and word in the header, on the runs list and in the traffic light are derived: red `failed`; green `succeeded`; gray `cancelled`; amber `waiting for you` when the run is `running` and its only live node instances wait for a person; otherwise blue `running`; a recovery overlay always names retained holds on a terminal run and unknown/blocked execution on a live run without changing the API state word (`pending` is shown as `running` with "scheduling").

## Approval requests

| `approval_request.status` | on the review page and in Approvals |
|---|---|
| `pending` | amber; the deadline from the wait |
| `approved` | approved audit badge, approver/channel/time; current node is blue during verification, green after success, red on gate failure, or its current state after clear; audit alone does not set node colour |
| `rejected` | the node is red "rejected"; the response and its comments |
| `failed` | the node is red "configuration error"; on the review page and in Approvals, "approval could not be applied" with the response, the evaluation error kind and field, never shown as approved ([ir.md section 7.7](../../specs/ir.md#77-evaluation-errors)) |
| `expired` | the node is red "approval expired" or gray `skipped` per `on_expiry` |
| `invalidated` | the banner with the bound commit and the observed head; the node is red "candidate moved" |
| `cancelled` | gray; the run or node was cancelled |

Observation/delivery errors are read-only reasons beside the existing lifecycle state (ADR-0021). Show last success/time, current deadline, next poll/retry and structured hint; unknown delivery can persist on terminal history without keeping a run active or retaining execution capacity. Never label an outage solely as waiting for a person, an empty lookup as successful cleanup, or recorded approval as verified gate success.

## Attention states

The six states the architecture critique asked the console to keep apart ([architecture-critique-2026-10-16.md](../../reviews/architecture-critique-2026-10-16.md), "Priorities for the user-experience design"), each with what the console knows, what it does not, and the permitted action.

| state | where it shows | known | not known | permitted action |
|---|---|---|---|---|
| pending review | amber node and run; the Approvals waiting block; the review page | the bound commit, the facts and their provenance, the cost, the deadline | whether a reviewer on the code host has started | approve or reject on the review page; cancel |
| stale evidence | red node "candidate moved"; the review page banner; the Approvals history | the bound commit, the observed head, the producing node to retry | what the new push contains | retry the producing node with feedback |
| exhausted budget | red node "budget exhausted"; for a sub-graph, the element's segment | the budget, the spend, the attempt that crossed it | whether a larger budget would have succeeded | retry on a new graph version by a rerun; cancel |
| unknown publish outcome | runtime phase `publication` with a recovery reason and retained reservations | stored prepared revision/destination, operation, every pending dispatcher and reconciliation evidence | whether an earlier call can still take effect until authoritative closure | observe/stop via the operator command; wait for closure before retry or new publication; branch cleanup remains the graph author's command node |
| unknown execution | blue recovery reason on the node or admission wait; System backlog and Targets include terminal runs with retained holds | original domain/version, last observation, earlier dispatcher grants, retained scopes, evidence provenance and operator command | whether old execution or an in-flight publication can still take effect | observe or stop through `kora reconcile`; attest actual termination and closure with evidence if the authority is permanently inaccessible; cancel does not release unresolved holds |
| incompatible resume | the attempt's `strategy_fallback` in the panel and in the comparison | that `resume` fell back to `fresh_with_feedback` and why (runtime capability, missing handle, changed harness version) | what the lost session state held | nothing; the fallback is the designed behaviour |

## Notifications

A notification subscription ([ADR-0025](../../../docs/decisions/0025-notifications.md)) selects event kinds explicitly from this set, which matches the states that need a person:

| event kind | attention state | message |
|---|---|---|
| `approval.requested` | pending review | what waits for approval, with links to the review page and the change request |
| `approval.invalidated` | stale evidence | the approval lapsed because the candidate moved or upstream was retried |
| `approval.expired` | pending review that timed out | the approval expired without a decision |
| `run.failed` | exhausted budget, a failed gate, a run deadline or another terminal failure | the failing node and its error class, or the run deadline |
| `run.succeeded` | none | the run finished; optional, for teams that want completion messages |
| `attempt.recovery_required` | unknown execution, unknown publish outcome | an attempt entered recovery (lease expiry, an uncertain effect, cancellation or failure awaiting closure); the message names the reasons and the reconcile action |

Recoverable attempt failures that the retry policy handles are not in the set.
