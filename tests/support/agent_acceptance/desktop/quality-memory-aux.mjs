import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityMemoryAux({ request, command, events, execute, state }) {
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
  assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
  const before = (await events()).filter(item => item.event === 'finished').length
  await command({ action: 'send', prompt: request.prompt, scope: request.scope })
  const deadline = Date.now() + 90000
  while ((await events()).filter(item => item.event === 'finished').length === before) {
    const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
    assert.ok(!fatal, fatal?.message)
    assert.ok(Date.now() < deadline, 'Bounded memory review finishes through the public Main')
    await pause(25)
  }
  const finished = (await events()).findLast(item => item.event === 'finished')
  assert.equal(finished.turn.status, 'completed', finished.turn.error)
  state.quality_memory_aux = { adapter: request.quality_memory_aux.adapter, task_id: finished.turn.task.id, finished_path: finished.path }
  state.task_id = finished.turn.task.id
  state.passed = true
}
