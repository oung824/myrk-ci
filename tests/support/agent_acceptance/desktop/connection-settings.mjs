import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runConnectionSettings({ authority, command, events, execute, state, request }) {
  request ??= JSON.parse(await readFile(join(authority.runtime, 'scenario.json'), 'utf8'))
  const until = async (check, label) => {
    const deadline = Date.now() + 90000
    while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(25) }
  }
  state.service_ready = (await events()).some(item => item.event === 'snapshot' && item.configured === true)
  if (authority.protocol === 'deepseek') {
    const before = (await events()).filter(item => item.event === 'configured').length
    await command({ action: 'configure', thinking: false })
    await until(async () => (await events()).filter(item => item.event === 'configured').length > before, 'public OFF configured')
    const configured = (await events()).filter(item => item.event === 'configured').at(-1)
    assert.equal(configured.thinking, false)
    await command({ action: 'snapshot' })
    await until(async () => (await events()).filter(item => item.event === 'snapshot').at(-1)?.thinking === false, 'OFF before new chat')
    state.off_before_new = true
    state.requested_reasoning = 'none'
  } else state.requested_reasoning = 'low'
  const conversations = (await events()).filter(item => item.event === 'conversation').length
  await command({ action: 'new' })
  await until(async () => (await events()).filter(item => item.event === 'conversation').length > conversations, 'new chat')
  const controlCount = (await events()).filter(item => item.event === 'model_controls').length
  await command({ action: 'get_model_controls' })
  await until(async () => (await events()).filter(item => item.event === 'model_controls').length > controlCount, 'actual new selection')
  state.new_chat_selection = (await events()).filter(item => item.event === 'model_controls').at(-1)
  if (authority.protocol !== 'deepseek') {
    const count = (await events()).filter(item => item.event === 'model_controls').length
    await command({ action: 'select_model', model: authority.provider_config.model, reasoning: 'low' })
    await until(async () => (await events()).filter(item => item.event === 'model_controls').length > count, 'native low selection')
    state.selected_controls = (await events()).filter(item => item.event === 'model_controls').at(-1)
    assert.equal(state.selected_controls.error, null)
  }
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, "desktop/package.json"))
  const { dialog } = require("electron")
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
  assert.equal(await execute("window.myrk.selectWorkspace()"), request.scope.root)
  const count = (await events()).filter(item => item.event === 'finished').length
  await command({ action: 'send', prompt: request.prompt, scope: request.scope })
  await until(async () => (await events()).filter(item => item.event === 'finished').length > count, 'native tool roundtrip')
  const finished = (await events()).filter(item => item.event === 'finished').at(-1)
  state.task_id = finished.turn.task.id
  state.finished_status = finished.turn.status
  state.passed = true
}
