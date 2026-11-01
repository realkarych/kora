#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  local cleanup_ownership="$1" cleanup_unit="$2" state description
  rmdir "$cleanup_ownership"
  description="$(systemctl --user show --property=Description --value -- "$cleanup_unit")" || return 1
  if test "$description" != "$cleanup_ownership"; then
    return
  fi
  state="$(systemctl --user show --property=LoadState --value -- "$cleanup_unit")"
  if test "$state" != not-found; then
    if ! systemctl --user kill --signal=KILL --kill-whom=all -- "$cleanup_unit"; then
      case "$(systemctl --user show --property=ActiveState --value -- "$cleanup_unit")" in
        inactive|failed) ;;
        *) return 1 ;;
      esac
    fi
    if ! systemctl --user stop -- "$cleanup_unit"; then
      test "$(systemctl --user show --property=LoadState --value -- "$cleanup_unit")" = not-found
    fi
  fi
}

run_service() (
  readonly ownership="$1"
  readonly unit="${ownership##*/}.service"
  shift
  readonly executable="$(command -v "$1")"
  shift
  trap 'cleanup "$ownership" "$unit"' EXIT
  environment=()
  for name in KORA_MEASUREMENT_OUTPUT KORA_MEASUREMENT_REVISION KORA_TEST_DATABASES UV_PYTHON; do
    if test -n "${!name:-}"; then
      environment+=("--setenv=$name=${!name}")
    fi
  done
  systemd-run --user --pipe --wait --quiet --collect \
    --unit="$unit" --working-directory="$PWD" \
    --property="Description=$ownership" \
    --property=KillMode=process --property=PrivateTmp=no \
    --property=OOMPolicy=continue --setenv="PATH=$PATH" \
    --setenv="XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" \
    --setenv="DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS" \
    "${environment[@]}" -- "$executable" "$@"
)

main() {
  if test "$(uname -s)" != Linux; then
    exec "$@"
  fi
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  local ownership
  ownership="$(mktemp -d /tmp/kora-ci.XXXXXXXX)"
  run_service "$ownership" "$@"
}

if test "${BASH_SOURCE[0]}" = "$0"; then
  main "$@"
fi
