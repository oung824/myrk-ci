import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQueueScenario({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const seenApprovals = new Set()
  state.approval_decisions = []
  const until = async (check, label) => {
    const deadline = Date.now() + 180000
    while (!await check()) {
      for (const event of await events()) {
        const action = event.action
        if (event.event !== 'action' || action.status !== 'awaiting_approval' || seenApprovals.has(action.approval_id)) continue
        const queued = (state.saved_queue ?? []).find(item => item.task.id === event.task_id)
        const allowed = action.kind === 'history_reuse' && queued && action.target === queued.task.scope.root
          && action.authorization_revision === queued.task.approval.revision
          && action.impact.includes(request.scope.root) && action.impact.includes(queued.task.scope.root)
          && action.impact.includes(authority.provider_config.base_url)
        assert.ok(allowed, 'Only the exact queued task history reuse for this same selected recipient may be approved')
        seenApprovals.add(action.approval_id)
        state.approval_decisions.push({ event, task_id: event.task_id, approved: true, observed_at: new Date().toISOString(),
          basis: 'Explicit synthetic same-chat history permission; no file access, goal or recipient is enlarged.' })
        await command({ action: 'approve_action', path: event.path, task_id: event.task_id,
          action_id: action.id, approval_id: action.approval_id, approved: true })
      }
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(25)
    }
  }
  const select = async scope => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
  }
  await select(request.scope)
  await command({ action: 'send', prompt: request.prompt, scope: request.scope })
  await until(async () => (await events()).some(item => item.event === 'started'), 'original actual task starts')
  const original = (await events()).find(item => item.event === 'started')
  await until(() => existsSync(join(root, 'queue-native-arrived.json')), 'original native response arrives')
  await command({ action: 'send', path: original.path, prompt: request.supplement })
  await until(async () => (await events()).some(item => item.event === 'supplement' && item.state === 'received'), 'one public supplement received')
  state.supplement_received = (await events()).find(item => item.event === 'supplement' && item.state === 'received')
  for (const [index, item] of request.queued.entries()) {
    await select(item.scope)
    await command({ action: 'send', path: original.path, prompt: item.prompt, scope: item.scope, new_task: true })
    await until(async () => (await events()).some(value => value.event === 'queue' && value.queue.length === index + 1), 'independent task saved in queue')
  }
  const queued = (await events()).findLast(item => item.event === 'queue')
  assert.equal(queued.saved, true)
  const raw = await readFile(original.path)
  await writeFile(join(root, 'queue-before-release.original.json'), raw, { flag: 'wx' })
  state.saved_queue = JSON.parse(raw.toString('utf8')).queue
  for (const [index, saved] of state.saved_queue.entries()) {
    for (const field of ['id', 'goal', 'scope', 'approval']) assert.deepEqual(saved.task[field], queued.queue[index].task[field])
  }
  await writeFile(join(root, 'queue-native-release.txt'), 'release only after actual supplement and durable queue', { flag: 'wx' })
  await until(async () => (await events()).filter(item => item.event === 'finished').length === 3, 'all original and queued tasks finish')
  const results = (await events()).filter(item => item.event === 'finished')
  assert.ok(results.every(item => item.saved === true))
  assert.ok(results.every(item => item.turn.status === 'completed'))
  const conversations = (await events()).filter(item => item.event === 'conversation').length
  await command({ action: 'open', path: original.path })
  await until(async () => (await events()).filter(item => item.event === 'conversation').length > conversations, 'finished actual saved conversation opens')
  state.opened = (await events()).findLast(item => item.event === 'conversation')
  state.main_pid = process.pid
  state.original = original
  state.task_id = original.task_id
  state.passed = true
}


