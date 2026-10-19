# Research: a local kora-runner that a restarted worker adopts

Checked on **2026-10-19** on one Apple silicon MacBook Pro: macOS 27.0.1 (Darwin 27.0.0, arm64), APFS on SSD, CPython 3.11.17, standard library only. This is the probe of [issue #115](https://github.com/realkarych/kora/issues/115). The prototype is throwaway code outside the repository (about 2,000 lines: `runner.py`, `worker.py`, `candidate.py`, `common.py`, a scenario driver, a checker and two micro-experiments). `$P` in the evidence stands for its directory. A Python script plays the agent: it emits a JSON event every 200 ms for 10 s, writes `out/result.txt` in its candidate root and starts a child process that ignores `SIGTERM`. Linux, Docker and the real harnesses (Claude SDK, Codex app-server) were not exercised. Nothing below is a protection claim: `local` remains a trusted development runtime ([ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md)).

## Result

On macOS, all five outcomes of issue #115 held:

1. A worker killed with `SIGKILL` mid-session left the runner and the candidate running.
2. A new worker incarnation found the runner through the on-disk registry and rejected a reused process id. It adopted the runner and resumed the event stream from its acknowledged offset, with no duplicate, no gap and no second candidate.
3. When the runner itself was killed, the next worker reported "runner lost" with the last journal state and the leftover processes, and did not restart work.
4. Bytes moved both ways with SHA-256 checks, and the injected corruption was detected in both directions.
5. Retirement left no orphan process. The one exception is a descendant that escaped the process group: it is covered by the negative finding below.

Three findings need a decision before implementation:

- **A crashed worker cannot close its own dispatcher.** Under the current wording of [ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md), only the originating worker can acknowledge that its dispatcher closed. A worker killed with `SIGKILL` never can, so adoption after a worker crash would stay observational forever. That is the blocked run the owner rejected. The probe closed the dead worker's dispatcher with runtime-observed evidence instead: the kernel-attested peer process had exited and the runner had seen all its connections close. The specification has to allow that kind of evidence.
- **A local termination receipt can be wrong.** A descendant that leaves the process group with `setsid` and runs a platform binary is invisible to both the process-group check and the environment sweep. The receipt then reports "affirmative" while that process is still running (scenario D).
- **The channel and transfer are undefined.** The specifications do not define the transfer channel, the socket location, how the dispatcher is authenticated or the required durability level. The probe's choices are recorded below as candidates.

Recommended process model: **one detached `kora-runner` process per attempt**, found through a registry on disk for each execution domain.

## Process model that was exercised

**Layout per execution domain.** The state root has mode 0700.

- `domain.json` holds an `execution_domain_id` that is created once. Registry entries carry it, and lookup rejects any other lineage.
- `registry/<attempt>.json` is replaced atomically: write a temporary file, fsync it, rename it over the old one, fsync the directory. Writes are serialized by `flock` on `registry/<attempt>.lock`, which the kernel releases when a worker dies. The states are `starting` (written before the spawn), `running` (written by the runner), `retiring` (the fence) and `retired`. A retired entry is a tombstone that keeps the receipt and drops the process id and socket.
- `attempts/<attempt>/control/` (0700) is the private control directory. It holds:
  - `secret`: 32 random bytes, mode 0600, created with `O_EXCL|O_NOFOLLOW`;
  - `request.json`: the saved request and its digest;
  - `journal.jsonl` and `checkpoint.json`;
  - `ctl.sock` (0600);
  - the candidate's `stdout.log` and `stderr.log`, and `runner.log`.
- `attempts/<attempt>/input/` (0700) is the private input root, outside the candidate root.
- `attempts/<attempt>/candidate/` is the candidate root.

**A spawn that survives the worker.**

1. The worker starts the launcher with `Popen(runner.py, start_new_session=True)`. Its stdin is `/dev/null`, stdout and stderr go to `runner.log`, file descriptors are closed, and one readiness pipe is passed through `pass_fds`.
2. The launcher forks once and exits, so the daemon is reparented to launchd (ppid 1) at once and is not a session leader.
3. The daemon sets `umask 077`, changes into the control directory, ignores `SIGHUP` and runs one asyncio loop.
4. It journals `runner.started`, binds the socket and writes the `running` registry entry with `{pid, start_us}`. Only then does it write the readiness line.
5. The worker waits on the pipe with a deadline. It reaps only the launcher, which exits at once, so no zombie accumulates in a long-lived worker.

**Candidate group.** The runner starts the candidate with `start_new_session=True`: its own session and process group, with pgid equal to the leader's pid.

- cwd is the candidate root.
- The environment is constructed: `PATH`, `HOME`, `KORA_INPUT_ROOT` and a random `KORA_ATTEMPT_TOKEN`.
- stdin is `/dev/null`, stdout is a pipe to the runner, and stderr is a file in the control directory.

**Journal.** Each `{i, at, kind, data}` record is one canonical JSON line. It is written with a single `write(2)` on an `O_APPEND` descriptor and followed by `fsync(2)`. Harness stdout lines become `harness` records. Supervisor facts (`runner.*`, `phase`, `transfer.*`, `control.*`, `dispatcher.*`) share the same sequence. Readers ignore a trailing partial line.

**Control channel.** The channel is an `AF_UNIX` stream socket. Frames are a 4-byte length plus a 1-byte type: `J` for JSON, `B` for bytes, `E` for one journal line. The maximum frame size is 1 MiB.

Handshake:

1. Before the handshake, the worker checks that the socket's peer pid (`LOCAL_PEERPID`) equals the registry pid and that this pid's start time matches the recorded one.
2. The runner checks the peer uid (`LOCAL_PEERCRED`), records the peer pid and sends a nonce.
3. The worker answers with `HMAC-SHA256(secret, "kora-ctl-v1|client|<attempt>|<server nonce>|<client nonce>")`, its own nonce, its dispatcher id and its incarnation id.
4. The runner verifies the answer and replies with the matching `server` HMAC. The worker therefore also authenticates the runner, and an impostor socket fails.

Operations: `status`, `heartbeat`, `adopt`, `put`, `start`, `events{from}`, `get`, `cancel`, `retire`, `record_dispatcher_quiescence`.

**Lookup.** A process is identified by its pid plus its start time in microseconds from `proc_pidinfo(PROC_PIDTBSDINFO)`.

- No entry: absent.
- `retired`: return the tombstone.
- `starting`: retryable error.
- Dead or reused pid: `lost`, with a report of the journal tail, the last phase, the number of candidate starts and any leftover candidate processes.
- Otherwise: authenticate and send a heartbeat.

**Adoption.** `adopt` returns the phase, the journal position, the candidate identity and its group members, the sealed outputs and the dispatcher table. For each earlier dispatcher, the table holds the incarnation, the kernel-attested pid and start time, the number of open connections and the quiescence record.

- An earlier dispatcher whose process no longer exists and has no open connections is closed: the runner journals `dispatcher.quiescence` with provenance `runtime_observed_process_exit`.
- If an earlier worker process is still alive (for example paused), adoption stays observational.

Events resume from the offset the API side acknowledged, on a dedicated connection. The receiver appends only `i == next`, skips byte-identical duplicates and rejects conflicts and gaps.

**Transfer.**

- Push (input into the private root):
  1. The worker sends a header `{name, size, sha256}` whose digest is known in advance, then 64 KiB `B` frames, then `put_end`.
  2. The runner writes a temporary file relative to the input-root descriptor (`O_CREAT|O_EXCL|O_NOFOLLOW`), hashes it while writing and fsyncs it.
  3. On a match the runner sets mode 0400, renames the file into place with `renameat` and journals `transfer.materialised`. On a mismatch it unlinks the file and journals `transfer.rejected`.
  4. `start` re-hashes every input immediately before spawning.
- Pull (outputs back to the worker), allowed only after the candidate domain has stopped:
  1. The runner journals a sealed manifest, `runner.outputs_sealed`, with the SHA-256 of `out/**`, `stdout.log` and `stderr.log`.
  2. It streams the file in chunks and sends a trailer digest.
  3. The worker keeps the file only if the received digest, the trailer and the sealed manifest all agree.

Paths are opened component by component with `O_NOFOLLOW` beneath a directory descriptor. Only the two logs can be exported from the control directory.

**Retirement.** The worker takes the lock, writes `retiring` and calls `retire{grace}`. The runner then:

1. Sets its own fence, so later `put` and `start` calls are rejected.
2. Sends `killpg(SIGTERM)` and polls the non-zombie group members until the grace period ends.
3. Sends `killpg(SIGKILL)` and waits for the group to be empty.
4. Sweeps every same-uid process for `KORA_ATTEMPT_TOKEN` through `sysctl KERN_PROCARGS2` and kills the matches.
5. Waits until the leader has been reaped.
6. Journals the `failed` phase if needed, and the receipt as the final record, `runner.retired`.
7. Flushes that record to the event followers, unlinks the socket and exits.

The worker then confirms that the runner process is gone and writes the tombstone. `release` deletes the attempt directory but keeps the tombstone, which rejects any later `submit`.

If the runner is already dead, the worker-side runtime performs the same stop from the journal and the checkpoint. It first checks that no group member started before the recorded leader, which would mean the pgid had been reused. It then appends `runtime.retired_without_runner` itself.

## Evidence

Driver output, trimmed. Process ids are from one clean run.

### Worker killed mid-session; a new incarnation adopts (outcomes 1–3)

```text
$ .venv/bin/python worker.py run --attempt att-adopt --duration 10 --input-mib 4 > logs/A-worker1.log 2>&1 &
$ kill -9 63122
# worker-1 killed at 03:49:11.128; API store holds 20 acknowledged events
$ ps -o pid,ppid,pgid,stat,args -p 63124,63126,63125
  PID  PPID  PGID STAT ARGS
63124     1 63123 S    $P/.venv/bin/python $P/runner.py --attempt att-adopt --ready-fd 6
63125 63124 63125 Ss   $P/.venv/bin/python $P/candidate.py --duration 10.0
63126 63125 63125 S    $P/.venv/bin/python $P/candidate.py --role child
$ .venv/bin/python worker.py submit --attempt att-adopt --duration 10 --input-mib 4
submit is idempotent: live runner pid=63124 already registered; no spawn
$ .venv/bin/python worker.py submit --attempt att-adopt --duration 10 --input-mib 4 --changed-request
ERROR workload_conflict: same attempt id, different execution request digest
# no worker attached for 1.5 s; the runner keeps journalling
$ .venv/bin/python worker.py run --attempt att-adopt --grace 1
lookup -> live
  check: naive kill(pid 63124, 0) succeeds -> a process with that pid exists
  check: identity check (proc_pidinfo start time): pid 63124 start_us 1792370949012701 matches
  check: socket peer pid 63124 (LOCAL_PEERPID) == registry pid; mutual HMAC challenge ok
  check: heartbeat: phase=execution candidate_alive=True journal_next=32
earlier dispatcher disp-246e5abb (worker pid=63122): pid 63122 does not exist; runner recorded quiescence ok=True {'kind': 'runtime_observed_process_exit', ...}
adopted: phase=execution journal_next=34 candidate={'pid': 63125, 'pgid': 63125, ..., 'group_members': [63126, 63125]} observational=False; resuming from acknowledged offset 20
  event i=20 kind=control.detached data={"dispatcher_id": "disp-246e5abb"}
  ...
  event i=72 kind=phase data={"phase": "complete", "returncode": 0}
pulled {'name': 'candidate:out/result.txt', ..., 'ok': True}   (stdout.log, stderr.log likewise)
retired: {"path": "runner", "retire_ms": 0.3, "worker_retire_ms": 8.7, "runner_exit_confirmed": true}
# worker-2 exec -> first resumed event (incl. interpreter start): 57 ms
$ .venv/bin/python verify.py att-adopt
journal records (runner, final copy): 78  i=0..77
API event store records:              78  i=0..77
duplicate i in API store: none; missing i: none; contiguous: True
byte-identical to journal: True  sha256(api)=c8c5cab3f771cdba sha256(journal)=c8c5cab3f771cdba
runner.started records: 1; runner.candidate_started records: 1
candidate ticks journalled: 50; seq contiguous 0..49: True
  {"event": "attached", "dispatcher_id": "disp-246e5abb", "mode": "submit", "resume_from_offset": 0}
  {"event": "attached", "dispatcher_id": "disp-0542a8c4", "mode": "adopt", "resume_from_offset": 20}
$ pgrep -fl '[p]robes/runner/(runner|candidate)\.py|[w]orker\.py|[s]leep 4[0-9]'
(no matching process)
$ .venv/bin/python worker.py submit --attempt att-adopt --duration 10 --input-mib 4
ERROR retired: attempt has a retirement fence; submit rejected
```

While no worker was attached, `pgrep` showed exactly one runner, one candidate leader and its child. Events 20–30 were produced with no worker attached and arrived from the journal backlog.

**Paused worker.** Here worker-1 is stopped with `SIGSTOP` instead of killed.

```text
$ kill -STOP 63167
$ .venv/bin/python worker.py run --attempt att-paused
earlier dispatcher disp-d8704481 worker pid=63167 still alive -> adoption stays observational
adoption is observational until every earlier dispatcher closes; no effects, no output collection
[exit 7]
$ kill -9 63167
$ .venv/bin/python worker.py run --attempt att-paused --grace 1
earlier dispatcher disp-d8704481 (worker pid=63167): pid 63167 does not exist; runner recorded quiescence ok=True
adopted: phase=execution ... observational=False; resuming from acknowledged offset 12
$ .venv/bin/python verify.py att-paused
journal records (runner, final copy): 60 ... duplicate i: none; missing i: none; byte-identical to journal: True
runner.candidate_started records: 1
```

### Runner killed: reported distinctly, never restarted

```text
$ kill -9 63188          # worker-1
$ kill -9 63190          # runner
# candidate leader 63191 exited 197 ms after the runner died (EPIPE on its stdout pipe)
$ ps -o pid,ppid,pgid,stat,args -p 63190,63191,63192
  PID  PPID  PGID STAT ARGS
63192     1 63191 S    $P/.venv/bin/python $P/candidate.py --role child
$ .venv/bin/python worker.py run --attempt att-lost
lookup -> lost
  check: naive kill(pid 63190, 0) -> ESRCH
  check: identity check (proc_pidinfo start time): pid 63190 does not exist
  journal_last: {'i': 15, 'at': '2026-10-19T00:49:29.356049+00:00', 'kind': 'control.detached'}
  journal_last_harness: {'i': 14, 'data': {'seq': 5, 'type': 'tick'}}
  last_phase: execution
  candidate_starts: 1
  leftover_candidate_processes: [{'pid': 63192, 'ppid': 1, 'pgid': 63191, 'via': 'process group'}]
RUNNER LOST: ... Not restarting work under this attempt id; recovery must retire the original domain (retire) before any replacement.
[exit 3]
$ pgrep -fl ...
63192 $P/.venv/bin/python $P/candidate.py --role child
# pid-reuse simulation: the registry entry is pointed at a live /bin/sleep
$ .venv/bin/python worker.py lookup --attempt att-lost
  check: naive kill(pid 63204, 0) succeeds -> a process with that pid exists
  check: identity check (proc_pidinfo start time): pid 63204 exists but started at 1792370969664718 (recorded 1792370968266710): pid reused by 'sleep'
lookup -> lost
$ .venv/bin/python worker.py retire --attempt att-lost --grace 1
receipt: {..., "path": "worker_fallback_runner_lost", "evidence": {"runner_lost": "pid 63190 does not exist",
  "domain_stop": {"pgid": 63191, "members_before": [63192], "term_sent": true, "members_after_grace": [63192],
  "kill_sent": true, "group_empty": true, "swept_escaped": [], "sweep_empty_after": true, "duration_ms": 1055.2}}}
$ .venv/bin/python worker.py retire --attempt att-lost --grace 1
retire is idempotent: tombstone already present, returning the recorded receipt
$ pgrep -fl ...
(no matching process)
```

### Bytes over the channel with digests (outcome 4)

```text
# input push corrupted in transit: one byte of chunk 2 flipped after the sender hashed it
$ .venv/bin/python worker.py submit --attempt att-badput --input-mib 8 --fault-push
ERROR integrity_error: {'expected': '05d9a749…', 'received': '5921ffc6…'}
$ ls -la state/attempts/att-badput/input/     -> empty; journal has transfer.rejected and no runner.candidate_started
# 64 MiB input; the candidate copies it to out/copy.bin
pushed input.bin 67108864 B sha256=474a8ca6b80d0a77.. verified by runner, 125.3 ms (511 MiB/s)
-r-------- input/input.bin; drwx------ attempt, control, input; srw------- control/ctl.sock; -rw------- secret, journal
# output pull corrupted in transit: the runner flips a byte after hashing
$ .venv/bin/python worker.py pull --attempt att-xfer --root candidate --name out/copy.bin --fault flip_byte
pull -> {"received_sha256": "9fe5219d…", "trailer_sha256": "474a8ca6…", "sealed_sha256": "474a8ca6…", "ok": false,
         "error": "integrity_error: received bytes do not match the runner digest; nothing stored"}
$ .venv/bin/python worker.py pull --attempt att-xfer --root candidate --name out/copy.bin
pull -> {"size": 67108864, "received_sha256": "474a8ca6…", "trailer_sha256": "474a8ca6…", "sealed_sha256": "474a8ca6…", "ms": 152.1, "ok": true}
   (out/result.txt, stdout.log, stderr.log likewise ok)
$ .venv/bin/python worker.py pull --attempt att-xfer --root candidate --name ../control/secret
ERROR get_failed: {'ok': False, 'error': 'protocol_error', 'detail': "bad path '../control/secret'"}
$ .venv/bin/python worker.py pull --attempt att-xfer --root control --name journal.jsonl
ERROR get_failed: {'ok': False, 'error': 'protocol_error', 'detail': 'control file not exportable'}
$ shasum -a 256 api/att-xfer/input.bin api/att-xfer/outputs/copy.bin    -> both 474a8ca6…; result.txt: input_sha256=474a8ca6…
```

### Retirement (outcome 5) and its limit

```text
$ ps -o pid,ppid,pgid,stat,args ...
63285     1 63284 S    runner.py --attempt att-cancel
63286 63285 63286 Ss   candidate.py --duration 30.0 --escape --escape-platform-binary   # leader
63287 63286 63286 S    candidate.py --role child       # same group, ignores SIGTERM
63288 63286 63288 Ss   candidate.py --role escaped     # setsid, own group
63289 63286 63289 Ss   /bin/sleep 40                   # setsid, own group
$ .venv/bin/python worker.py retire --attempt att-cancel --grace 1.5
"domain_stop": {"pgid": 63286, "members_before": [63287, 63286], "term_sent": true, "members_after_grace": [63287],
  "kill_sent": true, "group_empty": true, "swept_escaped": [{"pid": 63288, "pgid": 63288, "ppid": 1}],
  "sweep_empty_after": true, "duration_ms": 1586.0}, "retire_ms": 1589.4, "runner_exit_confirmed": true
$ pgrep -fl ...
63289 /bin/sleep 40          # survived although the receipt is affirmative; removed by hand
```

`KERN_PROCARGS2` returned the arguments of `/bin/sleep` but no environment: the kernel withholds the environment of platform binaries. For the Python descendant the sweep worked. When every process honours `SIGTERM` (grace 5 s), the domain stopped in 53.6 ms without `SIGKILL`; the worker-side retirement took 66.5 ms.

**macOS responsibility attribution.** A separate experiment checked whether macOS attribution survives escape. A leader was spawned through `posix_spawn` with the private `responsibility_spawnattrs_setdisclaim`, and it double-forked `/bin/sleep` after `setsid`:

```text
disclaim=False: escaped /bin/sleep ppid=1; responsible pid while leader alive=53266 (the desktop app), after leader exit=53266
disclaim=True:  escaped /bin/sleep ppid=1; responsible pid while leader alive=62797 (the disclaimed leader), after leader exit=62799 (itself)
```

Attribution survives `setsid`, a double fork and a platform binary, but only while the disclaimed process is alive. One possible design is a disclaimed "domain anchor" process that stays alive until retirement and launches the harness. Its enumeration would find every escaped descendant. The interface is private and can change in any macOS release, so it is only a candidate.

### Microbenchmarks

```text
journal append + fsync(2)      x200: median 0.025 ms, p95 0.044 ms, max 0.110 ms
journal append + F_FULLFSYNC   x200: median 3.905 ms, p95 4.659 ms, max 5.776 ms
identity via proc_pidinfo(PROC_PIDTBSDINFO): 2.23 us/call
identity via `ps -o lstart=` (1 s resolution): 3.11 ms/call; output 'Mon Oct 19 03:48:35 2026'
environment-token sweep over 754 processes: 15.6 ms
bind absolute socket path of 146 bytes: OSError: AF_UNIX path too long
socket mode 600: same-uid connect succeeded; socket mode 0: same-uid connect failed: [Errno 13] Permission denied
```

## Timings (one machine, rough)

| Measurement | Value |
|---|---|
| Runner spawn: launcher, daemon fork, interpreter, bind, registry, readiness | 39–41 ms |
| `submit` including a 1 MiB push and candidate start | 45–48 ms |
| Adoption inside the new worker: lookup (identity, authentication, heartbeat) + `adopt` with dispatcher closure + first resumed event | 1.0–1.3 ms + 0.8–0.9 ms + 0.5–0.7 ms |
| New worker `exec` to first resumed event, including interpreter start | 57 ms |
| Runner death to harness exit through `EPIPE` (5 events/s) | 197–217 ms |
| Retirement, cooperative candidate | 54 ms domain stop, 67 ms worker side, 0.12 s CLI wall |
| Retirement with a child ignoring `SIGTERM`, grace 1.5 s | 1.59 s |
| Retirement after normal completion (domain already stopped) | 0.3 ms runner, 8–14 ms worker including runner-exit confirmation |
| Push / pull of 64 MiB with SHA-256 and fsync | 125 ms (~510 MiB/s) / 152 ms (~420 MiB/s) |
| Runner resident memory (Python, asyncio) | ~16 MiB |

## Mapping to `Runtime` (protocols.md section 2)

| Method | Mechanism in the probe |
|---|---|
| `capabilities` | Not exercised. Local values follow the runtime table: `protected_execution`, `portable_handles` and `supports_resume` are false. |
| `execution_domain_id` | `domain.json` created once per state root. Each registry entry binds it, and lookup raises a lineage error on mismatch instead of returning `None`. |
| `workspace_target` | Worker-side allocation of `candidate/`, `input/` and `control/` with their modes. Needs no runner. |
| `submit` | Under the registry lock: the tombstone rejects; an existing entry with the same request digest returns the same handle without spawning; a different digest gives `workload_conflict`. Otherwise: secret, `request.json`, a `starting` entry, double-fork spawn with a readiness pipe, `put` of the inputs, then `start` (re-hash, spawn the candidate group). |
| `lookup` | Registry read under the lock, then process identity (pid + `start_us`), socket peer pid and mutual HMAC. Returns live, `lost` with the journal tail and leftovers, retired, or absent. `starting` is a retryable error. |
| `adopt` | `adopt` returns phase, journal position, candidate identity, sealed outputs and the dispatcher table. Dead earlier dispatchers are closed; a live earlier one keeps the adoption observational. |
| `status` | `status`. preparation maps to `starting`, execution to `running`, complete or failed to `exited(code)`; a lost runner comes from lookup. |
| `events` | `events{from}` on its own connection. It streams journal lines from the offset, follows appends and ends after the final record. The receiver deduplicates by `i`. The runner notices a closed follower through EOF, not only on the next write. |
| `heartbeat` | `heartbeat` reports the runner alive, the candidate group alive, `journal_next` and the time of the last record. |
| `finalize` | Not exercised. It would be one more operation on the same authenticated channel carrying `PublicationAuthorization`, never candidate stdio or files. |
| `cancel` | `cancel{grace}` stops the domain without a fence. Implemented but not exercised. |
| `recovery` | Partly: the lost report, the dispatcher table and the tombstone receipt. Not yet a single call. |
| `record_dispatcher_quiescence` | The runner checks that the dispatcher process exited and has no open connections, then journals `dispatcher.quiescence`. |
| `retire` | Lock, `retiring` fence, runner `retire{grace}` (TERM, grace, KILL, environment sweep, final journal record, exit), runner-exit confirmation, tombstone with receipt. With the runner gone, a worker-side fallback with a pgid-reuse check. Idempotent through the tombstone. |
| `release` | Deletes the attempt directory (candidate, input, control journal) after the tombstone exists. The tombstone remains. |

## Contradictions and gaps in the specifications

1. **A crashed worker cannot close its dispatcher. This contradicts the decision to adopt.**
   - What the specification says: `record_dispatcher_quiescence` "authenticates the originating incarnation", and protocols.md says "a process heartbeat gap or runtime receipt cannot certify" closure.
   - Why that blocks: a worker killed with `SIGKILL` never acknowledges. Under ADR-0017 adoption stays observational and `recovery_required` never clears, so the run stays blocked.
   - What the probe showed: on one host the runtime has stronger evidence than a heartbeat gap. It has the kernel-attested peer pid and start time from attach time, proof that this process no longer exists, and the observation that all its connections closed. Every runtime effect from that dispatcher passed through the runner, so none can still be in flight.
   - What to add: a provenance kind such as `runtime_observed_process_exit`, limited to runtime-side effects. An external call the dead worker made itself (a code-host request) still needs the publication-outcome reconciliation that already exists.
   - What happens otherwise: a paused worker stays unresolved, which matches the specification.
2. **The transfer channel is undefined.** The probe's candidate design:
   - It shares the authenticated control channel and uses chunked frames.
   - A push carries its digest in the header, because the expected digest is known in advance. A pull carries it in a trailer and is also checked against a sealed manifest in the journal.
   - The receiver writes a temporary file and renames it only on a match. Every transfer is journalled.
   - For resources, `test_resource_digest_mismatch_fails_attempt` makes a transit mismatch fail the attempt. For outputs nothing is specified. Retrying a pull from the sealed, immutable source is safe and should be allowed.
3. **The socket location is unspecified, and `sun_path` is short.** It is 104 bytes on macOS and 108 on Linux. The probe directory alone is 108 bytes, and the absolute socket path was 146, which fails with `AF_UNIX path too long`. The probe binds a relative name from the control directory and connects from a fixed worker cwd (the state root). Alternatives:
   - a short per-user directory, such as `/tmp/kora-<uid>/` or `$XDG_RUNTIME_DIR`, verified to be 0700 and owned by the user;
   - never Linux's abstract namespace, which has no filesystem permissions.
4. **How `submit` and transfer are ordered is unspecified.** `submit(workload)` is one call, but inputs must be in place before the candidate starts. The probe runs a preparation phase inside `submit`: spawn, `put`, `start`.
   - A crash between the spawn and `start` is resumable, because `submit` is idempotent and continues a runner in preparation once earlier dispatchers are closed. This path was designed but not exercised.
   - A `starting` registry entry whose runner never registered has no resolution rule yet. Candidates are a launch nonce found in the process table, or retirement after a bound.
5. **Supervisor death is unspecified.** With a stdout pipe the harness dies on its next write, about 200 ms later. Descendants that do not write to the pipe survive as orphans with ppid 1. `retire` therefore has to work without the runner, and the runtime must append the final journal record itself. The specification covers `retire` without a handle, not without a supervisor.
6. **The durability level is unspecified.** On macOS `fsync(2)` does not flush the drive cache. Surviving power loss needs `F_FULLFSYNC`, which costs about 3.9 ms per record instead of 0.025 ms. Surviving a process crash needs only `write(2)`. The specification should say which failure the journal and registry must survive. Bursty harness streams under `F_FULLFSYNC` need group commit.
7. **A local receipt can be affirmative while a descendant still runs.** Process groups do not contain `setsid` descendants, and the environment sweep cannot see platform binaries (scenario D). ADR-0016 permits weaker local termination, but "only an affirmative `TerminationReceipt` establishes quiescence" treats every receipt alike. The receipt should name the scope of its evidence (`process_group+env_sweep`). Stronger mechanisms per OS:
   - macOS: the responsibility anchor (private interface);
   - Linux: a cgroup v2 per attempt with `cgroup.kill`, or `PR_SET_CHILD_SUBREAPER` on the runner.
8. **Dispatcher authentication is unspecified.** The probe's secret belongs to the attempt, not to a dispatcher, and the dispatcher id is self-asserted inside an HMAC-authenticated session. The specification says only "opaque dispatcher identity bound to that grant". It should say what proves the dispatcher id: for example an API-issued per-grant token that the runner can verify, or same-host peer identity for `local`.
9. **The journal sequence is shared.** Control records (attach, detach, adopted, quiescence, transfers) and harness events share one `i` sequence, which keeps resumption simple. The specification does not say this. The normalizer must filter by `kind`, and `adopt` is itself journalled.
10. **Closing a dispatcher races with connection teardown.** Right after a worker dies, the runner may not yet have processed EOF on its sockets, so recording closure is refused while `open_conns > 0`. The adopting worker must retry with a deadline. A follower blocked on "wait for the next event" must watch for EOF, or a dead worker's stream connection stays open after the candidate ends.
11. **The tombstone is not a removal.** The registry entry is not deleted. The live fields (pid, socket) are replaced by a tombstone with the receipt, as the specification requires, so that a delayed `submit` is rejected after `release`.

## One runner per attempt or one runner daemon per host

The open question of issue #115: per attempt, given that adoption is required.

- **Adoption costs the same in both models.** The worker reads the registry, checks identity, authenticates and resumes from an offset.
- **A host daemon adds a second adoption problem.** When the daemon itself restarts or upgrades, it would have to re-adopt the stdio pipes of live candidates, and those pipes die with the process that holds them. That requires file-descriptor passing (`SCM_RIGHTS`) or a re-exec that keeps descriptors. Without either, one daemon crash turns into `runner lost` for every attempt on the host at once, because each harness dies on `EPIPE` within one event interval.
- **Per attempt, everything shares one lifetime.** Crash scope, authentication scope (the secret and socket), version skew during an upgrade (old attempts finish under their old runner) and lifecycle (retirement equals runner exit, one registry entry per process) all match the attempt. The protocol needs no multiplexing.
- **A shared event loop couples attempts.** In one daemon, a slow fsync, a hash of a large file or a stop with a grace period in one attempt stalls the journals of all attempts unless every step is threaded carefully.
- **The cost per attempt is small.** About 40 ms to spawn and about 16 MiB of resident memory for a Python runner. That is negligible against agent sessions that last minutes or hours, at the concurrency a development host runs.
- **What a host daemon would add.** One place for capacity accounting across attempts, and one subreaper or cgroup owner on Linux. Each per-attempt runner can be its own subreaper, and capacity already lives in the scheduler.

**Recommendation:** one detached runner per attempt, plus a registry directory per execution domain owned by the worker-side runtime. A host daemon should be reconsidered only if measurements at real concurrency show the per-attempt cost matters.

## macOS-specific mechanisms used

- `proc_pidinfo(PROC_PIDTBSDINFO)` through ctypes from libSystem: start time in microseconds, ppid, pgid, uid, zombie state. `p_uniqueid` from `PROC_PIDT_BSDINFOWITHUNIQID` is a possible stronger identity; it was not exercised.
- `proc_listallpids` and `proc_listpgrppids`.
- `sysctl(KERN_PROCARGS2)` for the environment sweep. The kernel withholds the environment of platform binaries.
- `LOCAL_PEERPID` (option 0x002) and `LOCAL_PEERCRED` at `SOL_LOCAL` on `AF_UNIX`. Socket file modes are enforced on connect.
- `F_FULLFSYNC` for durability that survives power loss.
- launchd (pid 1) adopts orphans. macOS has no `PR_SET_PDEATHSIG`, no subreaper, no pidfd and no cgroups.
- `sun_path` of 104 bytes.
- Private interfaces `responsibility_spawnattrs_setdisclaim` and `responsibility_get_pid_responsible_for_pid`, used only in the attribution experiment.

## Linux and Docker: recheck before claiming parity

- **Process identity.** Start time from field 22 of `/proc/<pid>/stat` (clock ticks since boot) combined with `boot_id`, or `pidfd_open` and `pidfd_send_signal` for signalling without races. Repeat the pid-reuse case.
- **Peer credentials.** `SO_PEERCRED` instead of `LOCAL_PEERPID` and `LOCAL_PEERCRED`. Check the pid it reports across PID namespaces.
- **Socket path.** `sun_path` is 108 bytes; avoid abstract sockets.
- **Survival of the detached runner under systemd.** This is the main risk. With a worker run as a systemd service and the default `KillMode=control-group`, `setsid` and a double fork do not leave the cgroup: stopping or restarting the worker kills the runner and the candidate. Logind `KillUserProcesses=yes` has the same effect for user sessions. Check `KillMode=process`, or a separate scope for each attempt (`systemd-run --user --scope`).
- **Survival in a container.** If the worker is PID 1 of a container, its death stops the container. Worker restart in a container means runner death unless the runner runs outside it.
- **Orphan reparenting.** Orphans go to the nearest subreaper (`systemd --user`, tini, a terminal multiplexer), not necessarily pid 1. Recheck the ppid assumptions and how group membership is enumerated through `/proc`.
- **Whole-domain termination.** `PR_SET_CHILD_SUBREAPER` on the runner, and a cgroup v2 per attempt with `cgroup.kill` where delegation exists without root. `/proc/<pid>/environ` access rules for the environment sweep (ptrace read mode). Whether `PR_SET_PDEATHSIG` should kill the candidate when the runner dies.
- **Durability.** `fsync` and `fdatasync` semantics on ext4 and xfs. There is no `F_FULLFSYNC`.
- **Docker runtime.** Adoption of host-side supervisors that own candidate and publisher containers, container identity instead of process groups, and attaching to container output after a supervisor restart. All of this is separate from `local` and unverified.

## Not covered

- Real harness launch, interrupt, hooks and subagents.
- `finalize` and publication.
- A worker crash inside preparation, or between runtime acceptance and handle persistence (designed, not exercised).
- Several attempts on one host, host reboot, a full disk and clock changes.
- Hostile same-user code.
- High event rates.
