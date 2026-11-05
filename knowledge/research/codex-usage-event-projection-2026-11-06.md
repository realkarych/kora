# Codex cumulative usage event projection

[#793](https://github.com/realkarych/kora/issues/793) connects the existing
confirmed native cumulative usage boundary to the shared usage event and
ledger models. It supplies no new runner, journal, persisted result or
model attribution source.

## Projection and provenance

The private one-shot controller retains one native snapshot and its trusted
receipt time. Before the matched turn-start acknowledgement, it publishes
nothing. A coherent burst can yield one latest total after confirmation.
Later advances and the first retained observation gap publish once each.
Duplicates and repeated gaps do not allocate another event or re-read
the usage clock. A gap-only revision retains the last coherent counts and
their original source time. The peer's `emittedAtMs` does not supply the
clock. Transport exit does not erase an already confirmed observation or
invent a final one.

The selected projection uses a caller-owned message id rather than the
native thread or turn string. Event and coverage have the same attempt
revision and source time. Known input separates uncached, cached and
cache creation; unknown cache writes also make uncached input unknown.
Wire zero is distinct from absence. Reasoning is a subset of output.
The shared count bounds apply to the snapshot selected for projection.
A regressive raw notification is reduced to a gap on the prior safe
counts. The known native context-fill shape fails the existing decoder
rather than creating an accounting event.

The emitted API-key event uses `price_table` as basis, no reported USD,
no model and no inferred cache TTL split. Its coverage is partial with
an attribution gap and any actual observation gap. The shared ledger
retains the observation, reports unavailable model coverage, and creates
no model row. This does not resolve aggregate final `SessionResult`
or native model attribution. The caller supplies distinct revisions. At exhaustion the projection
returns a fixed refusal and leaves the prior event intact; it cannot create
a new gap revision or renew the sequence. Final disposition remains with
the runner owner. No state proportional to stream length is added.

## Real Linux handoff

Observed on 2026-11-06 (Europe/Moscow). Pinned Codex CLI 0.166.0, upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, Linux amd64 musl archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
Pinned Python 3.11.17 base image digest
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
Docker Engine 29.9.2 / Buildx 0.38.1, LinuxKit 7.0.14 aarch64 VM with amd64
emulation. Use the [existing native probe prerequisites](codex-managed-hook-probe-2026-11-04.md).
Uncached builds fetch pinned assets; the fixture runs unprivileged with no
host mounts, operator credentials or external model calls.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-app-server-usage
```

The earlier [native usage mode](codex-app-server-usage-2026-11-05.md) retains
its two raw expected cumulative vectors and decoded replay assertion. The
new typed observer saw two confirmed vectors, with the exact final uncached
input of 95 and no observation gap. The fixture uses an injected test source
time. The turn completed, the probe verified the case and Docker confirmed
container removal. A separate coordinator observation found an empty final
probe-container inventory. The original 30/5/75-second
case, cleanup and attach limits are unchanged.

The actual staged provider and probe source matches executable
`c9d9f71e8ef2c87f930596df1e0f54463527beb3`, provider tree
`62a806a8f66dc2bf18c02e10c64cb92f75811383` and fixture tree
`d6960809cb4f10634db1dad3925d676d45cd46b9`. The early local test-only
refinement does not change Docker's staged files. Exit zero, 8.638s wall,
fixed diagnostic log SHA-256
`12befc49db4b894053275f22b32594392d597ebc60e7f62f39cf5f972b34f8db`.
The association and wall times are coordinator-recorded; timings are
diagnostic. This native fixture exercises the
stdlib handoff. It does not exercise shared Pydantic serialization or a
production journal in the isolated image.

## Shared event and ledger checks

At the same executable revision, Python 3.11.17 on macOS 27.2 arm64 with
locked uv 0.13.1:

```sh
uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  tests/plugins/harness/codex tests/runner/test_usage_ledger_completeness.py \
  tests/architecture --junitxml=/tmp/kora-793-local.xml
```

Exit zero, 1,581 tests (642 Codex including 15 new projection cases,
nine ledger and 930 architecture), zero failure/error/skip, 37.83s wall.
JUnit SHA-256 `f68b66b97c4f1eb5698139e268d39982c829c82047ec16a60f65350455c56283`.
Static checks: scoped strict Pyright, Ruff format/lint with `--ignore-noqa`,
shell syntax and Git whitespace all passed.

The socketpair drives the actual controller and the shared projection into
the existing conformance sink and real usage ledger. It exercises early
coalescing, first gap/replay, abort before and after confirmation, wrong
identity, absent and known zero counts, refusal above shared bounds,
unqualified source time, no stamp wrap and native fill refusal. A matching
printable native id and forged peer timestamp do not reach the event.
Only the conformance sink owns the test event list. No production stream
history, additional task or numerical gate is introduced.

## Remaining qualification

[#199](https://github.com/realkarych/kora/issues/199) owns actual runner
journal, time/revision authority, finalization and incomplete outcomes.
The selected adapter must still prove attributable model evidence, full
real-interface usage/failure/stop behavior and native macOS qualification
before complete usage is claimed. Resume, real provider calls and account
authentication remain separate. Whole CI timings are diagnostic.
