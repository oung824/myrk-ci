import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { runQualityMemoryAux } from './quality-memory-aux.mjs'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityMemory({ authority, request, command, events, execute, state }) {
  if (request.quality_memory_aux) return runQualityMemoryAux({ authority, request, command, events, execute, state })
  const root = authority.scenario_root
  const value = request.quality_memory
  assert.ok(['qg4-1', 'qg4-2', 'qg4-3', 'qg4-5', 'qg4-6', 'qg4-7', 'qg4-8', 'qg4-9', 'qg4-tuning-1', 'qg4-tuning-2'].includes(value.adapter))
  const measured = state.quality_memory = { task_id: value.task_id, approval_decisions: [], windows: [] }
  const seen = new Set()
  let activeGoal = request.prompt
  let activeScope = request.scope
  const liveApproval = async event => {
    const count = (await events()).filter(item => item.event === 'snapshot').length
    await command({ action: 'snapshot' })
    const deadline = Date.now() + 10000
    while (Date.now() < deadline) {
      const snapshot = (await events()).filter(item => item.event === 'snapshot')[count]
      if (snapshot) {
        return snapshot.conversations.find(item => item.path === event.path)?.turns
          .flatMap(turn => turn.actions ?? []).find(item => item.id === event.action.id) ?? null
      }
      await pause(25)
    }
    return null
  }
  const until = async check => {
    const deadline = Date.now() + 90000
    while (!await check()) {
      for (const event of await events()) {
        if (event.event !== 'action' || event.action.status !== 'awaiting_approval' || seen.has(event.action.approval_id)) continue
        seen.add(event.action.approval_id)
        const publicRecall = value.adapter === 'qg4-6' && event.action.kind === 'memory_recall'
        let action = event.action
        if (!publicRecall) {
          const record = JSON.parse(await readFile(event.path, 'utf8'))
          action = record.turns.flatMap(turn => turn.actions).find(item => item.id === event.action.id)
          assert.ok(action, 'The actual saved pending action exists')
          assert.match(action.prepared_action_sha256, /^[a-f0-9]{64}$/)
          assert.match(action.approval_context_sha256, /^[a-f0-9]{64}$/)
        }
        assert.equal(action.approval_id, event.action.approval_id)
        assert.equal(action.status, 'awaiting_approval')
        const changes = action.memory_changes ?? []
        let approved = action.kind === 'memory' && changes.length === 1 && changes[0].scope === value.scope_kind
        if (approved && changes[0].action === 'remove') {
          const observed = await current()
          const matches = observed.facts.filter(fact => fact.scope === value.scope_kind && fact.project === (value.scope_kind === 'project' ? value.project_root : null) && changes[0].old_text && fact.text.includes(changes[0].old_text))
          approved = ['qg4-2', 'qg4-8'].includes(value.adapter) && activeGoal === value.removal_prompt && matches.length === 1 && matches[0].text === (value.adapter === 'qg4-2' ? value.corrected : value.original)
        } else if (approved) {
          approved = changes[0].action === 'add' || value.adapter === 'qg4-2' && activeGoal === value.correction_prompt
        }
        let source_content = null
        let approvalEvidence
        if (action.kind === 'memory_recall') {
          const observed = await current()
          source_content = observed.facts.filter(fact => fact.scope === 'project' && fact.project === value.project_root).map(fact => fact.text)
          if (publicRecall) {
            const snapshot_action = await liveApproval(event)
            const contentSha = createHash('sha256').update(value.original, 'utf8').digest('hex')
            const matchesPreparedAction = candidate => candidate?.status === 'awaiting_approval'
              && candidate.kind === 'memory_recall' && candidate.target === value.project_root
              && candidate.task_id === event.task_id && candidate.approval_settings?.reviewer === 'user'
              && candidate.id === action.id && candidate.approval_id === action.approval_id
              && candidate.prepared_parameters?.content_sha256 === contentSha
              && candidate.prepared_parameters.source_project === value.project_root
              && candidate.prepared_parameters.recipient_project === request.project_b_scope.root
              && ['name', 'protocol', 'base_url', 'model'].every(key =>
                candidate.prepared_parameters.recipient?.[key] === authority.provider_config[key])
            approved = source_content.length === 1 && source_content[0] === value.original
              && [action, snapshot_action].every(matchesPreparedAction)
              && action.approval_preview === value.original && snapshot_action.approval_preview === value.original
            const before = await exchange()
            approvalEvidence = { snapshot_action,
              sdk_count_before_decision: before.sdk_inputs.length, http_count_before_decision: before.exchanges.length }
          }
          if (value.adapter === 'qg4-9') {
            await command({ action: 'send', path: event.path, prompt: value.withdrawal_prompt })
            const deadline = Date.now() + 10000
            while (!(await events()).some(item => item.event === 'supplement' && item.state === 'received' && item.supplement.text === value.withdrawal_prompt)) {
              assert.ok(Date.now() < deadline, 'Actual withdrawal arrives before the pending decision')
              await pause(25)
            }
          }
        }
        const decision = { action: 'approve_action', path: event.path, task_id: event.task_id,
          action_id: action.id, approval_id: action.approval_id, approved }
        measured.approval_decisions.push(approvalEvidence
          ? { action, approved, source_content, ...approvalEvidence, decision }
          : { action, approved, source_content })
        await command(decision)
      }
      const fatal = (await events()).findLast(event => event.event === 'error' && event.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, 'Public quality memory command completes')
      await pause(25)
    }
  }
  const finish = async payload => {
    if (payload.prompt) activeGoal = payload.prompt
    const count = (await events()).filter(item => item.event === 'finished').length
    await command(payload)
    await until(async () => (await events()).filter(item => item.event === 'finished').length > count)
    const event = (await events()).findLast(item => item.event === 'finished')
    if (event.turn.status === 'failed') {
      const record = JSON.parse(await readFile(event.path, 'utf8'))
      const task = record.turns.at(-1).task
      const missing = request.approval.access.denied_paths.filter(path => !task.approval.access.denied_paths.includes(path))
      if (missing.length && measured.current_new_event?.path === event.path) {
        measured.permission_change = { origin: "public_new_after_temporary_configuration", actual_new_event: measured.current_new_event, path: event.path, task_id: task.id, original_approval: request.approval,
          actual_approval: task.approval, missing_denied_paths: missing, actual_failed_turn: event.turn, failed_event: event }
        return event
      }
    }
    assert.equal(event.turn.status, 'completed', event.turn.error)
    return event
  }
  const exchange = async () => JSON.parse(await readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8'))
  const current = async () => {
    const count = (await events()).filter(item => item.event === 'memory').length
    await command({ action: 'memory_list' })
    await until(async () => (await events()).filter(item => item.event === 'memory').length > count)
    return (await events()).findLast(item => item.event === 'memory')
  }
  const selectScope = async scope => {
    const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
    const { dialog } = require('electron')
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
  }
  const recall = async () => {
    const before = (await events()).filter(item => item.event === 'conversation').length
    await command({ action: 'new' })
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > before)
    measured.current_new_event = (await events()).findLast(item => item.event === 'conversation')
    assert.equal(typeof measured.current_new_event.path, 'string')
    await selectScope(activeScope)
    const memory_state = await current()
    const start = await exchange()
    const recalled = await finish({ action: 'send', prompt: value.recall_prompt,
      scope: activeScope })
    const end = await exchange()
    const record = JSON.parse(await readFile(recalled.path, 'utf8'))
    assert.equal(typeof record.memory_contexts[activeScope.root], 'string')
    measured.windows.push({ sdk_start: start.sdk_inputs.length, sdk_end: end.sdk_inputs.length,
      http_start: start.exchanges.length, http_end: end.exchanges.length,
      applicable_context: record.memory_contexts[activeScope.root], memory_state })
    state.task_id = record.turns.at(-1).task.id
    if (measured.permission_change) {
      measured.permission_change.sdk_attempts_after_new = end.sdk_inputs.length - start.sdk_inputs.length
      measured.permission_change.http_after_new = end.exchanges.length - start.exchanges.length
      measured.permission_change.retained_http_before_new = start.exchanges.length
    }
  }
  await selectScope(request.scope)
  const seeded = await finish({ action: 'send', prompt: request.prompt, scope: request.scope })
  await writeFile(join(root, 'quality-memory-initial-history.json'), await readFile(seeded.path), { flag: 'wx' })
  measured.seed_current = await current()
  if (['qg4-5', 'qg4-6', 'qg4-9'].includes(value.adapter)) activeScope = request.project_b_scope
  await recall()
  if (!measured.permission_change && ['qg4-2', 'qg4-7', 'qg4-8'].includes(value.adapter)) {
    const prompts = value.adapter === 'qg4-2' ? [value.correction_prompt, value.removal_prompt] : value.adapter === 'qg4-7' ? [value.ambiguous_prompt] : [value.removal_prompt]
    for (const prompt of prompts) {
      await finish({ action: 'send', prompt, scope: request.scope })
      await recall()
    }
  }
  measured.current = await current()
  if (measured.permission_change) {
    const change = measured.permission_change
    const original = await readFile(join(root, 'quality-memory-initial-history.json'))
    const initialRecord = JSON.parse(original.toString('utf8'))
    const initial_original = 'quality-permission-initial.original.json'
    const new_original = 'quality-permission-new.original.json'
    await writeFile(join(root, initial_original), original, { flag: 'wx' })
    await writeFile(join(root, new_original), await readFile(change.path), { flag: 'wx' })
    const window = measured.windows.at(-1)
    await writeFile(join(root, 'quality-permission-change.json'), JSON.stringify({ schema_version: 1,
      origin: change.origin, actual_new_event: change.actual_new_event,
      initial_path: seeded.path, initial_task_id: initialRecord.turns[0].task.id,
      new_path: change.path, new_task_id: change.task_id, initial_original, new_original,
      before_http_count: window.http_start, after_http_count: window.http_end,
      failed_event: change.failed_event, explicit_mode_negative: false }, null, 2), { flag: 'wx' })
  }
  measured.passed = !measured.permission_change
  measured.dependent_steps_not_started = Boolean(measured.permission_change)
}
