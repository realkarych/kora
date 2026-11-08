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
    bytes=58256361
    digest=28ea0a438de9a13bd7891fd758eee6c7819ee8af5442dfd8d45bd65e56d4228c
    checksum=(sha256sum)
    ;;
  Darwin:arm64)
    platform=darwin
    architecture=arm64
    bytes=53049576
    digest=6a9724d9b50a5f22b4816cded441b5a8d9ddae546191a5e5b7efa5a5e2ee26d5
    checksum=(shasum -a 256)
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

mkdir -p -- "$version_dir"
staging=$(mktemp -d "$version_dir/.kora-node.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
archive="node-v$version-$platform-$architecture.tar.gz"
printf 'Acquiring Scaffold Node %s from nodejs.org\n' "$version"
curl --disable --fail --location --proto '=https' --proto-redir '=https' \
  --silent --show-error --connect-timeout 15 --max-time 60 --max-filesize 67108864 \
  --output "$staging/$archive" "https://nodejs.org/dist/v$version/$archive"

archive_size=$(wc -c < "$staging/$archive")
if (( archive_size != bytes )); then
  printf 'Unexpected Scaffold Node archive size\n' >&2
  exit 1
fi
printf '%s  %s\n' "$digest" "$staging/$archive" | "${checksum[@]}" -c -
tar -xzf "$staging/$archive" -C "$staging"
extracted="$staging/${archive%.tar.gz}"
actual_version=$("$extracted/bin/node" --version)
test "$actual_version" = "v$version"
mv -- "$extracted" "$destination"
: > "$complete"
