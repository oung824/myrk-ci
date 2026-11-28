import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises'
import { dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { runScenario } from '../../tests/support/agent_acceptance/desktop/scenarios.mjs'
import { projectRoot } from './project-paths.mjs'

const project = projectRoot(resolve(dirname(fileURLToPath(import.meta.url)), '../..'))
const payloadHash = 'a'.repeat(64)

async function sendScenario(t, { approved = true, changedParameter = false, unrelated = false, saved = true } = {}) {
  const scratch = join(project, '.scratch')
  const parent = await realpath(process.env.MYRK_TEST_RUN_DIRECTORY ?? scratch)
  const part = relative(scratch, parent)
  assert.ok(!part.startsWith('..') && !isAbsolute(part), 'Approval test files must stay in project scratch space')
  const root = await mkdtemp(join(parent, 'scenario-approval-'))
  t.after(async () => {
    assert.equal(await realpath(root), root)
    await rm(root, { recursive: true })
  })
  const evidence = join(root, 'receiver')
  await mkdir(evidence)
  const path = join(root, 'record.json')
  const source = join(root, 'payload.txt')
  const task_id = 'send-task'
  const stream = []
  const decisions = []
  const state = {}
  const finished = { event: 'finished', path, task_id, saved, turn: { task: { id: task_id }, status: 'completed' } }
  await writeFile(path, JSON.stringify({ turns: [] }), { flag: 'wx' })
  await writeFile(join(root, 'scenario.json'), JSON.stringify({
    records: root, prompt: 'Send the specified payload.', approval_mode: 'request_approval',
    receiver: { path: join(root, 'receiver.json'), source, payload_sha256: payloadHash },
  }))

  let proposal
  async function finish() {
    if (saved) {
      const final = {
        ...structuredClone(proposal),
        status: approved ? 'completed' : 'denied', effect: approved ? 'completed' : 'not_started',
        approval_basis: approved ? 'user' : 'denied',
        prepared_action_sha256: 'b'.repeat(64), approval_context_sha256: 'c'.repeat(64),
      }
      if (changedParameter) final.prepared_parameters.path = join(root, 'wrong-payload.txt')
      const next = join(root, 'record.next')
      await writeFile(next, JSON.stringify({ turns: [{ task: { id: task_id }, actions: [final] }] }), { flag: 'wx' })
      await rename(next, path)
    }
    stream.push(finished)
  }
  async function command(value) {
    if (value.action === 'send') {
      const url = value.prompt.split('\nExact isolated receiver URL: ')[1]
      assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/receive$/)
      proposal = {
        id: 'send-action', task_id, approval_id: 'send-approval', kind: 'send', target: url,
        status: 'awaiting_approval',
        prepared_parameters: { path: source, url, payload_sha256: approved ? payloadHash : 'd'.repeat(64) },
        approval_settings: { reviewer: 'user' }, authorization_revision: 'authorization',
      }
      stream.push({ event: 'started', path, task_id, task: { id: task_id } })
      if (unrelated) {
        stream.push({ ...finished, task_id: 'other-task', turn: { task: { id: 'other-task' }, status: 'completed' } })
        stream.push({ ...finished, path: join(root, 'other-record.json') })
      }
      stream.push({ event: 'action', path, task_id, action: structuredClone(proposal) })
      return
    }
    assert.deepEqual(value, {
      action: 'approve_action', path, task_id, action_id: proposal.id, approval_id: proposal.approval_id, approved,
    })
    assert.deepEqual(JSON.parse(await readFile(path, 'utf8')), { turns: [] })
    decisions.push(value)
    await finish()
  }

  const run = () => runScenario({
    authority: { case_id: 'formal.send', runtime: root, scenario_root: root, grant: { transport: 'mock' } },
    command, events: async () => structuredClone(stream), execute: async () => assert.fail('Unexpected renderer call'), state,
  })
  return { run, decisions, state }
}

test('formal send confirms from the public event while the saved record is still stale', async t => {
  const { run, decisions, state } = await sendScenario(t)
  await run()
  assert.equal(decisions.length, 1)
  assert.equal(state.approval_decisions[0].observation_source, 'public_main_action_event')
  assert.equal(state.approval_decisions[0].record_sha256_at_observation, null)
  assert.equal(state.approval_corroboration.status, 'passed')
  assert.equal(state.passed, true)
})

test('formal send refuses a different payload hash before the final record is saved', async t => {
  const { run, decisions, state } = await sendScenario(t, { approved: false })
  await run()
  assert.deepEqual(decisions.map(item => item.approved), [false])
  assert.equal(state.approval_corroboration.actions[0].approval_id, 'send-approval')
  assert.equal(state.passed, true)
})

test('formal send rejects final parameters that differ from the public proposal', async t => {
  const { run, decisions, state } = await sendScenario(t, { changedParameter: true })
  await assert.rejects(run, /Saved prepared_parameters differs from the actual public proposal/)
  assert.deepEqual(decisions.map(item => item.approved), [true])
  assert.equal(state.approval_decisions.length, 1)
  assert.equal(state.passed, undefined)
})

test('formal send waits for a finish on both the matching task and record path', async t => {
  const { run, decisions, state } = await sendScenario(t, { unrelated: true })
  await run()
  assert.equal(decisions.length, 1)
  assert.equal(state.task_id, 'send-task')
  assert.equal(state.approval_corroboration.status, 'passed')
})

test('formal send requires a successful save before reading back the approval', async t => {
  const { run, state } = await sendScenario(t, { saved: false })
  await assert.rejects(run, /The approval record must be saved before corroboration/)
  assert.equal(state.approval_corroboration, undefined)
})
