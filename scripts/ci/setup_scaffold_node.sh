set -euo pipefail

version=24.22.0
if ! cmp -s web/.node-version <(printf '%s\n' "$version"); then
  printf 'Scaffold Node archive must match web/.node-version\n' >&2
  exit 1
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    platform=linux
    architecture=x64
    ;;
  Darwin:arm64)
    platform=darwin
    architecture=arm64
    ;;
  *)
    printf 'Unsupported Scaffold Node archive platform\n' >&2
    exit 1
    ;;
esac

cache_root=${RUNNER_TOOL_CACHE:?}
version_dir="$cache_root/node/$version"
destination="$version_dir/$architecture"
complete="$destination.complete"

absent() {
  [[ ! -e $1 && ! -L $1 ]]
}

if [[ $cache_root != /* || ! -d $cache_root || $(cd -- "$cache_root" && pwd -P) != "$cache_root" ]]; then
  printf 'Invalid Scaffold Node cache root\n' >&2
  exit 1
fi

for directory in "$cache_root/node" "$version_dir"; do
  if ! absent "$directory" && [[ ! -d $directory || -L $directory ]]; then
    printf 'Invalid Scaffold Node cache directory\n' >&2
    exit 1
  fi
done

if [[ -d $destination && ! -L $destination && -f $complete && ! -L $complete &&
      -d $destination/bin && ! -L $destination/bin &&
      -f $destination/bin/node && -x $destination/bin/node && ! -L $destination/bin/node ]]; then
  printf 'Using the existing Scaffold Node %s cache entry\n' "$version"
  exit 0
fi

if ! absent "$destination" || ! absent "$complete"; then
  printf 'Incomplete or invalid Scaffold Node cache entry\n' >&2
  exit 1
fi

archive=$(python3 "$(dirname -- "${BASH_SOURCE[0]}")/acquire.py" fetch \
  "${RUNNER_TEMP:?}/kora-acquisitions" "node-$version-$platform-$architecture")

mkdir -p -- "$version_dir"
staging=$(mktemp -d "$version_dir/.kora-node.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
tar -xzf "$archive" -C "$staging"
extracted="$staging/node-v$version-$platform-$architecture"
actual_version=$("$extracted/bin/node" --version)
test "$actual_version" = "v$version"
mv -- "$extracted" "$destination"
: > "$complete"
