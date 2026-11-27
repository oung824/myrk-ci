import { app, BrowserWindow, dialog, Menu } from 'electron'
import assert from 'node:assert/strict'
import { appendFile, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const exitControl = installNormalExit(app)

app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const directory = process.env.MYRK_RECOVERY_DIRECTORY
const phase = process.env.MYRK_RECOVERY_PHASE
const log = (step, detail = {}) => appendFile(join(directory, `${phase}.jsonl`), JSON.stringify({ step, pid: process.pid, ...detail }) + '\n')
const waitFor = async check => {
  const deadline = Date.now() + 15000
  while (!await check()) { assert.ok(Date.now() < deadline, 'Timed out waiting for real recovery'); await pause(25) }
}
async function run() {
if (exitControl.requested) return
try {
await log('starting')
dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [directory] })
await import(builtMain())
await app.whenReady()
const attach = async () => {
  await waitFor(() => BrowserWindow.getAllWindows().length === 1)
  const window = BrowserWindow.getAllWindows()[0]
  await waitFor(() => !window.webContents.isLoading())
  assert.equal(window.isVisible(), false)
  await window.webContents.executeJavaScript(`window.recoveryEvents = []; window.myrk.subscribe(event => window.recoveryEvents.push(event)); window.myrk.command({action:'snapshot'})`)
  await waitFor(() => window.webContents.executeJavaScript(`window.recoveryEvents.some(event => event.event === 'snapshot')`))
  return window
}
const events = window => window.webContents.executeJavaScript('window.recoveryEvents')
const command = (window, value) => window.webContents.executeJavaScript(`window.myrk.command(${JSON.stringify(value)})`)
const click = (window, label) => window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(button => button.textContent === ${JSON.stringify(label)}).click()`)
  let window = await attach()
  if (phase === 'crash') {
    await command(window, { action: 'configure', key: 'controlled' })
    await window.webContents.executeJavaScript('window.myrk.selectWorkspace()')
    await command(window, { action: 'send', prompt: '改写再检查', scope: { root: directory, allowed_actions: ['read', 'edit', 'check'], provider: 'deepseek', recipients: ['deepseek'], checks: [{ name: '指定检查', argv: [process.env.MYRK_PYTHON, join(directory, 'check.py')] }] } })
    await waitFor(async () => { try { await readFile(process.env.MYRK_RECOVERY_MARKER); return true } catch { return false } })
    const started = (await events(window)).find(event => event.event === 'started')
    assert.equal(await readFile(join(directory, 'notes.txt'), 'utf8'), '苹果 5\n')
    await writeFile(join(directory, 'state.json'), JSON.stringify(started))
    await log('ready-to-crash', { started, service: Number(await readFile(join(process.env.MYRK_DATA_DIRECTORY, 'service.pid'), 'utf8')) })
  } else {
    const started = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8'))
    await command(window, { action: 'open', path: started.path })
    await waitFor(async () => (await events(window)).some(event => event.event === 'conversation' && event.path === started.path))
    const opened = (await events(window)).find(event => event.event === 'conversation' && event.path === started.path)
    assert.equal(opened.running, false)
    if (phase === 'recover') {
      assert.equal(opened.recovery.actions.at(-1).effect, 'unknown')
      assert.equal((await events(window)).some(event => event.event === 'started'), false)
      assert.ok(await window.webContents.executeJavaScript(`document.body.textContent.includes('影响未知')`))
      await click(window, '核查真实进展')
      await waitFor(async () => (await events(window)).some(event => event.event === 'recovery'))
      assert.equal((await events(window)).find(event => event.event === 'recovery').recovery.can_resume, true)
      await command(window, { action: 'configure', key: 'controlled' })
      await waitFor(() => window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).some(button => button.textContent === '继续原工作' && !button.disabled)`))
      await click(window, '继续原工作')
      await waitFor(async () => (await events(window)).some(event => event.event === 'finished'))
      const finished = (await events(window)).find(event => event.event === 'finished')
      assert.equal(finished.turn.status, 'completed')
      assert.deepEqual(finished.turn.actions.map(action => action.kind), ['check'])
      assert.equal(finished.turn.task.id, started.task_id)
      await log('resumed', { finished })
      window.close()
      await waitFor(() => window.isDestroyed())
      Menu.getApplicationMenu().getMenuItemById('myrk-open').click()
      window = await attach()
      assert.equal((await events(window)).find(event => event.event === 'snapshot').conversations[0].running, false)
    } else assert.equal(opened.recovery, null)
    assert.equal(await readFile(join(directory, 'runs.txt'), 'utf8'), 'x')
    assert.equal(await readFile(join(directory, 'notes.txt'), 'utf8'), '苹果 5\n')
    await log('normal-exit', { visible: window.isVisible() })
    await window.webContents.executeJavaScript('window.myrk.quit()')
  }
} catch (error) {
  await log('failed', { error: String(error), stack: error.stack })
  app.quit()
}
}
void run()
