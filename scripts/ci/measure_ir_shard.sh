set -eo pipefail

select_ir_shard() {
  case "$1" in
    0)
      selection=(
        tests/measurements/test_ir_models_cost.py
        '--deselect=tests/measurements/test_ir_models_cost.py::test_ir_configuration_repeated_lifetime[1]'
        '--deselect=tests/measurements/test_ir_models_cost.py::test_ir_configuration_repeated_lifetime[2]'
        --deselect=tests/measurements/test_ir_models_cost.py::test_cost_checker_refuses_unexpected_acceptance
        --deselect=tests/measurements/test_ir_models_cost.py::test_cost_checker_refuses_unexpected_rejection
        --deselect=tests/measurements/test_ir_models_cost.py::test_cost_checker_releases_nested_values
        --deselect=tests/measurements/test_ir_models_cost.py::test_cost_process_failure_preserves_lossless_evidence
        --deselect=tests/measurements/test_ir_models_cost.py::test_accounting_value_cost
        --deselect=tests/measurements/test_ir_models_cost.py::test_accounting_value_lifetime
        '--deselect=tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[0]'
        '--deselect=tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[1]'
        '--deselect=tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[2]'
        --deselect=tests/measurements/test_ir_models_cost.py::test_vcs_ref_validation_lifetime
        --deselect=tests/measurements/test_ir_models_cost.py::test_agent_session_action_cost
        --deselect=tests/measurements/test_ir_models_cost.py::test_agent_session_validation_lifetime
        --deselect=tests/measurements/test_ir_models_cost.py::test_command_result_action_cost
        --deselect=tests/measurements/test_ir_models_cost.py::test_command_result_validation_lifetime
        --deselect=tests/measurements/test_ir_models_cost.py::test_structured_output_action_cost
        --deselect=tests/measurements/test_ir_models_cost.py::test_structured_output_validation_lifetime
        -k 'not test_ir_model_validation_cost or test_ir_model_validation_cost[dense_'
      )
      ;;
    1)
      selection=(
        'tests/measurements/test_ir_models_cost.py::test_ir_configuration_repeated_lifetime[1]'
        'tests/measurements/test_ir_models_cost.py::test_ir_configuration_repeated_lifetime[2]'
        tests/measurements/test_ir_models_cost.py::test_cost_checker_refuses_unexpected_acceptance
        tests/measurements/test_ir_models_cost.py::test_cost_checker_refuses_unexpected_rejection
        tests/measurements/test_ir_models_cost.py::test_cost_checker_releases_nested_values
        tests/measurements/test_ir_models_cost.py::test_cost_process_failure_preserves_lossless_evidence
        tests/measurements/test_ir_models_cost.py::test_accounting_value_cost
        tests/measurements/test_ir_models_cost.py::test_ir_model_validation_cost
        -k 'not test_ir_model_validation_cost or (test_ir_model_validation_cost[schema_)'
      )
      ;;
    2)
      selection=(
        tests/measurements/test_ir_models_cost.py::test_accounting_value_lifetime
        tests/measurements/test_ir_models_cost.py::test_ir_model_validation_cost
        -k 'not test_ir_model_validation_cost or (test_ir_model_validation_cost[control_)'
      )
      ;;
    3)
      selection=(
        'tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[0]'
        tests/measurements/test_ir_models_cost.py::test_ir_model_validation_cost
        tests/measurements/test_ir_models_cost.py::test_agent_session_action_cost
        tests/measurements/test_ir_models_cost.py::test_agent_session_validation_lifetime
        tests/measurements/test_ir_models_cost.py::test_command_result_action_cost
        tests/measurements/test_ir_models_cost.py::test_command_result_validation_lifetime
        tests/measurements/test_ir_models_cost.py::test_structured_output_action_cost
        tests/measurements/test_ir_models_cost.py::test_structured_output_validation_lifetime
        -k 'not test_ir_model_validation_cost or (test_ir_model_validation_cost[remaining_)'
      )
      ;;
    4)
      selection=(
        'tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[1]'
        tests/measurements/test_ir_models_cost.py::test_ir_model_validation_cost
        -k 'not test_ir_model_validation_cost or (test_ir_model_validation_cost[config_ or test_ir_model_validation_cost[document_)'
      )
      ;;
    5)
      selection=(
        'tests/measurements/test_ir_models_cost.py::test_ir_schema_repeated_lifetime[2]'
        tests/measurements/test_ir_models_cost.py::test_ir_model_validation_cost
        tests/measurements/test_ir_models_cost.py::test_vcs_ref_validation_lifetime
        -k 'not test_ir_model_validation_cost or (not (test_ir_model_validation_cost[dense_ or test_ir_model_validation_cost[schema_ or test_ir_model_validation_cost[control_ or test_ir_model_validation_cost[remaining_ or test_ir_model_validation_cost[config_ or test_ir_model_validation_cost[document_))'
      )
      ;;
    *) exit 1 ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  test "$#" -eq 1
  select_ir_shard "$1"
  uv run --no-sync pytest -q -p no:logging -m measurement "${selection[@]}" --junitxml="measurements/ir-models-cost-$1.xml"
fi
