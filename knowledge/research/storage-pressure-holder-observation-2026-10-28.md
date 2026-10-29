# Pressure-fixture holder observation, 2026-10-28

## Retained failures

[Issue #363](https://github.com/realkarych/kora/issues/363) implements the
[accepted plan](https://github.com/realkarych/kora/issues/302#issuecomment-6455928291)
under its [owner approval](https://github.com/realkarych/kora/issues/302#issuecomment-6455980177).
The [fixture-size reconciliation](https://github.com/realkarych/kora/issues/363#issuecomment-6475566774)
updates only the implementation estimate under the owner's delegated autonomy.
Production storage, public contracts, manifests, deadlines, qualification inventories,
dependencies and workflows are unchanged.

The earlier busy-eject failures remain failed and unattributed. Source `8d8b766`,
[run 40723746858](https://github.com/realkarych/kora/actions/runs/40723746858),
failed `test_pressure_owner_closes_after_parent_interrupt[deadline-child_deadline]`
with `hdiutil detach`, exit 16, Resource busy. This is an actual cleanup failure.
It is distinct from #396's 480-second qualification failure. Diagnostics do not
repair either failure. #302 remains open for natural attribution and its own
three-consecutive-clean-run criterion.

## Observation schema and limits

Only a macOS detach returning `command_failed` receives one `holder_observation v1`
exception note. Successful detach, deadline and terminated-command paths do not
run the observer. Linux receives no note. The original exception object, code,
message, stderr bytes, command journal and private stderr file are preserved.
An observer defect propagates with the original detach failure as its context.

Capture and both revalidations bind up to eight owned entities' block/raw nodes
by type, device, inode and rdev, and bind the mounted root by device/inode.
Each performs at most seventeen lstat calls. Raw and block rdev must agree.
Mounted-to-unmounted after detach is expected when both observation checks agree;
volume matching is then disabled. Unexpected identity/state changes suppress all
holder details. An unmounted directory must retain owner device, effective UID
and 0700 directory mode.

The observer takes one census of at most 8192 PIDs. It inspects only same-UID
references through cwd/root, up to 4096 descriptors per process and a single
mapped-volume query. O_EVTONLY descriptors are excluded. Device targets match
exact devfs device/inode pairs; volume references match the mounted device.
Two complete sampled Darwin identities, including unique ID, parent unique ID
and version, bracket each process's reference and enrichment reads. Reused or
exited identities lose their details. A fixed twelve-prefix census admits only
stable processes, counts instances and retains the newest unique ID per prefix.

At most eight holders retain details and six references each; further holders
and references are counted. Limits render explicit truncation tokens. The note
is printable ASCII/LF, at most 6144 bytes, and reserves its header, timings,
errors, targets, scope, volume entries and truncation state before optional lines.
Errors are counted per fixed query/errno token; the rendered aggregate is capped
at 512 bytes. Descriptor-race EBADF/ENOENT outcomes are counted and skipped.

The six literal volume entries are `.Spotlight-V100`, `.fseventsd`, `.Trashes`,
`.TemporaryItems`, `.metadata_never_index` and `.DocumentRevisions-V100`.
Only their lstat types are retained through a no-follow, read-only root descriptor
closed in finally, after process enumeration. No other volume names or content
are read for display. Comm ends at its first NUL within sixteen bytes and accepts
only the fixed ASCII token alphabet. Executable paths are either `fixture-python`,
canonical bounded paths under the accepted system roots, or `other`; spaces are
escaped. `/System/Volumes/`, noncanonical and colon-containing paths are excluded.

## Native contract

One literal CDLL load uses `/usr/lib/libproc.dylib` with errno enabled and only
`proc_listallpids`, `proc_pidinfo`, `proc_pidfdinfo` and `proc_pidpath`.
Every signature is explicit and every buffer length is passed directly. Errno is
cleared before each call and read afterwards. Exact replies require 136, 2352,
64, 56, 1272 or 176 bytes for their selected flavors; census, fd-list and path
replies have their own count/alignment bounds. Silent zero on an exact-size
flavor is a layout failure. Parsing uses only owned byte buffers and unpack_from.

Pinned layouts come from Apple's
[proc_info.h](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/sys/proc_info.h)
and [proc_info_private.h](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/sys/proc_info_private.h).
The short-BSD implementation copies MAXCOMLEN bytes and explicitly sets byte
MAXCOMLEN-1 to NUL ([source](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/proc_info.c#L774-L782)).
Consequently the sixteen-character `diskarbitrationd` label is compared through
its fifteen-character native prefix. This is candidate classification, not proof
of executable identity. The observer never reads pbi_name, vip_path, argv or env.

## Evidence path

`child_diagnostic` keeps bounded stderr tails, escapes controls except LF,
neutralizes double colons and applies its cap after escaping. Recovery and
controller assertion messages are strings, preserving useful diagnostics under
pytest -q. Single messages retain at most 24 KiB; each of the two marked controller
diagnostic parts retains at most 8 KiB. No assertion condition is relaxed.

The existing busy regression owns a synchronized child with cwd, a regular-file
descriptor, a read-only mapping and the whole raw-device descriptor. It checks
native attribution, binding, candidate census and original failure persistence.
JUnit suite properties `pressure_holder_observation_ms` and
`pressure_busy_detach_phase` retain total/per-step timings and native phase.
Totals include native initialization, revalidation, enumeration, entries and
rendering. The final equal-width timing substitution has a small unmeasured tail.
Any final-head hosted macOS sample above 1000 ms requires re-planning before merge;
every such sample counts. A later hosted total above 2500 ms triggers revisiting.

Direct-file tests own and reap synchronized children without creating pressure
owners or changing qualification inventory. An injected checkpoint clock observes
the active scan frame to expire inside the specific owned holder's fd loop,
independently of native PID ordering; it replaces no native behavior. Tests also
exercise pre-pass and after-first-PID expiry, final revalidation and descriptor
equality, census/fd/holder limits, content policy and module-contract escape cases.

## Design gates

The [recorded one-pass local gates](https://github.com/realkarych/kora/issues/363#issuecomment-6471373214)
on macOS 27.0.1 arm64 found verbose/debug holder PID absent, both block/raw opens
successful, raw-only busy detach in eject phase, and owned cleanup successful.
The flag-only and reduced-device branches therefore do not apply. Local results
do not qualify hosted macOS 15. The final-proposal design record is
[retained separately](https://github.com/realkarych/kora/issues/363#issuecomment-6472023357);
actual implementation review and final-head execution evidence belong to the PR.

## Residuals

The 5-second budget bounds cooperative own work between checkpoints. It cannot
interrupt an in-flight native call or filesystem getattr/path lookup. Checks run
before enumeration, before each PID, after each 256 fd queries and before entries;
final revalidation and rendering always run after expiry. Existing outer command,
controller, recovery and job deadlines remain unchanged. The busy regression uses
an intentionally non-expiring injected budget; elapsed times never decide its
attribution assertions. An interrupted observer writes nothing and leaves recovery
authority in the already-persisted journal.

Only same-UID references are visible. Holders are observation-time candidates,
never proof of the earlier failure or cleanup authority. Zero means no visible
same-UID holder. Thread working directories and mapped device nodes are unchecked.
Devfs recreation with the same identity and remount elsewhere remain undetected.
Natural hosted attribution remains unverified until a recurrence surfaced through
pytest carries both the detach line and the note in existing log/JUnit artifacts.
The unchanged nightly recover_fixtures catches PressureFailure and retains only
its code, discarding notes; no broader nightly retention is claimed.

## Remedy evaluation

No force, retry, signal, indexing change, privilege or holder exemption is added.
Spotlight evidence fits unmount interference, while retained failures show eject.
Never-index and holder-exclusion remedies remain deferred until a natural unmount
failure shows `.Spotlight-V100=dir` with an mds/mdworker holder or candidate created
after detach, or DiskArbitration dissenter evidence names an mds-family process.
Any remedy requires a separate storage issue and decision record. A natural busy
eject with holders=0 triggers the owner's separate diskutil dissenter-reporting
plan, with its own review and approval; this observer does not implement it.
The trigger recurred on 2026-10-29. [Issue #419's bounded native-release implementation](storage-pressure-native-eject-2026-10-29.md) uses the owner's subsequent autonomous routine-fix authorization, preserves actual release checks and retains both private refusal streams without claiming complete dissenter attribution.
