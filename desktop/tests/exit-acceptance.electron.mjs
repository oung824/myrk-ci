import { app, BrowserWindow, dialog, Menu } from 'electron'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { appendFile, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const control = installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const directory = process.env.MYRK_EXIT_CASE_DIRECTORY
const phase = process.env.MYRK_EXIT_PHASE
const mode = process.env.MYRK_EXIT_MODE
const log = (step, detail = {}) => appendFile(join(directory, `${phase}.jsonl`), JSON.stringify({ step, pid: process.pid, at: new Date().toISOString(), ...detail }) + '\n')
const waitFor = async (condition, label, milliseconds = 15000) => {
  const deadline = Date.now() + milliseconds
  while (!await condition()) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(25)
  }
}

async function run() {
  if (control.requested) return
  let shown = false
  app.on('browser-window-created', (_event, window) => window.on('show', () => { shown = true }))
  try {
    await log('starting')
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [directory] })
    await import(builtMain())
    await app.whenReady()
    await waitFor(() => BrowserWindow.getAllWindows().length === 1, 'one real hidden page')
    const window = BrowserWindow.getAllWindows()[0]
    await waitFor(() => !window.webContents.isLoading(), 'page loaded')
    assert.equal(window.isVisible(), false)
    assert.equal(window.webContents.isOffscreen(), true)
    const execute = source => window.webContents.executeJavaScript(source)
    const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
    const events = () => execute('window.exitEvents')
    await execute('window.exitEvents=[];window.myrk.subscribe(event=>window.exitEvents.push(event));window.myrk.command({action:"snapshot"})')
    await waitFor(async () => (await events()).some(event => event.event === 'snapshot'), 'real service snapshot')
    if (phase === 'first') {
      await command({ action: 'configure', key: 'exit-acceptance-controlled' })
      await waitFor(async () => (await events()).some(event => event.event === 'configured'), 'controlled connection')
      await execute('window.myrk.selectWorkspace()')
      await command({ action: 'send', prompt: mode === 'approval' ? '先改notes.txt，再申请删除keep.txt，等待我决定。' : '先把notes.txt中的苹果3改为苹果5，然后运行指定检查。', scope: {
        root: directory, allowed_actions: ['read', 'edit', 'check'], provider: 'deepseek', recipients: ['deepseek'],
        checks: [{ name: '指定检查', argv: [process.env.MYRK_PYTHON, '-u', '-m', 'tests.exit_acceptance_fixture', 'check'] }],
      } })
      await waitFor(() => execute(`document.querySelector('[role="status"]').getClientRects().length > 0 && document.querySelector('[role="status"]').textContent.includes('正在等待回答服务')`), 'actual waiting-for-service display')
      await log('waiting-service-observed', { text: await execute(`document.querySelector('[role="status"]').textContent`) })
      if (mode === 'approval') {
        await waitFor(() => execute(`document.body.textContent.includes('删除：') && document.body.textContent.includes('等待你批准') && Array.from(document.querySelectorAll('button')).some(button=>button.textContent==='批准这次动作' && button.getClientRects().length > 0)`), 'actual pending approval display')
        await log('approval-observed', { text: await execute('document.body.textContent') })
      } else {
        await waitFor(() => execute(`Array.from(document.querySelectorAll('[role="region"][aria-label="待处理事项"] [aria-label="动作：指定检查"] > p')).some(element => {
          const title = element.querySelector('strong'); const status = element.querySelector('span');
          return title && status && title.getClientRects().length > 0 && status.getClientRects().length > 0 && title.textContent === '指定检查：指定检查' && status.textContent === '执行中';
        })`), 'actual specified check in progress')
        await log('executing-observed', { text: await execute('document.body.textContent') })
        await waitFor(() => existsSync(join(directory, 'heartbeat.txt')), 'real child heartbeat')
        if (mode === 'timeout') {
          await waitFor(async () => (await events()).some(event => event.event === 'finished'), 'real default 60-second completion', 75000)
          const finished = (await events()).find(event => event.event === 'finished')
          assert.equal(finished.turn.actions.find(action => action.kind === 'check').timed_out, true)
          assert.ok(await execute(`document.body.textContent.includes('超时') && document.body.textContent.includes('实际检查未通过')`))
          await log('default-timeout-observed', { finished, text: await execute('document.body.textContent') })
        }
      }
      const started = (await events()).find(event => event.event === 'started')
      assert.ok(started?.task_id)
      await writeFile(join(directory, 'state.json'), JSON.stringify(started))
      await log('ready-to-exit', { started, mode })
    } else {
      const original = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8'))
      await command({ action: 'open', path: original.path })
      await waitFor(async () => (await events()).some(event => event.event === 'conversation' && event.path === original.path), 'original conversation reopened')
      const opened = (await events()).find(event => event.event === 'conversation' && event.path === original.path)
      assert.equal(opened.running, false)
      assert.equal(opened.turns[0].task.id, original.task_id)
      await command({ action: 'configure', key: 'exit-acceptance-controlled' })
      await waitFor(async () => (await events()).some(event => event.event === 'configured'), 'test connection restored without continue')
      await pause(750)
      assert.equal((await events()).some(event => event.event === 'started'), false)
      if (mode !== 'approval') {
        assert.ok(await execute(`document.body.textContent.includes('real-exit-check-stdout') && document.body.textContent.includes('real-exit-check-stderr')`))
      }
      await log('reopened-without-start', { opened })
    }
    assert.equal(shown, false)
    assert.equal(window.isVisible(), false)
    await log('explicit-page-quit', { visible: window.isVisible(), shown })
    const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
    assert.ok(quit, 'User needs the actual quit menu entry')
    quit.click()
  } catch (error) {
    await log('failed', { error: String(error), stack: error.stack })
    app.quit()
  }
}
void run()
