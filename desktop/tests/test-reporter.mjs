import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { testRun } from './run-context.mjs'
import { identity, timingKey } from './test-timings.mjs'

export default class TimingReporter {
  discovered = []
  executed = []
  starts = new Map()
  onInit(context) { this.root = context.config.root }
  onTestRunStart(specifications) { this.discovered = specifications.map(spec => identity(spec, this.root)) }
  onTestModuleStart(module) { this.starts.set(timingKey(identity(module.toTestSpecification(), this.root)), Date.now()) }
  onTestModuleEnd(module) {
    const item = identity(module.toTestSpecification(), this.root)
    const diagnostic = module.diagnostic()
    const phases = Object.fromEntries(['environmentSetupDuration', 'prepareDuration', 'collectDuration', 'setupDuration', 'duration'].map(name => [name, diagnostic[name]]))
    const cases = [...module.children.allTests()]
    const test_cases = { collected: cases.length, passed: cases.filter(test => test.result().state === 'passed').length }
    this.executed.push({ ...item, state: module.state(), test_cases, duration_ms: Object.values(phases).reduce((total, value) => total + value, 0), phases, started_at: this.starts.get(timingKey(item)), ended_at: Date.now() })
  }
  async onTestRunEnd(modules, errors, reason) {
    await writeFile(join(testRun().directory, 'scheduling-observed.json'), JSON.stringify({ discovered: this.discovered, executed: this.executed, reason, unhandled_errors: errors.length }))
  }
}
