# Selected Linux Codex dynamic-loader environment

[#1168](https://github.com/realkarych/kora/issues/1168) reserves `LD_PRELOAD` and `LD_AUDIT` in the selected Codex private launch constructor. The same typed environment conflict applies to either authored name collection. The accepted recipe leaves both controls unset.

## Matched Linux observations

The experiment used official Codex 0.166.0 and the selected Linux amd64 app-server and exec-server from fixture revision `c2986ca6bb37d35b37d3eaff451b618bc1f64f3e`. Every case used a fresh network-disabled container, private role homes, a synthetic model key and the local Responses fixture. The shared object was available in both members of each pair. Only the adverse case assigned its path through the named variable, and only to exec-server.

| Control | Without assignment | With executor assignment | Wrapper time |
| --- | --- | --- | --- |
| `LD_PRELOAD` | Normal marker, loader marker absent | Same normal marker, exact loader marker | 19 s |
| `LD_AUDIT` | Normal marker, loader marker absent | Same normal marker, exact loader marker | 16 s |

Each case checked the exact managed hook input and successful tool result. The constructor only ran its marker logic for `/bin/bash -c` while the normal command marker was absent. The ordinary fixture verified the full Bash command separately. Each run reaped both native roles. All four exact containers were removed, with empty probe inventories before and after each pair. The run used Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on a macOS arm64 host.

| Input or output | SHA-256 |
| --- | --- |
| Preload source | `62220f88aeaeac1562e65b1d2d5b61e442e56215a22736b2a421a2ad41e8b0ab` |
| Preload object | `5f165efa74a3423c3bb5ff843c56cff3d3b85ec0a0add677cdc7696d53b8a93e` |
| Audit source | `2b2a3f6b6933ab576bee32d982f67c771fe693e23daf90efbe36a5fc489f5a6d` |
| Linked audit object | `5a56e99b6bcf150901a422dc62e11f7b035c6ab2e59802193671a0b1903fa731` |
| Each successful pair's log | `8d1b511eb842a356d0b272677cf28053c5df7098798dff0855dbeb85032e37e4` |
| Initial audit failure log | `8d1e31319bff68a9db972c46b96949e9b16ceb120f2dfdae54ab2347d1f0c3c3` |

The initial audit object lacked a linked libc dependency. Its adverse run failed the normal command assertion. The successful pair used the object with a `libc.so.6` dependency and the `la_version` entry point.

The selected launch control passed all five cases on constructor revision `c4ac8b1ff2bb086a82ee6837503451bb1785adab` using `--check-selected-launch`. It confirmed the ordinary allow, denial, missing key, command metadata and exit-seven outcomes, reaped both roles in each case and removed all five exact containers. Before and after inventories were empty. Wall time was 42 seconds and the log SHA-256 was `224e6d80db27b9d33a2625d739ab7dc2efe0771fe84a1a154957fef2505ff24d`.

The complete five-case launch control also passed after rebasing onto the merged executor fixture. The exact executable revision is `e3e6caea4991507fd9f083cb8d798c65c1cf99eb`, with fixture tree `9cee1bfcc14df92cc342100af5cfbc01fb1572dd` and provider tree `0a9f1d0daf1e42b707f3e71e4a7ab127e3382bed`. All five direct pairs were reaped and all five exact containers were removed. Both inventories were empty. The run took 43 seconds and the log SHA-256 was `224e6d80db27b9d33a2625d739ab7dc2efe0771fe84a1a154957fef2505ff24d`.

## Scratch reproduction

Use a disposable directory and the existing probe prerequisites. The compiler step uses Apple clang 21 or a clang with the Linux x86-64 target, and GNU ld in a disposable Linux amd64 container. The compiler image may fetch binutils. The [native probe](../../tests/plugins/harness/codex/Dockerfile) pins the same official Codex archive and Python base. The two loader assignments belong only to the scratch executor environment.

Save this source as `loader.c` in the disposable directory.

```c
extern int access(const char *, int);
extern int close(int);
extern int open(const char *, int, ...);
extern int strcmp(const char *, const char *);
extern long write(int, const void *, unsigned long);

__attribute__((constructor))
static void mark_loader(int argc, char **argv, char **env) {
    (void)env;
    if (argc < 3 || strcmp(argv[0], "/bin/bash") || strcmp(argv[1], "-c")) {
        return;
    }
    if (access("kora-probe.marker", 0) == 0) {
        return;
    }
    int marker = open("kora-probe.startup", 1 | 64 | 128 | 131072, 0600);
    if (marker < 0) {
        return;
    }
    const char content[] = "loader constructor marker\n";
    write(marker, content, sizeof(content) - 1);
    close(marker);
}
```

Compile the two objects with the audit entry point added only to the audit object.

```sh
set -eu
scratch=$(pwd -P)
mkdir audit
cp loader.c audit/loader.c
cat >> audit/loader.c <<'AUDIT'

unsigned int la_version(unsigned int version) {
    return version;
}
AUDIT
clang --target=x86_64-linux-gnu -fPIC -O2 -c loader.c -o loader.o
clang --target=x86_64-linux-gnu -fPIC -O2 -c audit/loader.c -o audit.o
timeout -k 5s 125s docker run --rm --init --platform linux/amd64 \
  --mount "type=bind,source=$scratch,target=/fixture" \
  ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 \
  timeout -k 5s 100s bash -euc '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends binutils >/dev/null
    ld -shared -o /fixture/preload.so /fixture/loader.o
    ld -shared -o /fixture/audit.so /fixture/audit.o /lib/x86_64-linux-gnu/libc.so.6
    readelf -d /fixture/audit.so
  '
```

The documented compiler block reproduced both source and object hashes in the recorded toolchain.

From the repository, archive the recorded revision's `tests/plugins/harness/codex` and `src/kora/plugins/harness/codex` into a fresh directory for each pair. In the scratch fixture's `tests/plugins/harness/codex`, copy the pair's object to `loader.so`. Apply these changes in each scratch copy.

1. In `Dockerfile`, add `COPY loader.so /opt/kora-probe/loader.so` immediately after `FROM one-shot AS shell-controls`. In `run_hook_probe.sh`, add `loader.so` after `Dockerfile` in the probe context list.
2. In `probe.py`, replace the `BASH_ENV` assignment in the `selected-bash-startup-override` case with `exec_env["LD_PRELOAD"] = "/opt/kora-probe/loader.so"` for the preload pair or `exec_env["LD_AUDIT"] = "/opt/kora-probe/loader.so"` for the audit pair. Replace the value of `BASH_EXTRA_EFFECT_TEXT` with `loader constructor marker`.
3. Run this command from the scratch checkout for each pair, comparing the probe container inventory before and after.

```sh
set -eu
docker ps -aq --filter 'name=^/kora-hook-probe-' > before
probe_status=0
timeout -k 20s 350s sh tests/plugins/harness/codex/run_hook_probe.sh \
  --check-selected-bash-startup || probe_status=$?
docker ps -aq --filter 'name=^/kora-hook-probe-' > after
diff -u before after
test ! -s before
test ! -s after
test "$probe_status" -eq 0
```

Each pair should report two verified startup cases, two child-pair reaps and two exact removals. The scratch cases use the existing 30-second turn, five-second child and endpoint cleanup, and 75-second container attach deadlines.

Complete reservations are owned by [#549](https://github.com/realkarych/kora/issues/549). Physical production environment custody and selected profile activation retain their [runtime and Policy gates](../specs/codex-execution-profile.md#required-tool-policy-activation).
