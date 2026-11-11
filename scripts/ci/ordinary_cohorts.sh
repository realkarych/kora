set -eo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/scheduled_lanes.sh"

select_groups() {
  read -r -a general <<< "$KORA_PYTEST_ARGUMENTS"
  case "$KORA_SCAFFOLD_OS:$KORA_SCAFFOLD_LANE" in
    ubuntu-24.04:conformance) names=(first second third) ;;
    macos-15:conformance) names=(first second third surface) ;;
    ubuntu-24.04:remainder) names=(measurements other graph engine) ;;
    macos-15:remainder) names=(measurements integration other) ;;
    ubuntu-24.04:integration) names=(integration surface) ;;
    *) exit 1 ;;
  esac
  first=(
    tests/architecture/test_ir_measurement_ci.py
    tests/architecture/test_no_suppression.py
    tests/conformance/test_local_process_lifetime.py
    tests/ir/test_structured_schema_assertions.py
    tests/measurements/test_ir_measurement_evidence.py
    tests/ir/test_input_path_resources.py
    tests/ir/test_expression_syntax_resources.py
    tests/ir/test_edge_type_resources.py
    tests/ir/test_approval_predicate_resources.py
    tests/engine/test_graph_output_inventory.py
    tests/ir/test_structured_schema_collection_predicates.py
    tests/engine/test_graph_outputs.py
    tests/engine/test_graph_native_outputs.py
  )
  third=(
    tests/conformance/test_conformance_cases_detect_broken_providers.py
    tests/conformance/test_parent_lifetime.py
    tests/conformance/test_recovered_process_lifetime.py
    tests/engine/test_graph_projection_paths.py
    tests/conformance/test_storage_case_runner.py
  )
  second=(tests/conformance tests/ir/test_inline_schema_resources.py tests/ir/test_fan_out_type_resources.py tests/ir/test_export_type_resources.py tests/ir/test_diagnostic_collection.py tests/ir/test_subgraph_interface_resources.py --ignore=tests/conformance/test_artifact_storage_conformance.py --ignore=tests/conformance/test_installed_conformance.py)
  second+=(tests/ir/test_structured_schema_container_counts.py tests/plugins/artifact_type/test_agent_session.py)
  for path in "${first[@]}" "${third[@]}" "${KORA_SCHEDULED_LIFETIME_MODULES[@]}"; do
    second+=("--ignore=$path")
  done
  engine=(
    tests/engine/test_graph_execution_contract.py
    tests/engine/test_start_parameter_cut.py
    tests/browser/test_release_wait.py
    tests/engine/test_graph_native_sources.py
    tests/ir/test_tool_policy_resources.py
    tests/ir/test_type_schema_resources.py
    tests/db/test_pricing_migration.py
    tests/db/test_attempt_outcome_migration.py
    tests/db/test_db_upgrade_process.py
    tests/db/test_attempt_deadline_migration.py
  )
  graph=(
    tests/engine
    --ignore=tests/engine/test_graph_execution_contract.py
    --ignore=tests/engine/test_graph_native_sources.py
    --ignore=tests/engine/test_graph_native_outputs.py
    --ignore=tests/engine/test_graph_output_inventory.py
    --ignore=tests/engine/test_graph_outputs.py
    --ignore=tests/engine/test_graph_projection_paths.py
    --ignore=tests/engine/test_start_parameter_cut.py
  )
  measurements=(
    tests/measurements
    --ignore=tests/measurements/test_ir_measurement_evidence.py
    tests/db
    tests/plugins
    --ignore=tests/plugins/storage
    --ignore=tests/plugins/runtime/local
    --ignore=tests/plugins/artifact_type/test_agent_session.py
    tests/ir/test_output_schema_resources.py
    tests/ir/test_type_schema_resources.py
  )
  integration=(
    tests/api
    tests/cli
    tests/ir/test_structured_schema_string_lengths.py
  )
  surface=(
    tests/cli/test_conformance.py
    tests/api/test_graph_execution_preparation.py
  )
  other=(tests --ignore=tests/ir/test_approval_predicate_resources.py --ignore=tests/ir/test_fan_out_type_resources.py --ignore=tests/ir/test_export_type_resources.py --ignore=tests/ir/test_input_path_resources.py --ignore=tests/ir/test_expression_syntax_resources.py --ignore=tests/ir/test_edge_type_resources.py --ignore=tests/api --ignore=tests/conformance --ignore=tests/plugins --ignore=tests/cli --ignore=tests/db --ignore=tests/measurements --ignore=tests/ir/test_inline_schema_resources.py --ignore=tests/ir/test_type_schema_resources.py --ignore=tests/ir/test_structured_schema_assertions.py --ignore=tests/ir/test_structured_schema_collection_predicates.py --ignore=tests/ir/test_structured_schema_string_lengths.py --ignore=tests/ir/test_subgraph_interface_resources.py --ignore=tests/architecture/test_ir_measurement_ci.py --ignore=tests/architecture/test_no_suppression.py --ignore=tests/ir/test_output_schema_resources.py)
  other+=(--ignore=tests/engine/test_graph_output_inventory.py --ignore=tests/ir/test_diagnostic_collection.py)
  other+=(
    --ignore=tests/engine/test_graph_outputs.py
    --ignore=tests/engine/test_graph_native_outputs.py
    --ignore=tests/engine/test_graph_projection_paths.py
    --ignore=tests/ir/test_structured_schema_container_counts.py
  )
}

