set -eo pipefail

require_each() {
  local requirement name
  test "$#" -gt 0
  for requirement in "$@"; do
    name="${requirement%%=*}"
    if ! [[ $requirement == *=* && $name =~ ^[[:upper:]][[:upper:][:digit:]_]*$ ]]; then
      exit 1
    fi
    test "${!name}" = "${requirement#*=}"
  done
}

require_scaffold_lane() {
  test "$DEPENDENCIES_OUTCOME" = success
  test "$TESTS_OUTCOME" = success
  test "$UPLOAD_OUTCOME" = success
  test "$FINAL_SYNC_OUTCOME" = success
  test "$WORKFLOW_CANCELLED" = false
  case "$KORA_SCAFFOLD_LANE" in
    local-runtime)
      test "$PREPARATION_OUTCOME" = success
      test "$TEARDOWN_OUTCOME" = success
      ;;
    *)
      test "$PREPARATION_OUTCOME" = skipped
      test "$TEARDOWN_OUTCOME" = skipped
      ;;
  esac
  case "$KORA_SCAFFOLD_LANE" in
    conformance)
      test "$FRONTEND_OUTCOME" = success
      test "$STATIC_OUTCOME" = skipped
      test "$DISTRIBUTIONS_OUTCOME" = skipped
      ;;
    remainder|integration)
      test "$FRONTEND_OUTCOME" = success
      test "$STATIC_OUTCOME" = skipped
      test "$DISTRIBUTIONS_OUTCOME" = skipped
      ;;
    storage)
      test "$FRONTEND_OUTCOME" = success
      test "$STATIC_OUTCOME" = skipped
      test "$DISTRIBUTIONS_OUTCOME" = skipped
      ;;
    local-runtime)
      test "$FRONTEND_OUTCOME" = success
      test "$STATIC_OUTCOME" = success
      test "$DISTRIBUTIONS_OUTCOME" = success
      ;;
    *) exit 1 ;;
  esac
}

require_scaffold_evidence() {
  test "$SCOPE_RESULT" = success
  test "$SCOPE_VERIFIED" = true
  test "$WORKFLOW_CANCELLED" = false
  if test "$FULL_SCOPE" = false; then
    test "$LANES_RESULT" = skipped
    test "$DEPENDENCIES_OUTCOME" = skipped
    test "$DOWNLOAD_OUTCOME" = skipped
    test "$VERIFICATION_OUTCOME" = skipped
    printf 'verified=true\n' >> "$GITHUB_OUTPUT"
    exit 0
  fi
  test "$FULL_SCOPE" = true
  test "$LANES_RESULT" = success
  test "$DEPENDENCIES_OUTCOME" = success
  test "$DOWNLOAD_OUTCOME" = success
  test "$VERIFICATION_OUTCOME" = success
  test "$WORKFLOW_CANCELLED" = false
  printf 'verified=true\n' >> "$GITHUB_OUTPUT"
}

require_cel_measurements() {
  test "$SCOPE_RESULT" = success
  test "$SCOPE_VERIFIED" = true
  test "$WORKFLOW_CANCELLED" = false
  if test "$FULL_SCOPE" = false; then
    test "$SCAFFOLD_RESULT" = success
    test "$SCAFFOLD_VERIFIED" = true
    test "$IR_MATRIX_RESULT" = skipped
    test "$RESOURCE_RESULT" = skipped
    test "$PROVIDER_PROTOCOL_RESULT" = skipped
    test "$PRICING_RESULT" = skipped
    test "$CEL_RESULT" = skipped
    test "$GIT_PUBLICATION_RESULT" = skipped
    exit 0
  fi
  test "$FULL_SCOPE" = true
  test "$SCAFFOLD_RESULT" = success
  test "$SCAFFOLD_VERIFIED" = true
  test "$IR_MATRIX_RESULT" = success
  test "$RESOURCE_RESULT" = success
  test "$PROVIDER_PROTOCOL_RESULT" = success
  test "$PRICING_RESULT" = success
  test "$CEL_RESULT" = success
  test "$GIT_PUBLICATION_RESULT" = success
  test "$DEPENDENCIES_OUTCOME" = success
  test "$SOURCE_REVISION_OUTCOME" = success
  test "$IR_DOWNLOAD_OUTCOME" = success
  test "$IR_INVENTORY_OUTCOME" = success
  test "$IR_UNION_UPLOAD_OUTCOME" = success
  test "$CEL_LIFETIME_DOWNLOAD_OUTCOME" = success
  test "$CEL_COST_DOWNLOAD_OUTCOME" = success
  test "$CEL_INVENTORY_OUTCOME" = success
  test "$RESOURCE_VERIFIED" = true
  test "$PROVIDER_PROTOCOL_VERIFIED" = true
  test "$PRICING_VERIFIED" = true
  test "$CEL_VERIFIED" = true
  test "$GIT_PUBLICATION_VERIFIED" = true
  test "$WORKFLOW_CANCELLED" = false
}

require_browser_checks() {
  test "$CANCELLED" = false
  test "$SCOPE_RESULT" = success
  test "$SCOPE_VERIFIED" = true
  if test "$FULL_SCOPE" = false; then
    test "$BROWSER_RESULT" = skipped
  else
    test "$FULL_SCOPE" = true
    test "$BROWSER_RESULT" = success
  fi
}

mode="${1:-}"
shift "$(($# > 0))"
case "$mode:$#" in
  check:*) require_each "$@" ;;
  verify:*)
    require_each "$@"
    printf 'verified=true\n' >> "$GITHUB_OUTPUT"
    ;;
  scaffold-lane:0) require_scaffold_lane ;;
  scaffold-evidence:0) require_scaffold_evidence ;;
  cel-measurements:0) require_cel_measurements ;;
  browser:0) require_browser_checks ;;
  *) exit 1 ;;
esac
