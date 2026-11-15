# Disposable custody qualification environment

Prepared on 2026-11-15 for [#1640](https://github.com/realkarych/kora/issues/1640), against `9429aabbcf1a013a9cfe86400ba86516fa354c5d`. The owner confirmed that no qualification stand is available and requested its requirements. This is an environment request, not a qualification result, infrastructure purchase or permission to interrupt an existing machine.

The [accepted native method](../specs/execution-custody-native.md#6-qualification-method-and-activation-evidence) governs. The implementation can exercise native records, transactions, process loss, ownership and cleanup separately. Those results do not qualify system/storage failure or enable custody effects on an unqualified installation.

## Required roles and isolation

| Role | Required capability and boundary |
| --- | --- |
| Disposable core and local storage host | Runs the actual core, independent journal and local provider on the same host. Uses the supported native filesystem and real persistent device. Contains only replaceable qualification data and dedicated credentials. |
| Disposable PostgreSQL host | Supplies the separately identifiable database/server/storage failure domain for remote PostgreSQL cases. Core-only, database-only and combined cuts must be possible. |
| Independent observer and controller | Remains outside every cut domain. Durably records acknowledgments and original operation identities before commanding a cut, retains the evidence after it, and can recover the disposable hosts without relying on their lost process state. |
| macOS qualification target | A disposable supported Mac with APFS or HFS+, native full-sync observation and the same isolation. It may replace the core host for a sequential campaign; it cannot be the developer workstation. |

Linux uses the existing supported ext4 profile. A controller may share the observer host only if its storage and power stay independent of all tested cuts. A VM or container can supply process and guest-OS experiments, but a guest reset does not establish host-cache or physical-device power-loss behavior. A declared physical-power guarantee needs an actual isolated power interruption at that boundary. No shared database, production service, personal data or ordinary developer machine is a target.

The owner must identify the hosts, devices and power/reset control, confirm their exclusive disposable use, and authorize the exact cut mechanism before execution. The implementation coordinator does not provision paid infrastructure or infer permission from SSH access.

## Installation inputs to retain

- Exact kora revision and qualification workload/controller revisions; Python, drivers and native library/build identities. The inspected repository currently tests PostgreSQL with `postgres:16.15` in `tests/support/databases.py`; the actual server build and image digest must be retained rather than inferred from that tag.
- OS and kernel, filesystem type and mount options, native VFS, device/controller model and firmware, volatile caches, power-loss protection and the physical cut boundary.
- Complete placement of journal, metadata main/WAL files, provider root/control state and observer evidence. The journal must remain outside the metadata backup/restore set.
- Actual SQLite synchronization, checkpoint and macOS full-sync behavior, including failure/fallback observations; PostgreSQL server settings, cluster/database identity, replica policy and original-backend observations.
- Native path/file/process bindings, backup/replay tools and privileges, controller identity, original acknowledgment receipts and cut timestamps. Credentials are separate and do not enter reports or logs.

Ordinary core credentials retain their existing privileges. PostgreSQL checkpoint recovery uses a separate operator identity with the already-approved checkpoint privilege; it is not added to Worker or API credentials. Source/base-backup access is confined to the disposable installation.

## Capacity and resource accounting

Size each host from the declared workload before running it. The target needs the live metadata/provider/journal state, the admitted source and prepared staging high water, two complete metadata image locations where the image procedure needs both, their WAL/replay inputs, and retained failure artifacts. Every temporary image and native job needs a bounded reservation and an actual cleanup owner. The journal's 16 MiB main-page ceiling is not a bound on rollback journals, the whole installation or RSS.

The existing four finalizers, eight readers, four validators, two reserved cleanup admissions, 512 MiB encoded-buffer allowance and provider limits remain unchanged. Record actual process memory and throughput as diagnostics. This request introduces no hardware-memory minimum, new numerical acceptance threshold or increased product quota; the final workload inventory supplies the required disk reservation and concurrency fit.

## Execution and evidence

1. Establish explicit readiness barriers with the real native owners. The observer first persists the exact acknowledgment/operation identity outside the affected domain.
2. Exercise each accepted cut point, including after confirmed acknowledgment and before an unrelated flush/checkpoint. Trace the actual synchronization path. An incidental flush winning that window makes the sample inconclusive.
3. Cut the declared domain without graceful application, database or OS shutdown. Keep the observer/controller live. Never relabel a process termination as a system/storage cut.
4. Recover the original native installation and inspect the complete typed owners, original provider observations, frontier, pending commit, generations and retained charges. Equality of a counter or digest alone does not pass.
5. Exercise old and incomplete images, missing/substituted journal, paused old issuers, full ordinary registry with reserved recovery, and lost effect/acknowledgment responses under the same source/closure rules.
6. Include the accepted insufficient-persistence negative control only inside the disposable fixture. It must be rejected or expose the lost guarantee, not qualify an ordinary deployment.
7. Retain raw evidence, exact inputs, every failure and inconclusive case, actually released resources and all unexercised failure domains. Close or preserve each original owner explicitly before disposing of the stand.

Passing requires no lost acknowledged custody, no replacement of an unknown original transaction/effect, no invalid restore admission and no premature witness/charge/resource release. The method does not promise eventual resolution of every ambiguous original COMMIT. Any such unresolved outcome remains pending.

## Current status and handoff

No host, cut controller or qualifying result has been supplied. No destructive test has run. Physical qualification and production activation remain pending. To prepare execution, provide the role-to-host/device mapping, supported filesystems, independent observer storage, cut-control capability and explicit disposable-use authorization. The concrete workload and cut commands will then be made reviewable before they are run.