export async function runParallelScenario({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const receivers = Object.fromEntries(['a', 'b'].map(key => [key, { owner_pid: process.pid, received: [], requests: [], closed: false }]))
  const servers = {}
  state.main_pid = process.pid
  state.approval_decisions = []
  const seen = new Set()
  const exchange = async () => JSON.parse(await readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8'))
  const saveReceivers = () => writeFile(request.parallel.receivers, JSON.stringify(receivers, null, 2))
  const until = async (check, label) => {
    const deadline = Date.now() + 120000
    while (!await check()) {
      const all = await events()
      for (const event of all) {
        const action = event.action
        if (event.event !== 'action' || action.status !== 'awaiting_approval' || seen.has(action.approval_id)) continue
        const bytes = await readFile(request.parallel.b.payload)
        assert.equal(action.kind, 'send')
        assert.equal(event.task_id, state.b.task_id)
        assert.equal(action.target, receivers.b.url)
        assert.equal(action.prepared_parameters.path, request.parallel.b.payload)
        assert.equal(action.prepared_parameters.payload_sha256, createHash('sha256').update(bytes).digest('hex'))
        assert.ok(action.impact.includes(bytes.toString('utf8')) && action.impact.includes(receivers.b.url))
        seen.add(action.approval_id)
        state.approval_decisions.push({ event, source_hex: bytes.toString('hex'), observed_at: new Date().toISOString(), approved: true })
        await command({ action: 'approve_action', path: event.path, task_id: event.task_id, action_id: action.id, approval_id: action.approval_id, approved: true })
      }
      const failed = all.find(item => item.event === 'finished' && item.turn.status === 'failed')
      assert.ok(!failed, `Actual independent task failed before ${label}: ${failed?.turn.error}`)
      const fatal = all.findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(25)
    }
  }
  const select = async scope => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
  }
  const start = async key => {
    await select(request.parallel[key].scope)
    await command({ action: 'send', prompt: request.parallel[key].prompt + `\nOnly this ${key.toUpperCase()} exact isolated receiver: ${receivers[key].url}`, scope: request.parallel[key].scope })
    await until(async () => (await events()).filter(item => item.event === 'started').length === (key === 'a' ? 1 : 2), 'actual independent conversation starts')
    state[key] = (await events()).findLast(item => item.event === 'started')
  }
  const stop = async key => {
    const before = await events()
    const native = await exchange()
    const observation = { observed_at: new Date().toISOString(), sdk_input_ids: native.sdk_inputs.map(item => item.sdk_input_id),
      action_ids: [...new Set(before.filter(item => item.event === 'action' && item.task_id === state[key].task_id).map(item => item.action.id))] }
    await command({ action: 'stop', path: state[key].path })
    await until(async () => (await events()).some(item => item.event === 'stopping' && item.task_id === state[key].task_id), 'public per-task stop is accepted')
    observation.accepted = (await events()).findLast(item => item.event === 'stopping' && item.task_id === state[key].task_id)
    state[`${key}_stop`] = observation
    await until(async () => (await events()).some(item => item.event === 'finished' && item.task_id === state[key].task_id), 'stopped task saves its final state')
    const finished = (await events()).findLast(item => item.event === 'finished' && item.task_id === state[key].task_id)
    assert.equal(finished.turn.status, 'cancelled')
    assert.equal(finished.saved, true)
  }
  try {
    for (const key of ['a', 'b']) {
      servers[key] = createServer((incoming, response) => {
        const chunks = []
        const item = { method: incoming.method, path: incoming.url, arrived_at: new Date().toISOString() }
        receivers[key].requests.push(item)
        incoming.on('data', chunk => chunks.push(chunk))
        incoming.once('end', () => { receivers[key].received.push(Buffer.concat(chunks).toString('hex')); response.writeHead(204); response.end() })
      })
      await new Promise((accept, reject) => { servers[key].once('error', reject); servers[key].listen(0, '127.0.0.1', accept) })
      receivers[key].url = `http://127.0.0.1:${servers[key].address().port}/receive`
    }
    await saveReceivers()
    await start('a')
    await until(() => existsSync(join(root, 'parallel-fragment-2.json')), 'actual A text fragment reaches SDK')
    const conversations = (await events()).filter(item => item.event === 'conversation').length
    await command({ action: 'new' })
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > conversations, 'a second conversation in the same Main')
    await start('b')
    await until(() => existsSync(join(root, 'parallel-native-3-arrived.json')), 'B native request is simultaneously in flight')
    await stop('a')
    await writeFile(join(root, 'parallel-native-3-release.txt'), 'B continues after accepted A stop', { flag: 'wx' })
    await until(() => existsSync(join(root, 'parallel-fragment-5.json')), 'B completes its independent exact send after A stop')
    await stop('b')
    const beforeOpen = (await exchange()).sdk_inputs.length
    for (const key of ['a', 'b']) {
      const count = (await events()).filter(item => item.event === 'conversation').length
      await command({ action: 'open', path: state[key].path })
      await until(async () => (await events()).filter(item => item.event === 'conversation').length > count, 'saved stopped conversation opens without sending')
      const opened = (await events()).findLast(item => item.event === 'conversation')
      assert.equal(opened.running, false)
      state[`${key}_opened`] = opened
    }
    assert.equal((await exchange()).sdk_inputs.length, beforeOpen)
    state.task_id = state.a.task_id
    state.passed = true
  } finally {
    for (const key of ['a', 'b']) {
      if (!servers[key]?.listening) continue
      servers[key].closeAllConnections()
      await new Promise(accept => servers[key].close(accept))
      receivers[key].closed = true
    }
    await saveReceivers()
  }
}
