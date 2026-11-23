set -euo pipefail

version=12.14.0
digest=5e9119e6f4f18dace3795169e9b3672231ab3662505862dc3026571303a8ef32
runner_temp=${RUNNER_TEMP:?}
store="$runner_temp/kora-pnpm-store"

refuse() {
  printf '%s\n' "$1" >&2
  exit 1
}

owned_directory() {
  [[ -d $1 && -O $1 && ! -L $1 ]]
}

valid_store() {
  [[ ${versioned_store%/*} == "$store" && ${versioned_store##*/} =~ ^v[0-9]+$ &&
     ${PNPM_CONFIG_STORE_DIR:?} == "$store" ]] && owned_directory "$store" &&
    { [[ ! -e $versioned_store && ! -L $versioned_store ]] || owned_directory "$versioned_store"; }
}

empty_store() (
  shopt -s nullglob dotglob
  for entry in "$store"/* "$versioned_store"/*; do
    [[ $entry == "$versioned_store" ]] || return 1
  done
)

prepare() {
  [[ ! -e $store && ! -L $store ]] || refuse 'Scaffold pnpm store already exists'
  archive=$(python3 "$(dirname -- "${BASH_SOURCE[0]}")/acquire.py" fetch \
    "$runner_temp/kora-acquisitions" "pnpm-$version-linux-x64")
  staging=$(mktemp -d "$runner_temp/kora-pnpm.XXXXXXXX")
  trap 'rm -rf -- "$staging"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$staging"
  executable="$staging/pnpm"
  [[ -f $executable && -x $executable && ! -L $executable ]] ||
    refuse 'Invalid Scaffold pnpm executable'
  printf '%s  %s\n' "$digest" "$executable" | sha256sum --check --status ||
    refuse 'Scaffold pnpm executable failed SHA-256 verification'
  actual_version=$("$executable" --version)
  [[ $actual_version == "$version" ]] || refuse 'Unexpected Scaffold pnpm version'

  mkdir -m 700 -- "$store"
  export PNPM_CONFIG_STORE_DIR="$store"
  versioned_store=$("$executable" store path --silent)
  web_store=$("$executable" --dir web store path --silent)
  valid_store && empty_store && [[ $web_store == "$versioned_store" ]] ||
    refuse 'Scaffold pnpm did not select the same empty owned store from the checkout and web'
  printf '%s\n' "PNPM_CONFIG_STORE_DIR=$store" "KORA_SCAFFOLD_PNPM=$executable" \
    "KORA_SCAFFOLD_PNPM_STORE=$versioned_store" >> "${GITHUB_ENV:?}"
  printf '%s\n' "$staging" >> "${GITHUB_PATH:?}"
  trap - EXIT INT TERM
  printf 'Verified Scaffold pnpm %s SHA-256 %s\n' "$version" "$digest"
  printf 'Scaffold pnpm store initially empty: %s\n' "$versioned_store"
}

check_cache() {
  [[ ${KORA_PNPM_CACHE_OUTCOME:-} == success ]] || refuse 'Scaffold pnpm cache setup failed'
  primary=${KORA_PNPM_CACHE_PRIMARY:-}
  matched=${KORA_PNPM_CACHE_MATCHED:-}
  case ${KORA_PNPM_CACHE_HIT:-} in
    true)
      [[ $primary =~ ^node-cache-Linux-x64-pnpm-[0-9a-f]{64}$ && $matched == "$primary" ]] ||
        refuse 'Scaffold pnpm cache hit does not match the primary key'
      printf '%s\n' 'Scaffold pnpm store: exact hit'
      ;;
    false)
      if ! [[ $primary =~ ^node-cache-Linux-x64-pnpm-[0-9a-f]{64}$ && -z $matched ]] || ! empty_store; then
        refuse 'Scaffold pnpm cache miss has a matched key or a nonempty store, or lacks a valid primary key'
      fi
      printf '%s\n' 'Scaffold pnpm store: cold miss'
      ;;
    '')
      if ! [[ -z $primary && -z $matched ]] || ! empty_store; then
        refuse 'Invalid unavailable Scaffold pnpm cache'
      fi
      printf '%s\n' 'Scaffold pnpm store: cache unavailable'
      ;;
    *) refuse 'Invalid Scaffold pnpm cache result' ;;
  esac
}

check_selection() {
  [[ $(command -v pnpm) == "${KORA_SCAFFOLD_PNPM:?}" ]] || refuse 'Scaffold pnpm executable selection changed'
  web_store=$(pnpm --dir web store path --silent)
  [[ $web_store == "$versioned_store" ]] || refuse 'Scaffold pnpm store selection changed'
  printf 'Scaffold pnpm tool: %s; store: %s\n' "$KORA_SCAFFOLD_PNPM" "$versioned_store"
}

[[ ${RUNNER_OS:-} == Linux && ${RUNNER_ARCH:-} == X64 ]] || refuse 'Unsupported Scaffold pnpm platform'
[[ $runner_temp == /* ]] && owned_directory "$runner_temp" &&
  [[ $(cd -- "$runner_temp" && pwd -P) == "$runner_temp" ]] || refuse 'Invalid Scaffold pnpm runner temp directory'

case ${1:-} in
  prepare) prepare ;;
  check-cache | check-selection)
    versioned_store=${KORA_SCAFFOLD_PNPM_STORE:?}
    valid_store || refuse 'Invalid Scaffold pnpm store'
    if [[ $1 == check-cache ]]; then
      check_cache
    else
      check_selection
    fi
    ;;
  *) refuse 'Expected Scaffold pnpm prepare, check-cache or check-selection' ;;
esac
