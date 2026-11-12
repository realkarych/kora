set -euo pipefail

link_arguments() {
  local file escaped character index
  local LC_ALL=C
  link_args=(--hidden --mode task --format markdown --output "$2" --config lychee.toml --)
  while IFS= read -r -d '' file; do
    if [[ -d "./$file" ]]; then
      printf 'A selected Markdown input resolves to a directory\n' >&2
      return 1
    fi
    escaped=./
    for ((index = 0; index < ${#file}; index++)); do
      character=${file:index:1}
      case $character in
        '*'|'?'|'['|']') escaped+="[$character]" ;;
        *) escaped+="$character" ;;
      esac
    done
    link_args+=("$escaped")
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
  report="$staging/lychee.md"
  link_arguments "$staging/paths" "$report"

  binary="$staging/lychee"
  archive=$(python3 "$(dirname -- "${BASH_SOURCE[0]}")/acquire.py" fetch \
    "$RUNNER_TEMP/kora-acquisitions" lychee-0.24.2-x86_64-unknown-linux-gnu)
  tar -xOzf "$archive" -- lychee-x86_64-unknown-linux-gnu/lychee > "$binary"
  printf '%s  %s\n' 87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d "$binary" | sha256sum -c -
  chmod u+x "$binary"
  actual_version=$("$binary" --version)
  test "$actual_version" = 'lychee 0.24.2'

  checker_log="$staging/checker.log"
  checker_exit=0
  "$binary" "${link_args[@]}" \
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
