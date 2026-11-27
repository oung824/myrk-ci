import assert from 'node:assert/strict'
import { runAuthorizedSend } from './authorized-send.mjs'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { isAbsolute, join, relative, resolve } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runScenario({ authority, command, events, execute, state }) {
  if (authority.case_id.startsWith('quality.')) {
    const request = JSON.parse(await readFile(join(authority.runtime, 'scenario.json'), 'utf8'))
    const modules = { QG1: ['./quality-files.mjs','runQualityFiles'], QG2: ['./quality-files.mjs','runQualityFiles'],
      QG3: ['./quality-governance.mjs','runQualityGovernance'], QG4: ['./quality-memory.mjs','runQualityMemory'],
      QG5: ['./quality-recovery.mjs','runQualityRecovery'], QG6: ['./quality-compaction.mjs','runQualityCompaction'] }
    const [path, entry] = modules[request.quality_group]
    const module = await import(path)
    return module[entry]({ authority, request, command, events, execute, state })
  }
  if (authority.case_id === "formal.provider-settings") {
    const { runConnectionSettings } = await import("./connection-settings.mjs")
    return runConnectionSettings({ authority, command, events, execute, state })
  }
  if (authority.case_id === 'formal.independent-review' || authority.case_id.startsWith('formal.review-')) {
    const { runIndependentReviewScenario } = await import('./independent-review.mjs')
    return runIndependentReviewScenario({ authority, command, events, execute, state })
  }
  if ((authority.case_id === 'formal.user-approval' || authority.case_id.startsWith('formal.approval-'))) {
    const { runApprovalScenario } = await import('./approval-modes.mjs')
    return runApprovalScenario({ authority, command, events, execute, state })
  }
  const request = JSON.parse(await readFile(join(authority.runtime, 'scenario.json'), 'utf8'))
  if (request.deliverables_adapter) {
    const { runDeliverables } = await import('./deliverables.mjs')
    return runDeliverables({ authority, request, command, events, execute, state })
  }
  if (request.delegation_parent_check) {
    const { runDelegationScenario } = await import('./delegation-parent-check.mjs')
    return runDelegationScenario({ authority, request, command, events, execute, state })
  }
  if (request.parallel_adapter) {
    const { runParallelScenario } = await import('./queue-parallel-stop.mjs')
    return runParallelScenario({ authority, request, command, events, execute, state })
  }
  if (request.queue_adapter) {
    const { runQueueScenario } = await import('./queue-parallel-stop.mjs')
    return runQueueScenario({ authority, request, command, events, execute, state })
  }
  if (request.recovery_adapter) {
    const { runRecoveryScenario } = await import('./save-import-recovery.mjs')
    return runRecoveryScenario({ authority, request, command, events, execute, state })
  }
  if (request.memory_adapter) {
    const { runMemoryScenario } = await import('./memory-isolation.mjs')
    return runMemoryScenario({ authority, request, command, events, execute, state })
  }
  if (request.authorized_send) return runAuthorizedSend({ authority, request, command, events, execute, state })
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const root = authority.scenario_root
  const caseId = authority.case_id
  const approvalDecisions = []
  const seenApprovals = new Set()
  let localServer
  let receiver
  let adoptionSent = false
  let continuationSent = false
  const approvePending = async () => {
    for (const value of await events()) {
      if (value.event !== 'action' || value.action.status !== 'awaiting_approval'
        || value.action.approval_settings?.reviewer !== 'user') continue
      const key = `${value.path}:${value.task_id}:${value.action.id}:${value.action.approval_id}`
      if (seenApprovals.has(key)) continue
      const part = relative(resolve(request.records), resolve(value.path))
      assert.ok(part && !part.startsWith('..') && !isAbsolute(part) && part.endsWith('.json'), 'Actual pending record must belong to this scenario')
      const publicObservation = caseId === 'formal.guidance-untrusted'
      const original = publicObservation ? null : await readFile(value.path)
      let action = value.action
      if (original) {
        const record = JSON.parse(original.toString('utf8'))
        const candidates = record.turns.filter(turn => turn.task?.id === value.task_id)
          .flatMap(turn => turn.actions).filter(candidate => candidate.id === value.action.id)
        assert.equal(candidates.length, 1, 'One actual saved candidate is required before a decision')
        action = candidates[0]
      }
      assert.equal(action.task_id, value.task_id)
      assert.equal(action.approval_id, value.action.approval_id)
      assert.equal(action.status, 'awaiting_approval')
      assert.equal(action.approval_settings.reviewer, 'user')
      if (!publicObservation) {
        assert.match(action.prepared_action_sha256, /^[a-f0-9]{64}$/)
        assert.match(action.approval_context_sha256, /^[a-f0-9]{64}$/)
      }
      assert.ok(action.prepared_parameters && action.authorization_revision)
      const parameters = action.prepared_parameters
      const changes = action.memory_changes ?? []
      const allowed = (caseId === 'formal.user-approval' && action.kind === 'create'
          && action.target === join(root, 'candidate.txt') && parameters.path === action.target
          && parameters.content === 'approved-exact-bytes' && parameters.create_parents.length === 0
          && parameters.creation_parent === root)
        || (caseId === 'formal.send' && action.kind === 'send' && action.target === receiver.url
          && parameters.url === receiver.url && parameters.path === request.receiver.source
          && parameters.payload_sha256 === request.receiver.payload_sha256)
        || (caseId === 'formal.memory-lifecycle' && action.kind === 'memory' && action.memory_project === null
          && changes.length === 1 && changes[0].scope === 'personal' && changes[0].action === 'remove'
          && changes[0].old_text === request.memory.corrected && changes[0].text === '' && changes[0].replacement === '')
      const observation = { action, task_id: value.task_id, approval_id: action.approval_id,
        event: value, record: relative(join(authority.runtime, 'receiver'), value.path),
        observation_source: publicObservation ? 'public_main_action_event' : 'active_saved_record',
        record_sha256_at_observation: original ? createHash('sha256').update(original).digest('hex') : null, approved: allowed }
      await writeFile(join(authority.runtime, 'receiver', `approval-${action.id}-${action.approval_id}.json`),
        JSON.stringify(observation, null, 2), { flag: 'wx' })
      approvalDecisions.push(observation)
      seenApprovals.add(key)
      await command({ action: 'approve_action', path: value.path, task_id: value.task_id,
        action_id: action.id, approval_id: action.approval_id, approved: allowed })
    }
  }
  const until = async (check, label) => {
    const deadline = Date.now() + (authority.grant.transport === 'mock' ? 180000 : 1200000)
    while (!await check()) {
      await approvePending()
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(25)
    }
  }
  const select = async ({ scope = null, document = null } = {}) => {
    if (scope) {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
      assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
    }
    if (document) {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [document] })
      assert.equal(await execute('window.myrk.selectDocument()'), document)
    }
  }
  const newChat = async () => {
    const count = (await events()).filter(item => item.event === 'conversation').length
    await command({ action: 'new' })
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > count, 'new conversation')
  }
  const finish = async (value, { allowFailure = false } = {}) => {
    const count = (await events()).filter(item => item.event === 'finished').length
    await command(value)
    await until(async () => {
      const all = await events()
      if (request.adoption && !adoptionSent && all.filter(item => item.event === 'finished').length === count
        && all.some(item => item.event === 'compactions' && item.compactions.some(summary => summary.status === 'completed'))) {
        adoptionSent = true
        await command({ action: 'send', prompt: request.adoption })
      }
      const completedSummaries = all.filter(item => item.event === 'compactions').at(-1)?.compactions.filter(summary => summary.status === 'completed') ?? []
      if (request.continuation && !continuationSent && completedSummaries.length >= request.continuation_after_summaries
        && all.filter(item => item.event === 'finished').length === count) {
        continuationSent = true
        await writeFile(join(root, 'compaction-continuation-send.json'), JSON.stringify({ before_summary_ids: completedSummaries.map(summary => summary.id), prompt: request.continuation, scope: request.scope }, null, 2))
        await command({ action: 'send', prompt: request.continuation })
      }
      return (await events()).filter(item => item.event === 'finished').length > count
    }, 'finished formal task')
    const result = (await events()).findLast(item => item.event === 'finished')
    if (!allowFailure) assert.equal(result.turn.status, 'completed', result.turn.error)
    return result
  }
  try {
    if (request.receiver) {
      receiver = { host: '127.0.0.1', received: [], unexpected_requests: [], closed: false }
      localServer = createServer((incoming, response) => {
        const chunks = []
        incoming.on('data', chunk => chunks.push(chunk))
        incoming.once('end', () => {
          const body = Buffer.concat(chunks)
          if (incoming.method !== 'POST' || incoming.url !== '/receive' || body.length > 2048) {
            receiver.unexpected_requests.push({ method: incoming.method, path: incoming.url, bytes: body.length })
            response.writeHead(400)
          } else {
            receiver.received.push(body.toString('hex'))
            response.writeHead(204)
          }
          response.end()
        })
      })
      await new Promise((accept, reject) => { localServer.once('error', reject); localServer.listen(0, '127.0.0.1', accept) })
      const address = localServer.address()
      assert.equal(typeof address, 'object')
      receiver.url = `http://127.0.0.1:${address.port}/receive`
      await writeFile(request.receiver.path, JSON.stringify(receiver, null, 2))
    }
    if (caseId === 'formal.restart' && authority.phase === 'reopen') {
      const original = JSON.parse(await readFile(join(root, 'restart-original.json'), 'utf8'))
      await command({ action: 'open', path: original.started.path })
      await until(async () => (await events()).some(item => item.event === 'conversation' && item.path === original.started.path), 'open original task')
      const opened = (await events()).find(item => item.event === 'conversation' && item.path === original.started.path)
      assert.equal(opened.running, false)
      assert.equal((await events()).some(item => item.event === 'started'), false)
      const exchange = JSON.parse(await readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8'))
      assert.equal(exchange.exchanges.length, 0)
      await command({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id })
      await until(async () => (await events()).some(item => item.event === 'recovery'), 'verify original effects')
      const recovery = (await events()).find(item => item.event === 'recovery').recovery
      assert.equal(recovery.can_resume, true)
      const finished = await finish({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id })
      await writeFile(join(root, 'restart-reopened.json'), JSON.stringify({ opened, recovery, finished, open_http_attempts: exchange.exchanges.length }, null, 2))
      state.task_id = finished.turn.task.id
    } else {
      await select(request)
      if (request.project_trusted) await command({ action: 'set_project_trust', root: request.scope.root, trusted: true })
      if (request.prelude) await finish({ action: 'send', prompt: request.prelude, scope: request.scope })
      let prompt = request.prompt + (receiver ? `\nExact isolated receiver URL: ${receiver.url}` : '')
      const first = { action: 'send', prompt, ...(request.approval ? {} : { approval_mode: request.approval_mode }),
        ...(request.scope ? { scope: request.scope } : {}), ...(request.document ? { document: request.document } : {}) }
      if (caseId === 'formal.restart') {
        const finished = await finish(first, { allowFailure: true })
        assert.equal(finished.saved, true)
        assert.equal(finished.turn.service_failure, true)
        const all = await events()
        const started = all.find(item => item.event === 'started')
        assert.ok(started)
        const saved = JSON.parse(await readFile(started.path, 'utf8'))
        const reliable = saved.turns.at(-1)
        assert.equal(reliable.task.id, started.task_id)
        assert.equal(reliable.service_failure, true)
        await writeFile(join(root, 'restart-original.json'), JSON.stringify({ started, finished, reliable,
          requests_before: reliable.task.requests, failure_origin: 'acceptance-local pre-send transport fault after the actual native check' }, null, 2))
        state.task_id = started.task_id
      } else {
        const delivered = await finish(first, { allowFailure: caseId.startsWith('formal.compaction-') })
        state.task_id = delivered.turn.task.id
        if (caseId === 'formal.guidance-untrusted') {
          const recordBytes = await readFile(delivered.path)
          const record = JSON.parse(recordBytes.toString('utf8'))
          const fields = ['id', 'task_id', 'kind', 'target', 'impact', 'approval_id', 'prepared_request',
            'prepared_parameters', 'approval_settings', 'authorization_revision', 'tool_name',
            'tool_arguments', 'tool_source', 'tool_schema_version']
          const corroborated = approvalDecisions.map(observation => {
            const candidates = record.turns.filter(turn => turn.task?.id === observation.task_id)
              .flatMap(turn => turn.actions).filter(action => action.id === observation.action.id)
            assert.equal(candidates.length, 1)
            const saved = candidates[0]
            for (const field of fields) assert.deepEqual(saved[field], observation.action[field], `Saved ${field} differs from the actual public proposal`)
            assert.equal(saved.status, 'denied')
            assert.equal(saved.effect, 'not_started')
            assert.match(saved.prepared_action_sha256, /^[a-f0-9]{64}$/)
            assert.match(saved.approval_context_sha256, /^[a-f0-9]{64}$/)
            return { action_id: saved.id, approval_id: saved.approval_id,
              prepared_action_sha256_after_finished: saved.prepared_action_sha256,
              approval_context_sha256_after_finished: saved.approval_context_sha256 }
          })
          state.approval_corroboration = { source: 'saved_record_after_finished', status: 'passed',
            record_sha256_after_finished: createHash('sha256').update(recordBytes).digest('hex'), actions: corroborated }
          await writeFile(join(authority.runtime, 'receiver/approval-after-finished.json'),
            JSON.stringify(state.approval_corroboration, null, 2), { flag: 'wx' })
        }
        if (request.source_revision) {
          const revision = request.source_revision
          await writeFile(join(root, 'guidance-before-revision.json'), JSON.stringify({
            artifact_before: await readFile(revision.artifact, 'utf8'), source_before: await readFile(revision.path, 'utf8'),
            record_before: JSON.parse(await readFile(delivered.path, 'utf8')), changed_at: new Date().toISOString(),
          }, null, 2), { flag: 'wx' })
          await writeFile(revision.path, await readFile(revision.replacement))
          await finish({ action: 'send', prompt: revision.prompt, scope: request.scope, new_task: true })
        } else if (caseId === 'formal.memory-lifecycle') {
          await writeFile(join(root, 'memory-original-history.json'), await readFile(delivered.path))
          await newChat()
          await finish({ action: 'send', prompt: 'Recall the currently applicable synthetic personal preference from memory; do not invent another fact.' })
          for (const change of [
            { action: 'replace', old_text: request.memory.original, text: request.memory.replaced },
            { action: 'patch', old_text: request.memory.replaced, text: '2 cups', replacement: '3 cups' },
            { action: 'remove', old_text: request.memory.corrected },
          ]) await finish({ action: 'memory_change', changes: [{ scope: 'personal', ...change }] })
          const count = (await events()).filter(item => item.event === 'memory').length
          await command({ action: 'memory_list' })
          await until(async () => (await events()).filter(item => item.event === 'memory').length > count, 'memory list after removal')
          const current = (await events()).findLast(item => item.event === 'memory')
          assert.deepEqual(current.facts, [])
          const originalHistory = JSON.parse(await readFile(join(root, 'memory-original-history.json'), 'utf8'))
          const savedHistory = JSON.parse(await readFile(delivered.path, 'utf8'))
          assert.deepEqual(savedHistory.messages, originalHistory.messages)
          assert.deepEqual(savedHistory.turns, originalHistory.turns)
          await newChat()
          await finish({ action: 'send', prompt: 'List only current applicable saved facts. Do not restore facts from other chats or deleted notes.' })
        } else if (caseId === 'formal.memory-isolation') {
          await newChat()
          await select({ scope: request.project_b_scope })
          await finish({ action: 'send', prompt: 'Report only the current project B applicable saved facts. Do not recall another project or infer project A content.', scope: request.project_b_scope })
        } else if (caseId.startsWith('formal.compaction-') && delivered.turn.status === 'completed') {
          const saved = JSON.parse(await readFile(delivered.path, 'utf8'))
          const latest = saved.turns.flatMap(turn => turn.compactions).filter(item => item.status === 'completed').at(-1)
          assert.ok(latest, 'Actual automatic summary is required')
          const readbacks = []
          for (const summary of saved.turns.flatMap(turn => turn.compactions).filter(item => item.status === 'completed')) {
            const source = summary.covered[0]
            const previous = (await events()).filter(item => item.event === 'compaction_evidence').length
            await command({ action: 'get_compaction_evidence', path: delivered.path, summary_id: summary.id,
              message_index: source.message_index, part_index: source.part_index, start: 0 })
            await until(async () => (await events()).filter(item => item.event === 'compaction_evidence').length > previous, 'original summary source readback')
            const evidence = (await events()).findLast(item => item.event === 'compaction_evidence')
            assert.equal(evidence.error, null)
            assert.ok(evidence.text.length > 0)
            readbacks.push(evidence)
          }
          await writeFile(join(root, 'compaction-readback.json'), JSON.stringify(readbacks, null, 2))

        }
      }
    }
    state.approval_decisions = approvalDecisions
    state.adoption_sent = adoptionSent
    state.continuation_command_issued = continuationSent
    state.passed = true
  } finally {
    state.approval_decisions = approvalDecisions
    if (localServer) {
      localServer.closeAllConnections()
      await new Promise(accept => localServer.close(accept))
      receiver.closed = true
      await writeFile(request.receiver.path, JSON.stringify(receiver, null, 2))
    }
  }
}
