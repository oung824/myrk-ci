import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { BaseSequencer } from 'vitest/node'
import { testRun } from './run-context.mjs'
import { identity, timingKey } from './test-timings.mjs'

export default class DurationSequencer extends BaseSequencer {
  async sort(files) {
    const run = testRun()
    const input = JSON.parse(await readFile(join(run.directory, 'scheduling-input.json'), 'utf8'))
    const known = new Map(input.entries.map(item => [timingKey(item), item]))
    const durations = files.map(spec => known.get(timingKey(identity(spec, this.ctx.config.root)))?.duration_ms).filter(value => value !== undefined).sort((a, b) => a - b)
    const estimate = durations.length ? durations[Math.ceil(durations.length * 0.75) - 1] : 1000
    const planned = new Map(files.map(spec => {
      const item = identity(spec, this.ctx.config.root)
      const observed = known.get(timingKey(item))
      return [spec, { ...item, estimated_ms: observed?.duration_ms ?? estimate, origin: observed ? observed.origin ?? 'timing-index' : 'estimate' }]
    }))
    const sorted = input.mode === 'legacy' ? await super.sort(files) : [...files].sort((a, b) =>
      a.project.config.sequence.groupOrder - b.project.config.sequence.groupOrder
      || a.project.name.localeCompare(b.project.name)
      || Number(b.project.config.isolate) - Number(a.project.config.isolate)
      || planned.get(b).estimated_ms - planned.get(a).estimated_ms
      || planned.get(a).path.localeCompare(planned.get(b).path))
    await writeFile(join(run.directory, 'scheduling-plan.json'), JSON.stringify(sorted.map(spec => planned.get(spec))))
    return sorted
  }
}
