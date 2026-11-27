import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runAuthorizedSend({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const decisions = []
  const seen = new Set()
  const receiver = { host: '127.0.0.1', received: [], requests: [], closed: false,
    owner_pid: process.pid, owner_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString(),
    listening_at: null, closed_at: null }
  const server = createServer((incoming, response) => {
    const item = { method: incoming.method, path: incoming.url, started_at: new Date().toISOString(), chunks: [], completed_at: null }
    receiver.requests.push(item)
    if (request.variant === 'inflight-stop') {
      incoming.once('readable', () => {
        const prefix = incoming.read(7)
        if (prefix) { item.chunks.push({ hex: prefix.toString('hex'), observed_at: new Date().toISOString() }); item.unobserved_remainder = 'unknown; remaining bytes may already be buffered by the OS or request stream' }
      })
      return
    }
    incoming.on('data', chunk => item.chunks.push({ hex: chunk.toString('hex'), observed_at: new Date().toISOString() }))
    incoming.once('end', () => {
      item.completed_at = new Date().toISOString()
      receiver.received.push(Buffer.concat(item.chunks.map(chunk => Buffer.from(chunk.hex, 'hex'))).toString('hex'))
      response.writeHead(['receiver-failure', 'false-delivery', 'wrong-bytes'].includes(request.variant) ? 500 : 204); response.end()
    })
  })
  const secondReceiver = { host: '127.0.0.1', received: [], requests: [], closed: false, owner_pid: process.pid, owner_started_at: receiver.owner_started_at }
  const secondServer = createServer((incoming, response) => {
    const chunks = []; secondReceiver.requests.push({ started_at: new Date().toISOString(), method: incoming.method, path: incoming.url })
    incoming.on('data', chunk => chunks.push(chunk))
    incoming.once('end', () => { secondReceiver.received.push(Buffer.concat(chunks).toString('hex')); response.writeHead(500); response.end() })
  })
  const until = async (check, label) => {
    const deadline = Date.now() + 180000
    while (!await check()) {
      for (const event of await events()) {
        const action = event.action
        if (event.event !== 'action' || action.status !== 'awaiting_approval' || action.approval_settings?.reviewer !== 'user') continue
        const key = `${event.path}:${event.task_id}:${action.id}:${action.approval_id}`
        if (seen.has(key)) continue
        assert.equal(action.kind, 'send')
        const source = await readFile(action.prepared_parameters.path)
        const parameters = action.prepared_parameters
        const display = action.target === receiver.url && parameters.url === receiver.url
          && parameters.path === request.receiver.source && parameters.payload_sha256 === createHash('sha256').update(source).digest('hex')
          && action.impact.includes(source.toString('utf8')) && action.impact.includes(receiver.url)
        const approved = display && (['allow', 'absolute-path', 'receiver-failure', 'inflight-stop', 'stop-marker-only'].includes(request.variant) || request.variant === 'changed-context' && decisions.length === 0)
        const observation = { event, action, task_id: event.task_id, approval_id: action.approval_id,
          observed_at: new Date().toISOString(), source_hex: source.toString('hex'), source_sha256: createHash('sha256').update(source).digest('hex'),
          display_complete: display, approved, observation_source: 'public_main_action_event', record_sha256_at_observation: null }
        decisions.push(observation); seen.add(key)
        await writeFile(join(authority.runtime, 'receiver', `send-decision-${decisions.length}.json`), JSON.stringify(observation, null, 2), { flag: 'wx' })
        if (request.variant === 'changed-context' && decisions.length > 1) {
          const before = (await events()).filter(item => item.event === 'error').length
          await command({ action: 'approve_action', path: event.path, task_id: event.task_id, action_id: action.id, approval_id: decisions[0].approval_id, approved: true })
          const deadline = Date.now() + 5000
          while ((await events()).filter(item => item.event === 'error').length === before) { assert.ok(Date.now() < deadline, 'stale approval refused'); await pause(10) }
          observation.stale_replay_error = (await events()).filter(item => item.event === 'error').at(-1)
        }
        if (request.variant === 'stop-marker-only') state.stop_observation = { observed_at: new Date().toISOString(), action_id: action.id, marker_only: true, provenance: 'Controlled offline caller flag; no public stop command was invoked.' }
        if (request.variant === 'stop') {
          state.stop_control_invoked = true
          state.stop_observation = { observed_at: new Date().toISOString(), action_id: action.id, approval_id: action.approval_id, effect: action.effect, prepared_request: action.prepared_request }
          await command({ action: 'stop', path: event.path })
        } else await command({ action: 'approve_action', path: event.path, task_id: event.task_id, action_id: action.id, approval_id: action.approval_id, approved })
        if (request.variant === 'changed-context' && decisions.length === 3) {
          const replacement = Buffer.from('Synthetic material revision v2; not approved.\n')
          await writeFile(request.receiver.source, replacement)
          state.version_change = { at: new Date().toISOString(), source_hex: replacement.toString('hex'), origin: 'synthetic independent material change after third refusal' }
        }
      }
      if (request.variant === 'inflight-stop' && !state.stop_observation && receiver.requests.some(item => item.chunks.length)) {
        const actionEvent = (await events()).findLast(item => item.event === 'action' && item.action.kind === 'send')
        state.stop_observation = { observed_at: new Date().toISOString(), action_id: actionEvent.action.id, approval_id: actionEvent.action.approval_id, effect: actionEvent.action.effect, prepared_request: actionEvent.action.prepared_request, receiver_prefix_hex: receiver.requests[0].chunks[0].hex }
        state.stop_control_invoked = true
        await command({ action: 'stop', path: actionEvent.path })
      }
      assert.ok(Date.now() < deadline, label)
      const fatal = (await events()).findLast(event => event.event === 'error' && event.fatal)
      assert.ok(!fatal, fatal?.message)
      await pause(25)
    }
  }
  try {
    await new Promise((accept, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', accept) })
    receiver.url = `http://127.0.0.1:${server.address().port}/receive`
    receiver.listening_at = new Date().toISOString()
    if (['changed-context', 'wrong-recipient'].includes(request.variant)) {
      await new Promise((accept, reject) => { secondServer.once('error', reject); secondServer.listen(0, '127.0.0.1', accept) })
      receiver.second_url = `http://127.0.0.1:${secondServer.address().port}/receive`
      secondReceiver.url = receiver.second_url
    }
    await writeFile(request.receiver.path, JSON.stringify(receiver, null, 2))
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    const count = (await events()).filter(event => event.event === 'finished').length
    await command({ action: 'send', prompt: request.prompt + `\nExact isolated receiver URL: ${receiver.url}`, scope: request.scope, ...(request.approval ? {} : { approval_mode: request.approval_mode }) })
    await until(async () => (await events()).filter(event => event.event === 'finished').length > count, 'authorized send finished')
    const delivered = (await events()).findLast(event => event.event === 'finished')
    const bytes = await readFile(delivered.path)
    const record = JSON.parse(bytes.toString('utf8'))
    const fields = ['id', 'task_id', 'approval_id', 'kind', 'target', 'impact', 'prepared_parameters', 'approval_settings', 'authorization_revision', 'tool_name', 'tool_arguments', 'tool_source', 'tool_schema_version']
    for (const observation of decisions) {
      const candidates = record.turns.filter(turn => turn.task?.id === observation.task_id).flatMap(turn => turn.actions).filter(action => action.id === observation.action.id)
      assert.equal(candidates.length, 1)
      for (const field of fields) assert.deepEqual(candidates[0][field], observation.action[field])
      observation.after_finished = { status: candidates[0].status, effect: candidates[0].effect,
        prepared_action_sha256: candidates[0].prepared_action_sha256, approval_context_sha256: candidates[0].approval_context_sha256,
        record_sha256: createHash('sha256').update(bytes).digest('hex') }
    }
    state.send_corroborated = true
    state.task_id = delivered.turn.task.id
    state.passed = true
  } finally {
    state.send_decisions = decisions
    server.closeAllConnections()
    await new Promise(accept => server.close(accept))
    if (secondServer.listening) { secondServer.closeAllConnections(); await new Promise(accept => secondServer.close(accept)); secondReceiver.closed = true; secondReceiver.closed_at = new Date().toISOString(); await writeFile(join(root, 'second-receiver.json'), JSON.stringify(secondReceiver, null, 2)) }
    receiver.closed = true; receiver.closed_at = new Date().toISOString()
    await writeFile(request.receiver.path, JSON.stringify(receiver, null, 2))
  }
}
