export function parseRunnerMode(arguments_: string[]): 'checks' | 'smoke' {
  if (arguments_.length === 0) return 'checks'
  if (arguments_.length === 1 && arguments_[0] === '--smoke') return 'smoke'
  throw new Error('invalid-browser-mode')
}
