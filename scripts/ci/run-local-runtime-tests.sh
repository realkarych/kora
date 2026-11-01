#!/usr/bin/env bash
set -euo pipefail

if test "$(uname -s)" != Linux; then
  exec "$@"
fi

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
readonly ownership="$(mktemp -d /tmp/kora-ci.XXXXXXXX)"
readonly unit="${ownership##*/}.service"
readonly executable="$(command -v "$1")"
shift

cleanup() {
  local state description
  rmdir "$ownership"
  description="$(systemctl --user show --property=Description --value -- "$unit")" || return 1
  if test "$description" != "$ownership"; then
    return
  fi
  state="$(systemctl --user show --property=LoadState --value -- "$unit")"
  if test "$state" != not-found; then
    if ! systemctl --user kill --signal=KILL --kill-whom=all -- "$unit"; then
      case "$(systemctl --user show --property=ActiveState --value -- "$unit")" in
        inactive|failed) ;;
        *) return 1 ;;
      esac
    fi
    if ! systemctl --user stop -- "$unit"; then
      test "$(systemctl --user show --property=LoadState --value -- "$unit")" = not-found
    fi
  fi
}

trap cleanup EXIT
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
