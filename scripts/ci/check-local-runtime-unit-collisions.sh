#!/usr/bin/env bash
set -euo pipefail

test "$(uname -s)" = Linux
readonly mode="$1"
manager=()
case "$mode" in
  root)
    test "$(id -u)" = 0
    test ! -e /opt/kora-local-runtime
    unit=kora-local-runtime-session.service
    ;;
  user)
    test "$(id -u)" != 0
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    manager=(--user)
    ;;
  *) exit 1 ;;
esac
readonly evidence="$(mktemp -d /tmp/kora-collision.XXXXXXXX)"
ownership=
readonly description="collision:$evidence"
invocation=
fixture_started=false

property() {
  systemctl "${manager[@]}" show --property="$1" --value -- "$unit"
}

cleanup_collision() {
  if test "$fixture_started" = true; then
    bash scripts/ci/prepare-local-runtime-fixtures.sh teardown
  fi
  if test -n "$invocation"; then
    test "$(property Description)" = "$description"
    test "$(property InvocationID)" = "$invocation"
    systemctl "${manager[@]}" stop -- "$unit"
    if kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
  fi
  if test -d "$ownership"; then rmdir "$ownership"; fi
  if test -f "$evidence/payload"; then rm "$evidence/payload"; fi
  rmdir "$evidence"
}

trap cleanup_collision EXIT
if test "$mode" = user; then
  unit="${evidence##*/}.service"
  ownership="$evidence/${unit%.service}"
  mkdir "$ownership"
fi
test "$(property LoadState)" = not-found
systemd-run "${manager[@]}" --collect --unit="$unit" \
  --property=Type=exec --property="Description=$description" \
  --property=RuntimeMaxSec=60s --property=TimeoutStopSec=5s \
  -- /usr/bin/sleep 60
invocation="$(property InvocationID)"
test -n "$invocation"
pid="$(property MainPID)"
test "$pid" -gt 0
test "$(property ActiveState)" = active
kill -0 "$pid"

if test "$mode" = root; then
  fixture_started=true
  if bash scripts/ci/prepare-local-runtime-fixtures.sh prepare; then
    exit 1
  fi
  test -f /opt/kora-local-runtime/prepared
  test "$(stat -c %u /opt/kora-local-runtime/prepared)" = 0
  test -d /opt/kora-local-runtime/service-session
  test "$(stat -c %u /opt/kora-local-runtime/service-session)" = "$SUDO_UID"
  test "$(stat -c %a /opt/kora-local-runtime/service-session)" = 700
  test ! -e /opt/kora-local-runtime/service-session.boot
  test ! -e /opt/kora-local-runtime/service-session.scope
  bash scripts/ci/prepare-local-runtime-fixtures.sh teardown
  fixture_started=false
  test ! -e /opt/kora-local-runtime
else
  if bash -ec 'source scripts/ci/run-local-runtime-tests.sh; run_service "$1" /usr/bin/touch "$2"' bash "$ownership" "$evidence/payload"; then
    exit 1
  fi
  test ! -e "$ownership"
  test ! -e "$evidence/payload"
fi
test "$(property Description)" = "$description"
test "$(property InvocationID)" = "$invocation"
test "$(property MainPID)" = "$pid"
test "$(property ActiveState)" = active
kill -0 "$pid"
printf '%s unit collision preserved invocation and process\n' "$mode"
