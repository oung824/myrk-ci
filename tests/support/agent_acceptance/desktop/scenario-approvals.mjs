import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { isAbsolute, join, relative, resolve } from 'node:path'

const proposalFields = [
  'id', 'task_id', 'kind', 'target', 'impact', 'approval_id', 'prepared_request',
  'prepared_parameters', 'approval_settings', 'authorization_revision', 'tool_name',
  'tool_arguments', 'tool_source', 'tool_schema_version',
]

export function scenarioApprovals({ authority, request, command }) {
  const decisions = []
  const seen = new Set()
  const evidence = join(authority.runtime, 'receiver')

  async function decide(events, receiver) {
    for (const event of events) {
      const { action } = event
      if (event.event !== 'action' || action.status !== 'awaiting_approval'
        || action.approval_settings?.reviewer !== 'user') continue
      const key = JSON.stringify([event.path, event.task_id, action.id, action.approval_id])
      if (seen.has(key)) continue
      const part = relative(resolve(request.records), resolve(event.path))
      assert.ok(part && !part.startsWith('..') && !isAbsolute(part) && part.endsWith('.json'),
        'Actual pending record must belong to this scenario')
      assert.equal(action.task_id, event.task_id)
      const parameters = action.prepared_parameters
      assert.ok(action.approval_id && parameters && action.authorization_revision)
      const approved = authority.case_id === 'formal.send' && action.kind === 'send'
        && action.target === receiver.url && parameters.url === receiver.url
        && parameters.path === request.receiver.source && parameters.payload_sha256 === request.receiver.payload_sha256
      const observation = { action, task_id: event.task_id, approval_id: action.approval_id, event,
        record: relative(evidence, event.path), observation_source: 'public_main_action_event',
        record_sha256_at_observation: null, approved }
      await writeFile(join(evidence, `approval-${action.id}-${action.approval_id}.json`),
        JSON.stringify(observation, null, 2), { flag: 'wx' })
      decisions.push(observation)
      seen.add(key)
      await command({ action: 'approve_action', path: event.path, task_id: event.task_id,
        action_id: action.id, approval_id: action.approval_id, approved })
    }
  }

  async function corroborate(finished, observations) {
    assert.equal(finished.saved, true, 'The approval record must be saved before corroboration')
    const recordBytes = await readFile(finished.path)
    const record = JSON.parse(recordBytes.toString('utf8'))
    const turnActions = record.turns.filter(turn => turn.task?.id === finished.turn.task.id).flatMap(turn => turn.actions)
    const actions = observations.map(observation => {
      const candidates = turnActions.filter(action => action.id === observation.action.id)
      assert.equal(candidates.length, 1, 'One saved action must match the public proposal')
      const saved = candidates[0]
      for (const field of proposalFields) {
        assert.deepEqual(saved[field], observation.action[field], `Saved ${field} differs from the actual public proposal`)
      }
      const terminal = observation.approved ? ['completed', 'completed', 'user'] : ['denied', 'not_started', 'denied']
      assert.deepEqual([saved.status, saved.effect, saved.approval_basis], terminal,
        'Saved approval outcome differs from the actual decision')
      assert.match(saved.prepared_action_sha256, /^[a-f0-9]{64}$/)
      assert.match(saved.approval_context_sha256, /^[a-f0-9]{64}$/)
      return { action_id: saved.id, approval_id: saved.approval_id,
        prepared_action_sha256_after_finished: saved.prepared_action_sha256,
        approval_context_sha256_after_finished: saved.approval_context_sha256 }
    })
    const result = { source: 'saved_record_after_finished', status: 'passed',
      record_sha256_after_finished: createHash('sha256').update(recordBytes).digest('hex'), actions }
    if (authority.case_id === 'formal.guidance-untrusted') {
      await writeFile(join(evidence, 'approval-after-finished.json'), JSON.stringify(result, null, 2), { flag: 'wx' })
    }
    return result
  }

  return { decisions, decide, corroborate }
}
