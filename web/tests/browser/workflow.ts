import assert from 'node:assert/strict'

function replaceOnce(value: string, before: string, after: string): string {
  assert(value.split(before).length === 2, 'invalid-training-workflow')
  return value.replace(before, after)
}

export function validationWorkflow(training: string): string {
  let output = replaceOnce(training, 'name: Console browser calibration (${{ matrix.slot }})', 'name: Console browser validation (${{ matrix.slot }})')
  output = replaceOnce(output, 'pnpm --dir web run test:browser --calibrate', 'pnpm --dir web run test:browser')
  return replaceOnce(output, 'name: console-browser-calibration-${{ matrix.slot }}', 'name: console-browser-validation-${{ matrix.slot }}')
}

export function validateWorkflowTransition(training: string, validation: string): void {
  assert.equal(validation, validationWorkflow(training), 'Changed workflow inputs between stages')
}
