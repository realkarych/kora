import assert from 'node:assert/strict'

function replaceOnce(value: string, before: string, after: string): string {
  assert(value.split(before).length === 2, 'invalid-training-workflow')
  return value.replace(before, after)
}

export function validationWorkflow(training: string): string {
  const entries = Array.from({ length: 9 }, (_, index) => {
    const slot = index + 1
    const control = ['cycle-1', 'cycle-8', 'cycle-32', 'intertrial'][index - 5]
    const label = slot <= 5 ? `holdout-${String(slot)}` : control
    assert(label, 'invalid-validation-slot')
    const argument = slot <= 5 ? '""' : `--control=${label}`
    return `          - slot: ${String(slot)}\n            label: ${label}\n            argument: ${argument}`
  }).join('\n')
  let result = replaceOnce(training, 'name: Console browser calibration (${{ matrix.slot }})', 'name: Console browser validation (${{ matrix.label }})')
  result = replaceOnce(result, '        slot: [1, 2, 3, 4, 5]', `        include:\n${entries}`)
  result = replaceOnce(result, 'run: pnpm --dir web run test:browser --calibrate', 'run: pnpm --dir web run test:browser ${{ matrix.argument }}')
  return replaceOnce(result, 'name: console-browser-calibration-${{ matrix.slot }}', 'name: console-browser-validation-${{ matrix.label }}')
}

export function validateWorkflowTransition(training: string, validation: string): void {
  assert.equal(validation, validationWorkflow(training), 'Workflow changed beyond the approved stage transition')
}
