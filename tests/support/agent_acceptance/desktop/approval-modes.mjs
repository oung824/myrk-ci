import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join, relative, resolve, isAbsolute } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runApprovalScenario({ authority, command, events, execute, state }) {
  const request = JSON.parse(await readFile(join(authority.runtime, 'scenario.json'), 'utf8'))
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
  assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
  state.approval_decisions = []
  state.rendered_observations = []
  const seen = new Set()
  state.effect_before_send = existsSync(join(authority.scenario_root, 'candidate.txt')) ? 'present' : 'absent'
  assert.equal(state.effect_before_send, 'absent')
  await command({ action: 'send', prompt: request.prompt, scope: request.scope, approval_mode: request.approval_mode })
  const deadline = Date.now() + (authority.grant.transport === 'mock' ? 180000 : 1200000)
  while (!(await events()).some(item => item.event === 'finished')) {
    for (const [index, value] of (await events()).entries()) {
      if (value.event !== 'action' || value.action.status !== 'awaiting_approval' || value.action.approval_settings?.reviewer !== 'user') continue
      const action = value.action
      const key = `${value.path}:${value.task_id}:${action.id}:${action.approval_id}`
      if (seen.has(key)) continue
      seen.add(key)
      const part = relative(resolve(request.records), resolve(value.path))
      assert.ok(part && !part.startsWith('..') && !isAbsolute(part) && part.endsWith('.json'))
      const changed = authority.case_id === 'formal.approval-changed-subject' && state.approval_decisions.length === 1
      assert.equal(action.target, join(authority.scenario_root, changed ? 'changed-target.txt' : 'candidate.txt'))
      assert.equal(action.prepared_parameters.path, action.target)
      assert.equal(action.prepared_parameters.content, changed ? 'unapproved-changed-content' : 'approved-exact-bytes')
      assert.equal(existsSync(action.target), false, 'The approved effect must be absent before deciding')
      if (changed) {
        const old = state.approval_decisions[0]
        assert.notEqual(action.id, old.action_id)
        assert.notEqual(action.approval_id, old.approval_id)
        const errors = (await events()).filter(item => item.event === 'error').length
        await command({ action: 'approve_action', path: value.path, task_id: value.task_id, action_id: old.action_id, approval_id: old.approval_id, approved: true })
        while ((await events()).filter(item => item.event === 'error').length === errors) {
          assert.ok(Date.now() < deadline, 'Old approval is explicitly rejected')
          await pause(25)
        }
        const rejection = (await events()).filter(item => item.event === 'error').at(-1)
        assert.match(rejection.message, /不对应当前待批准动作/)
        assert.equal(existsSync(action.target), false)
        state.stale_decision_rejected = true
        state.stale_decision_rejection = rejection
      }
      let rendered
      if (authority.case_id === 'formal.user-approval') {
        const inspect = `(() => {
          const node = [...document.querySelectorAll('.pending-work section.action-awaiting_approval')].find(item => item.getAttribute('aria-label') === ${JSON.stringify('动作：')} + ${JSON.stringify(action.target)});
          if (!node) return null;
          const details = node.querySelector('details.action-parameters');
          if (details && !details.open) details.querySelector('summary').click();
          return { text: node.innerText, buttons: [...node.querySelectorAll('button')].map(item => ({ text: item.textContent, disabled: item.disabled })) };
        })()`
        while (!(rendered = await execute(inspect))) {
          assert.ok(Date.now() < deadline, 'The actual pending subject is rendered before deciding')
          await pause(25)
        }
        state.rendered_observations.push({ observed_at: new Date().toISOString(), event: value, rendered })
        await writeFile(join(authority.runtime, 'receiver', `approval-rendered-${action.id}.json`), JSON.stringify(state.rendered_observations.at(-1), null, 2), { flag: 'wx' })
        assert.ok(rendered.text.includes(action.target))
        assert.ok(rendered.text.includes(action.prepared_parameters.content))
        assert.ok(rendered.buttons.some(item => item.text === '批准这次动作' && !item.disabled))
        assert.ok(rendered.buttons.some(item => item.text === '拒绝这次动作' && !item.disabled))
      }
      const decision = { event: value, event_index: index, observed_at: new Date().toISOString(),
        task_id: value.task_id, action_id: action.id, approval_id: action.approval_id,
        target: action.target, prepared_parameters: action.prepared_parameters,
        decider: 'offline-local-driver', decision_origin: 'synthetic decision through public approve_action',
        approved: authority.case_id === 'formal.approval-stop-pending' ? null : authority.case_id !== 'formal.approval-deny' && !changed,
        rendered_before_decision: rendered ?? null, decision_input: rendered ? 'renderer_button' : 'public_command', effect_before_decision: 'absent', record_sha256_at_observation: null }
      state.approval_decisions.push(decision)
      await writeFile(join(authority.runtime, 'receiver', `approval-${action.id}-${action.approval_id}.json`), JSON.stringify(decision, null, 2), { flag: 'wx' })
      if (authority.case_id === 'formal.approval-stop-pending') {
        decision.control = 'stop'
        await command({ action: 'stop' })
      } else if (rendered) {
        await execute(`(() => { const node = [...document.querySelectorAll('.pending-work section.action-awaiting_approval')].find(item => item.getAttribute('aria-label') === ${JSON.stringify('动作：')} + ${JSON.stringify(action.target)}); const button = [...node.querySelectorAll('button')].find(item => item.textContent === '批准这次动作'); button.click(); })()`)
      } else await command({ action: 'approve_action', path: value.path, task_id: value.task_id,
        action_id: action.id, approval_id: action.approval_id, approved: decision.approved })
    }
    const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
    assert.ok(!fatal, fatal?.message)
    assert.ok(Date.now() < deadline, 'Approval task finishes')
    await pause(25)
  }
  const finished = (await events()).findLast(item => item.event === 'finished')
  const stopping = (await events()).findLast(item => item.event === 'stopping')
  state.stop_accepted = Boolean(stopping)
  state.stop_event = stopping ?? null
  assert.equal(finished.turn.status, authority.case_id === 'formal.approval-stop-pending' ? 'cancelled' : 'completed', finished.turn.error)
  const original = await readFile(finished.path)
  const saved = JSON.parse(original)
  for (const decision of state.approval_decisions) {
    const action = saved.turns.filter(turn => turn.task?.id === decision.task_id).flatMap(turn => turn.actions).find(item => item.id === decision.action_id)
    assert.ok(action)
    for (const key of ['id', 'task_id', 'approval_id', 'kind', 'target', 'impact', 'prepared_parameters', 'approval_settings', 'authorization_revision', 'tool_name', 'tool_arguments', 'tool_source', 'tool_schema_version']) {
      assert.deepEqual(action[key], decision.event.action[key], `Saved ${key} matches the actual public prepared event`)
    }
    assert.match(action.prepared_action_sha256, /^[a-f0-9]{64}$/)
    assert.match(action.approval_context_sha256, /^[a-f0-9]{64}$/)
    decision.saved_candidate_matches = true
    decision.saved_record_sha256_after_finished = createHash('sha256').update(original).digest('hex')
    decision.prepared_action_sha256_after_finished = action.prepared_action_sha256
    decision.approval_context_sha256_after_finished = action.approval_context_sha256
  }
  state.task_id = finished.turn.task.id
  state.passed = true
}
