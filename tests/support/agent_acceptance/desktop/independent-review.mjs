import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runIndependentReviewScenario({ authority, command, events, execute, state }) {
  const request = JSON.parse(await readFile(join(authority.runtime, 'scenario.json'), 'utf8'))
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
  assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
  state.effect_before_send = existsSync(join(authority.scenario_root, 'candidate.txt')) ? 'present' : 'absent'
  assert.equal(state.effect_before_send, 'absent')
  await command({ action: 'send', prompt: request.prompt, scope: request.scope, approval_mode: 'auto_review' })
  const deadline = Date.now() + 180000
  while (!(await events()).some(item => item.event === 'finished')) {
    const all = await events()
    assert.ok(!all.some(item => item.event === 'action' && item.action.status === 'awaiting_approval' && item.action.approval_settings?.reviewer === 'user'), 'Review failure cannot become ordinary user approval')
    if (authority.case_id === 'formal.review-stop' && !state.stop_sent && existsSync(join(authority.scenario_root, 'review-inflight.json'))) {
      state.stop_sent = true
      state.stop_requested_at = new Date().toISOString()
      await command({ action: 'stop' })
    }
    if (authority.case_id.startsWith('formal.review-changed-') && !state.subject_changed && existsSync(join(authority.scenario_root, 'review-inflight.json'))) {
      state.subject_changed = true
      if (authority.case_id === 'formal.review-changed-authorization') {
        await command({ action:'send', prompt:'Updated explicit authorization: preserve the candidate path absent; do not create it. The previous allow is no longer valid.' })
        while (!(await events()).some(item => item.event === 'supplement' && item.state === 'received')) {
          assert.ok(Date.now() < deadline, 'Actual supplemental authorization is received before the old decision returns')
          await pause(25)
        }
      } else await writeFile(join(authority.scenario_root,'source.txt'),'external-review-version-9037')
      state.subject_changed_at = new Date().toISOString()
      await writeFile(join(authority.scenario_root, 'review-release.txt'),'release-after-actual-public-or-material-change', { flag:'wx' })
    }
    const fatal = all.findLast(item => item.event === 'error' && item.fatal)
    assert.ok(!fatal, fatal?.message)
    assert.ok(Date.now() < deadline, 'Independent review finishes through the public Main command')
    await pause(25)
  }
  const all = await events()
  const finished = all.findLast(item => item.event === 'finished')
  if (authority.case_id === 'formal.review-timeout') assert.ok(finished.turn.actions.some(action => action.review?.status === 'timeout'), 'Actual review deadline, separate from ordinary denial')
  else assert.equal(finished.turn.status, authority.case_id === 'formal.review-stop' ? 'cancelled' : 'completed', finished.turn.error)
  const bytes = await readFile(finished.path)
  const record = JSON.parse(bytes)
  const candidates = record.turns.flatMap(turn => turn.actions).filter(action => action.review)
  state.candidates = candidates.map(action => {
    const proposal = all.find(item => item.event === 'action' && item.action.id === action.id && item.action.status === 'awaiting_approval')
    assert.ok(proposal, 'Actual prepared public action event is retained')
    for (const key of ['id', 'task_id', 'approval_id', 'kind', 'target', 'prepared_parameters', 'approval_settings', 'authorization_revision']) assert.deepEqual(proposal.action[key], action[key])
    return { public_proposal: proposal, saved_record_sha256_after_finished: createHash('sha256').update(bytes).digest('hex'),
      prepared_action_sha256_after_finished: action.prepared_action_sha256, approval_context_sha256_after_finished: action.approval_context_sha256 }
  })
  state.stop_event = all.findLast(item => item.event === 'stopping') ?? null
  state.task_id = finished.turn.task.id
  state.passed = true
}