group_arguments() {
  local name="$1"
  case "$name" in
    first) arguments=("${first[@]}") ;;
    second) arguments=("${second[@]}") ;;
    measurements) arguments=("${measurements[@]}") ;;
    integration) arguments=("${integration[@]}") ;;
    other) arguments=("${other[@]}") ;;
    third) arguments=("${third[@]}") ;;
    engine) arguments=("${engine[@]}") ;;
    graph) arguments=("${graph[@]}") ;;
    surface) arguments=("${surface[@]}") ;;
    *) exit 1 ;;
  esac
  if test "$name" = integration; then
    case "$KORA_SCAFFOLD_OS:$KORA_SCAFFOLD_LANE" in
      ubuntu-24.04:integration|macos-15:remainder)
        for path in "${surface[@]}"; do
          arguments+=("--ignore=$path")
        done
        ;;
    esac
  fi
  if test "$KORA_SCAFFOLD_OS" = ubuntu-24.04 && test "$KORA_SCAFFOLD_LANE" = remainder && test "$name" != engine; then
    for path in "${engine[@]}"; do
      for index in "${!arguments[@]}"; do
        if test "${arguments[$index]}" = "$path"; then
          unset "arguments[$index]"
        fi
      done
      arguments+=("--ignore=$path")
    done
  fi
  if test "$KORA_SCAFFOLD_OS" = ubuntu-24.04 && test "$KORA_SCAFFOLD_LANE" = remainder && test "$name" = other; then
    arguments+=("--ignore=tests/engine")
  fi
}

launch_groups() {
  pids=()
  evidence=()
  for name in "${names[@]}"; do
    group_arguments "$name"
    uv run --no-sync python -m tests.support.ordinary_pytest -v --exitfirst -o cache_dir="$KORA_ORDINARY_OUTPUT/cache-$name" --basetemp="$KORA_ORDINARY_OUTPUT/tmp-$name" --junitxml="$KORA_ORDINARY_OUTPUT/$name.xml" "${arguments[@]}" > "$KORA_ORDINARY_OUTPUT/$name.log" 2>&1 &
    pids+=("$!")
    evidence+=("$name.log" "$name.xml" "$name.exit")
  done
}

join_groups() {
  result=0
  for index in "${!pids[@]}"; do
    if wait "${pids[$index]}"; then
      status=0
    else
      status="$?"
      result=1
    fi
    printf '%s\n' "$status" > "$KORA_ORDINARY_OUTPUT/${names[$index]}.exit"
  done
}

require_group_evidence() {
  for name in "${evidence[@]}"; do
    test -f "$KORA_ORDINARY_OUTPUT/$name"
    test ! -L "$KORA_ORDINARY_OUTPUT/$name"
    test -s "$KORA_ORDINARY_OUTPUT/$name"
  done
}

print_group_logs() {
  for name in "${evidence[@]}"; do
    case "$name" in
      inventory.json|*.log) cat "$KORA_ORDINARY_OUTPUT/$name" ;;
    esac
  done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  test "$#" -eq 0
  select_groups
  launch_groups
  join_groups
  require_group_evidence
  print_group_logs
  exit "$result"
fi
