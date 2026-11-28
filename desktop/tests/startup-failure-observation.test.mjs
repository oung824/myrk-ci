import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate as flushEvents } from 'node:timers/promises'
import { JSDOM } from 'jsdom'
import { observeStartupFailure } from '../../tests/support/agent_acceptance/desktop/startup-failure-observation.mjs'

const genericError = '与办事程序通信失败，请关闭窗口后重新启动。'
const originalError = '无法启动办事程序，请检查开发环境和 Python（执行办事逻辑的程序）路径。'

test('the startup failure receipt includes the original error rendered after 100 ms', async t => {
  const { observation, caption, advanceTo } = startObservation(t)
  await advanceTo(100)
  await advanceTo(150)
  caption.textContent = originalError
  const facts = await observation
  caption.textContent = genericError
  await flushEvents()

  assert.equal(facts.original_startup_error, true)
  assert.equal(facts.startup_failure_visible, true)
  assert.equal(facts.no_ready_success, true)
  assert.equal(facts.startup_status_observation.snapshot_reply, 'rejected')
  assert.deepEqual(Array.from(facts.startup_status_observation.states, state => state.caption), [genericError, originalError])
})

test('a transient ready state after 100 ms still fails the startup failure check', async t => {
  const { observation, caption, header, advanceTo } = startObservation(t)
  await advanceTo(125)
  header.textContent = '准备就绪'
  await advanceTo(150)
  header.textContent = '办事程序不可用'
  caption.textContent = originalError
  const facts = await observation

  assert.equal(facts.original_startup_error, true)
  assert.equal(facts.no_ready_success, false)
  assert.deepEqual(Array.from(facts.startup_status_observation.states, state => state.header), [
    '办事程序不可用', '准备就绪', '办事程序不可用',
  ])
})

test('the observation deadline saves the evidence if either composer control stays enabled', async t => {
  const { observation, caption, input, send, advanceTo } = startObservation(t)
  let settled = false
  void observation.then(() => { settled = true })
  await advanceTo(150)
  caption.textContent = originalError
  send.disabled = false
  await advanceTo(175)
  send.disabled = true
  input.disabled = false
  await advanceTo(1999)
  assert.equal(settled, false)
  await advanceTo(2000)
  const facts = await observation

  assert.equal(facts.original_startup_error, true)
  assert.equal(facts.no_ready_success, false)
  assert.equal(facts.startup_status_observation.snapshot_reply, 'rejected')
  assert.equal(facts.startup_status_observation.states.at(-1).input_disabled, false)
})

test('a confirmed failure state does not replace the snapshot reply timeout', async t => {
  const { observation, caption, advanceTo } = startObservation(t, () => new Promise(() => {}))
  let settled = false
  void observation.then(() => { settled = true })
  await advanceTo(150)
  caption.textContent = originalError
  await advanceTo(1999)
  assert.equal(settled, false)
  await advanceTo(2000)
  const facts = await observation

  assert.equal(facts.startup_status_observation.snapshot_reply, 'no_reply')
  assert.equal(facts.original_startup_error, true)
  assert.equal(facts.no_ready_success, false)
})

function startObservation(t, sendSnapshot = () => Promise.reject(new Error('Unavailable test service'))) {
  const dom = new JSDOM(`
    <div class="caption"><span role="status">${genericError}</span></div>
    <header class="topbar"><span class="main-status">办事程序不可用</span></header>
    <form class="composer"><textarea disabled></textarea><button type="submit" disabled>发送</button></form>
  `, { runScripts: 'outside-only' })
  t.after(() => dom.window.close())
  t.mock.timers.enable({ apis: ['setTimeout'] })
  dom.window.setTimeout = setTimeout
  dom.window.clearTimeout = clearTimeout
  dom.window.myrk = {
    command(command) {
      assert.equal(command.action, 'snapshot')
      return sendSnapshot()
    },
  }
  const document = dom.window.document
  let now = 0
  return {
    observation: observeStartupFailure(script => dom.window.eval(script)),
    caption: document.querySelector('.caption [role=status]'),
    header: document.querySelector('.topbar .main-status'),
    input: document.querySelector('.composer textarea'),
    send: document.querySelector('.composer button[type=submit]'),
    async advanceTo(milliseconds) {
      await flushEvents()
      t.mock.timers.tick(milliseconds - now)
      now = milliseconds
      await flushEvents()
    },
  }
}
