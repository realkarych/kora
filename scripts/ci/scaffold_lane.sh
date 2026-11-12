set -eo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/scheduled_lanes.sh"

KORA_HEADROOM_WARNING_SECONDS=450

admit_lane() {
  case "$KORA_SCAFFOLD_OS:$KORA_SCAFFOLD_LANE" in
    ubuntu-24.04:general-[1-8]|ubuntu-24.04:storage|ubuntu-24.04:local-runtime) ;;
    macos-15:general-[1-8]|macos-15:storage|macos-15:local-runtime) ;;
    *) exit 1 ;;
  esac
}

install_dependencies() {
  uv sync --locked --no-build --group build --extra postgres --extra cel --extra claude --no-install-project
  KORA_BUILD_CONSTRAINT_DIR="$(mktemp -d)"
  uv export --locked --only-group build --no-emit-project \
    --format requirements.txt \
    --output-file "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
  uv pip install --python "$PWD/.venv/bin/python" \
    --target "$KORA_BUILD_CONSTRAINT_DIR/backend" \
    --require-hashes \
    --requirements "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt"
  uv pip install --python "$PWD/.venv/bin/python" --no-deps \
    --build-constraints "$KORA_BUILD_CONSTRAINT_DIR/constraints.txt" \
    --require-build-hashes \
    --editable .
  printf 'KORA_BUILD_CONSTRAINT_DIR=%s\n' "$KORA_BUILD_CONSTRAINT_DIR" >> "$GITHUB_ENV"
}

complete_arguments() {
  scheduled_ignores "$KORA_SCAFFOLD_OS"
  KORA_COMPLETE_ARGUMENTS=(-m "not measurement" "${KORA_SCHEDULED_IGNORES[@]}")
}

collect_complete() {
  local output="$1"
  complete_arguments
  if uv run --no-sync pytest --collect-only -q "${KORA_COMPLETE_ARGUMENTS[@]}" > "$output.txt" 2> "$output.stderr"; then
    printf '0\n' > "$output.exit"
  else
    KORA_EXIT="$?"
    printf '%s\n' "$KORA_EXIT" > "$output.exit"
    tail -c 65536 "$output.txt"
    tail -c 65536 "$output.stderr"
    exit "$KORA_EXIT"
  fi
}

plan_lanes() {
  KORA_PLAN_DIRECTORY="$(mktemp -d "$RUNNER_TEMP/kora-plan.XXXXXX")"
  (
    export KORA_SCAFFOLD_OS=ubuntu-24.04 KORA_TEST_DATABASES=sqlite,postgresql
    collect_complete "$KORA_PLAN_DIRECTORY/linux"
  ) &
  linux="$!"
  (
    export KORA_SCAFFOLD_OS=macos-15 KORA_TEST_DATABASES=sqlite
    collect_complete "$KORA_PLAN_DIRECTORY/macos"
  ) &
  macos="$!"
  wait "$linux"
  wait "$macos"
  uv run --no-sync python -m tests.scaffold_evidence matrix \
    "$KORA_PLAN_DIRECTORY/linux.txt" "$KORA_PLAN_DIRECTORY/macos.txt" >> "$GITHUB_OUTPUT"
}

lane_selection() {
  KORA_SCAFFOLD_SELECTION=()
  case "$KORA_SCAFFOLD_OS:$KORA_SCAFFOLD_LANE" in
    ubuntu-24.04:local-runtime|macos-15:local-runtime) KORA_SCAFFOLD_SELECTION=(-m "not measurement") ;;
    macos-15:storage)
      for path in "${KORA_SCHEDULED_MACOS_STORAGE_MODULES[@]}"; do
        KORA_SCAFFOLD_SELECTION+=("--ignore=$path")
      done
      ;;
  esac
}

open_ordinary_output() {
  KORA_ORDINARY_OUTPUT="$(mktemp -d "$RUNNER_TEMP/kora-ordinary.XXXXXX")"
  export KORA_ORDINARY_OUTPUT
  printf 'KORA_ORDINARY_OUTPUT=%s\n' "$KORA_ORDINARY_OUTPUT" >> "$GITHUB_ENV"
}

preserve_ordinary_evidence() {
  for path in "$KORA_ORDINARY_OUTPUT"/child-*.log "$KORA_ORDINARY_OUTPUT"/child-*.xml "$KORA_ORDINARY_OUTPUT"/child-*.exit; do
    if test -f "$path" && test ! -L "$path"; then
      cp "$path" "$KORA_SCAFFOLD_DIRECTORY/${path##*/}"
    fi
  done
}

