import assert from 'node:assert/strict'
import { runAuthorizedSend } from './authorized-send.mjs'
import { scenarioApprovals } from './scenario-approvals.mjs'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { join } from 'node:path'
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
  const root = authority.scenario_root
  const caseId = authority.case_id
  const approvals = scenarioApprovals({ authority, request, command })
  state.approval_decisions = approvals.decisions
  let localServer
  let receiver
  let adoptionSent = false
  let continuationSent = false
  const until = async (check, label) => {
    const deadline = Date.now() + (authority.grant.transport === 'mock' ? 180000 : 1200000)
    while (!await check()) {
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(25)
    }
  }
  const select = async ({ scope = null, document = null } = {}) => {
    if (!scope && !document) return
    const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
    const { dialog } = require('electron')
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
    const eventOffset = (await events()).length
    const decisionOffset = approvals.decisions.length
    let result
    await command(value)
    await until(async () => {
      const all = await events()
      const recent = all.slice(eventOffset)
      const startIndex = recent.findIndex(item => item.event === 'started'
        && (value.action !== 'resume_task' || (item.path === value.path && item.task_id === value.task_id)))
      if (startIndex !== -1) {
        const started = recent[startIndex]
        const turnEvents = recent.slice(startIndex + 1).filter(item => item.path === started.path && item.task_id === started.task_id)
        result = turnEvents.find(item => item.event === 'finished' && item.turn.task.id === started.task_id)
        if (result) return true
        await approvals.decide(turnEvents, receiver)
      }
      if (request.adoption && !adoptionSent
        && all.some(item => item.event === 'compactions' && item.compactions.some(summary => summary.status === 'completed'))) {
        adoptionSent = true
        await command({ action: 'send', prompt: request.adoption })
      }
      const completedSummaries = all.filter(item => item.event === 'compactions').at(-1)?.compactions.filter(summary => summary.status === 'completed') ?? []
      if (request.continuation && !continuationSent && completedSummaries.length >= request.continuation_after_summaries) {
        continuationSent = true
        await writeFile(join(root, 'compaction-continuation-send.json'), JSON.stringify({ before_summary_ids: completedSummaries.map(summary => summary.id), prompt: request.continuation, scope: request.scope }, null, 2))
        await command({ action: 'send', prompt: request.continuation })
      }
      return false
    }, 'finished formal task')
    if (!allowFailure) assert.equal(result.turn.status, 'completed', result.turn.error)
    const observations = approvals.decisions.slice(decisionOffset)
    if (observations.length) state.approval_corroboration = await approvals.corroborate(result, observations)
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
    state.adoption_sent = adoptionSent
    state.continuation_command_issued = continuationSent
    state.passed = true
  } finally {
    if (localServer) {
      localServer.closeAllConnections()
      await new Promise(accept => localServer.close(accept))
      receiver.closed = true
      await writeFile(request.receiver.path, JSON.stringify(receiver, null, 2))
    }
  }
}
