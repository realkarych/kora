set -euo pipefail

link_arguments() {
  local file escaped character index
  local LC_ALL=C
  link_paths=()
  while IFS= read -r -d '' file; do
    escaped=./
    for ((index = 0; index < ${#file}; index++)); do
      character=${file:index:1}
      case $character in
        '*'|'?'|'['|']') escaped+="[$character]" ;;
        *) escaped+="$character" ;;
      esac
    done
    link_paths+=("$escaped")
  done < "$1"
}

link_report_has_positive_total() {
  awk '
    NR == 1 { if ($0 != "# Summary") exit 1; next }
    NR == 2 { if (NF) exit 1; next }
    NR == 3 { if ($0 !~ /^\| Status +\| Count +\|$/) exit 1; next }
    NR == 4 { if ($0 !~ /^\|-+\|-+\|$/) exit 1; table = 1; next }
    table && !NF { table = 0; next }
    table && $0 !~ /^\|.*\|$/ { exit 1 }
    table && /^\| 🔍 Total *\|/ {
      totals++
      if ($0 ~ /^\| 🔍 Total +\| +0*[1-9][0-9]* +\|$/) positives++
    }
    END { if (totals != 1 || positives != 1) exit 1 }
  ' "$1"
}

print_lychee_output() {
  local stop_token status=0
  stop_token=$(od -An -v -N32 -tx1 /dev/urandom | tr -d '[:space:]')
  [[ $stop_token =~ ^[0-9a-f]{64}$ ]]
  printf '::stop-commands::%s\n' "$stop_token"
  cat -- "$@" || status=$?
  printf '\n::%s::\n' "$stop_token"
  return "$status"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  staging=$(mktemp -d "${RUNNER_TEMP:?}/.kora-links.XXXXXXXX")
  trap 'rm -rf -- "$staging"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  git ls-files --cached --others --exclude-standard -z -- '*.md' > "$staging/paths"
  test -s "$staging/paths"
  link_arguments "$staging/paths"

  archive="$staging/lychee.tar.gz"
  binary="$staging/lychee"
  curl --disable --fail --location --proto '=https' --proto-redir '=https' \
    --silent --show-error --connect-timeout 15 --max-time 60 --max-filesize 16777216 \
    --header 'Accept: application/octet-stream' --output "$archive" \
    'https://api.github.com/repos/lycheeverse/lychee/releases/assets/409959156'
  archive_size=$(wc -c < "$archive")
  if (( archive_size != 7626970 )); then
    printf 'Unexpected Lychee archive size\n' >&2
    exit 1
  fi
  printf '%s  %s\n' 1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a "$archive" | sha256sum -c -
  tar -xOzf "$archive" -- lychee-x86_64-unknown-linux-gnu/lychee > "$binary"
  printf '%s  %s\n' 87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d "$binary" | sha256sum -c -
  chmod u+x "$binary"
  actual_version=$("$binary" --version)
  test "$actual_version" = 'lychee 0.24.2'

  report="$staging/lychee.md"
  checker_log="$staging/checker.log"
  checker_exit=0
  "$binary" --mode task --format markdown --output "$report" --config lychee.toml -- "${link_paths[@]}" \
    > "$checker_log" 2>&1 || checker_exit=$?
  if [[ ! -f $report ]]; then
    print_lychee_output "$checker_log"
    printf 'Lychee did not produce its Markdown report\n' >&2
    exit 1
  fi
  print_lychee_output "$checker_log" "$report"
  printf '\n[Full Github Actions output](https://github.com/%s/actions/runs/%s?check_suite_focus=true)\n' \
    "${GITHUB_REPOSITORY:?}" "${GITHUB_RUN_ID:?}" >> "$report"
  mv -f -- "$report" "$RUNNER_TEMP/lychee.md"
  if (( checker_exit != 0 )); then
    exit "$checker_exit"
  fi
  if ! link_report_has_positive_total "$RUNNER_TEMP/lychee.md"; then
    printf 'Lychee summary must contain one positive Total count\n' >&2
    exit 1
  fi
fi