run_general_lane() {
  collect_complete "$KORA_SCAFFOLD_DIRECTORY/complete"
  open_ordinary_output
  uv run --no-sync python -m tests.scaffold_evidence assign \
    "$KORA_SCAFFOLD_DIRECTORY/complete.txt" "$KORA_SCAFFOLD_OS" "$KORA_SCAFFOLD_PYTHON" \
    "$KORA_SCAFFOLD_LANE" "$KORA_SCAFFOLD_LANES" "$KORA_ORDINARY_OUTPUT"
  if bash scripts/ci/ordinary_cohorts.sh > "$KORA_SCAFFOLD_DIRECTORY/tests.stdout" 2> "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"; then
    KORA_EXIT=0
  else
    KORA_EXIT="$?"
  fi
  preserve_ordinary_evidence
}

run_special_lane() {
  KORA_TEST_LAUNCH=()
  if test "$KORA_SCAFFOLD_LANE" = local-runtime; then
    KORA_TEST_LAUNCH=(bash scripts/ci/run-local-runtime-tests.sh)
  fi
  if "${KORA_TEST_LAUNCH[@]}" uv run --no-sync pytest -q "${KORA_SCAFFOLD_SELECTION[@]}" "${KORA_SCAFFOLD_ARGUMENTS[@]}" --junitxml="$KORA_SCAFFOLD_DIRECTORY/junit.xml" > "$KORA_SCAFFOLD_DIRECTORY/tests.stdout" 2> "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"; then
    KORA_EXIT=0
  else
    KORA_EXIT="$?"
  fi
}

require_lane_start() {
  [[ ${KORA_LANE_STARTED:-} =~ ^[1-9][0-9]*$ ]]
}

report_headroom() {
  local elapsed=$(($(date +%s) - KORA_LANE_STARTED))
  if test "$elapsed" -gt "$KORA_HEADROOM_WARNING_SECONDS"; then
    printf '::warning title=Scaffold lane headroom::%s Python %s %s lane job ran %s s since it started, more than %s s of the 600 s job deadline\n' \
      "$KORA_SCAFFOLD_OS" "$KORA_SCAFFOLD_PYTHON" "$KORA_SCAFFOLD_LANE" "$elapsed" "$KORA_HEADROOM_WARNING_SECONDS"
  fi
}

run_lane() {
  admit_lane
  KORA_CHECKOUT_REVISION="$(git rev-parse HEAD)"
  test "$KORA_CHECKOUT_REVISION" = "$KORA_WORKFLOW_REVISION"
  mkdir -p "$KORA_SCAFFOLD_DIRECTORY"
  read -r -a KORA_SCAFFOLD_ARGUMENTS <<< "$KORA_PYTEST_ARGUMENTS"
  lane_selection
  case "$KORA_SCAFFOLD_LANE" in
    storage|local-runtime) run_special_lane ;;
    *) run_general_lane ;;
  esac
  printf '%s\n' "$KORA_EXIT" > "$KORA_SCAFFOLD_DIRECTORY/tests.exit"
  if test "$KORA_EXIT" != 0; then
    tail -c 65536 "$KORA_SCAFFOLD_DIRECTORY/tests.stdout"
    tail -c 65536 "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"
    exit "$KORA_EXIT"
  fi
  uv run --no-sync python -m tests.scaffold_evidence record \
    "$KORA_SCAFFOLD_DIRECTORY" "$KORA_SCAFFOLD_RUN" "$KORA_SCAFFOLD_ATTEMPT" \
    "$KORA_CHECKOUT_REVISION" "$KORA_REQUESTED_REVISION" \
    "$KORA_SCAFFOLD_OS" "$KORA_SCAFFOLD_PYTHON" "$KORA_TEST_DATABASES" "$KORA_SCAFFOLD_LANE" \
    "${KORA_SCAFFOLD_SELECTION[@]}" "${KORA_SCAFFOLD_ARGUMENTS[@]}"
  tail -c 2048 "$KORA_SCAFFOLD_DIRECTORY/tests.stdout"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  case "$*" in
    dependencies) install_dependencies ;;
    plan) plan_lanes ;;
    tests)
      require_lane_start
      trap report_headroom EXIT
      run_lane
      ;;
    preserve)
      test -n "${KORA_ORDINARY_OUTPUT:-}"
      preserve_ordinary_evidence
      ;;
    *) exit 1 ;;
  esac
fi
