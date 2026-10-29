#!/usr/bin/env bash
set -euo pipefail

readonly fixtures=/opt/kora-local-runtime
readonly sysctl_key=kernel.apparmor_restrict_unprivileged_userns

require_root() {
  test "$(id -u)" = 0
  test -n "${SUDO_UID:-}"
  test -n "${SUDO_GID:-}"
  test -n "${SUDO_USER:-}"
  test "$SUDO_UID" != 0
}

prepare_common() {
  test ! -e "$fixtures"
  test "$(stat -c %u /opt 2>/dev/null || stat -f %u /opt)" = 0
  install -d -o 0 -g 0 -m 0755 "$fixtures"
  touch "$fixtures/prepared"
  install -d -o nobody -m 0755 "$fixtures/foreign"
  install -d -o nobody -m 0755 "$fixtures/foreign-parent"
  install -d -o "$SUDO_UID" -g "$SUDO_GID" -m 0700 "$fixtures/foreign-parent/child"
  install -o nobody -m 4755 "$(command -v sleep)" "$fixtures/setuid-sleep"
  install -d -o 0 -g 0 -m 0755 "$fixtures/small-filesystem"
}

prepare_linux() {
  mount -t tmpfs -o "size=16m,mode=0700,uid=$SUDO_UID,gid=$SUDO_GID" kora-small "$fixtures/small-filesystem"
  if sysctl -n "$sysctl_key" > "$fixtures/userns.previous" 2>/dev/null; then
    sysctl -q -w "$sysctl_key=0"
  else
    rm -f "$fixtures/userns.previous"
  fi
  touch "$fixtures/user-namespaces"
  DEBIAN_FRONTEND=noninteractive apt-get update -q -o Acquire::Retries=3
  DEBIAN_FRONTEND=noninteractive apt-get install -o Acquire::Retries=3 -y -q --no-install-recommends nfs-kernel-server
  install -d -o "$SUDO_UID" -g "$SUDO_GID" -m 0700 "$fixtures/nfs-export"
  install -d -o "$SUDO_UID" -g "$SUDO_GID" -m 0700 "$fixtures/nfs-export/state"
  install -d -o 0 -g 0 -m 0755 /etc/exports.d
  printf '%s 127.0.0.1(rw,sync,no_subtree_check,root_squash)\n' "$fixtures/nfs-export" > /etc/exports.d/kora-local-runtime.exports
  systemctl start nfs-kernel-server
  exportfs -ra
  install -d -o 0 -g 0 -m 0755 "$fixtures/nfs-mount"
  mount -t nfs -o vers=3,nolock,soft,timeo=50,retrans=2 "127.0.0.1:$fixtures/nfs-export" "$fixtures/nfs-mount"
  test "$(sudo -u "$SUDO_USER" stat -c %u "$fixtures/nfs-mount/state")" = "$SUDO_UID"
}

prepare_darwin() {
  local device
  device="$(hdiutil attach -nomount ram://32768 | awk '{print $1}')"
  test -n "$device"
  printf '%s\n' "$device" > "$fixtures/small-filesystem.device"
  newfs_hfs -v kora-small "$device" > /dev/null
  mount -t hfs "$device" "$fixtures/small-filesystem"
  chown "$SUDO_UID:$SUDO_GID" "$fixtures/small-filesystem"
  chmod 0700 "$fixtures/small-filesystem"
}

teardown_linux() {
  if test -f "$fixtures/userns.previous"; then
    sysctl -q -w "$sysctl_key=$(cat "$fixtures/userns.previous")"
  fi
  if mountpoint -q "$fixtures/nfs-mount"; then umount -f "$fixtures/nfs-mount"; fi
  if test -f /etc/exports.d/kora-local-runtime.exports; then
    rm -f /etc/exports.d/kora-local-runtime.exports
    exportfs -ra
    systemctl stop nfs-kernel-server
  fi
  if mountpoint -q "$fixtures/small-filesystem"; then umount "$fixtures/small-filesystem"; fi
}

teardown_darwin() {
  if mount | grep -q " on $fixtures/small-filesystem "; then umount "$fixtures/small-filesystem"; fi
  if test -f "$fixtures/small-filesystem.device"; then
    hdiutil detach "$(cat "$fixtures/small-filesystem.device")" > /dev/null
  fi
}

main() {
  require_root
  local platform
  platform="$(uname -s)"
  case "$1:$platform" in
    prepare:Linux) prepare_common; prepare_linux ;;
    prepare:Darwin) prepare_common; prepare_darwin ;;
    teardown:Linux) if test -f "$fixtures/prepared"; then teardown_linux; rm -rf "$fixtures"; fi ;;
    teardown:Darwin) if test -f "$fixtures/prepared"; then teardown_darwin; rm -rf "$fixtures"; fi ;;
    *) exit 1 ;;
  esac
}

main "$@"
