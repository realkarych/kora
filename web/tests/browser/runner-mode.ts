export function parseRunnerMode(arguments_: string[]): 'verification' | 'calibration' | 'smoke' {
  if (arguments_.length === 0) return 'verification'
  if (arguments_.length === 1 && arguments_[0] === '--calibrate') return 'calibration'
  if (arguments_.length === 1 && arguments_[0] === '--smoke') return 'smoke'
  throw new Error('invalid-browser-mode')
}
