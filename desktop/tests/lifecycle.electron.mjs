import { app, BrowserWindow, Menu } from 'electron'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { appendFile, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { setTimeout as pause } from 'node:timers/promises'
import { resolve } from 'node:path'
import { existsSync } from 'node:fs'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'
import { ProcessObservation } from './process-observation.mjs'

const exitControl = installNormalExit(app)

app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const directory = process.env.MYRK_LIFECYCLE_DIRECTORY
const dataDirectory = process.env.MYRK_DATA_DIRECTORY
const savedDirectory = resolve(directory, 'records-before-save-block')
const log = async (step, detail = {}) => appendFile(resolve(directory, 'electron.jsonl'), JSON.stringify({ step, pid: process.pid, ...detail }) + '\n')
const waitFor = async (check) => {
  const until = Date.now() + 12000
  while (!await check()) {
    assert.ok(Date.now() < until, 'Timed out waiting for desktop behavior')
    await pause(30)
  }
}
const attach = async () => {
  await waitFor(() => BrowserWindow.getAllWindows().length === 1)
  const window = BrowserWindow.getAllWindows()[0]
  await waitFor(() => !window.webContents.isLoading())
  assert.equal(window.isVisible(), false)
  await window.webContents.executeJavaScript(`window.lifecycleEvents = []; window.myrk.subscribe(event => window.lifecycleEvents.push(event)); window.myrk.command({action:'snapshot'})`)
  await waitFor(() => window.webContents.executeJavaScript(`window.lifecycleEvents.some(event => event.event === 'snapshot')`))
  return window
}
const command = (window, value) => window.webContents.executeJavaScript(`window.myrk.command(${JSON.stringify(value)})`)
const events = window => window.webContents.executeJavaScript('window.lifecycleEvents')
async function run() {
if (exitControl.requested) return
const failedSave = process.env.MYRK_LIFECYCLE_SAVE_FAILURE === '1'
try {
await log('starting')
await import(builtMain())
await app.whenReady()
  let window = await attach()
  await command(window, { action: 'configure', key: 'lifecycle-controlled' })
  await command(window, { action: 'send', prompt: '后台持续工作甲' })
  await waitFor(async () => (await events(window)).some(event => event.event === 'text'))
  const first = (await events(window)).find(event => event.event === 'started')
  await command(window, { action: 'send', prompt: '不应在退出时开始的排队工作', new_task: true })
  await command(window, { action: 'new' })
  await command(window, { action: 'send', prompt: '后台持续工作乙' })
  await waitFor(async () => (await events(window)).some(event => event.event === 'started' && event.prompt === '后台持续工作乙'))
  const second = (await events(window)).find(event => event.event === 'started' && event.prompt === '后台持续工作乙')
  await waitFor(async () => (await events(window)).some(event => event.event === 'text' && event.path === second.path))
  await log('running', { first: first.path, second: second.path, tasks: [first.task_id, second.task_id] })
  if (!failedSave) {
    for (let count = 0; count < 2; count++) {
      const previousId = window.id
      window.close()
      await waitFor(() => window.isDestroyed())
      await pause(250)
      assert.equal(BrowserWindow.getAllWindows().length, 0)
      const reopen = Menu.getApplicationMenu()?.getMenuItemById('myrk-open')
      assert.ok(reopen, 'User needs an actual reopen entry')
      if (count === 0) {
        const observer = await ProcessObservation.start(resolve(directory, 'second-observer'))
        const release = resolve(directory, 'second-release.flag')
        const secondResult = resolve(directory, 'second-launch-result.json')
        const argv = [resolve('tests/reopen.electron.mjs')]
        const secondLaunch = spawn(process.execPath, argv, { windowsHide: true, stdio: 'ignore', env: { ...process.env, MYRK_REOPEN_RELEASE: release, MYRK_REOPEN_RESULT: secondResult } })
        const closed = new Promise(resolveClosed => secondLaunch.once('close', resolveClosed))
        try {
          const identity = await observer.capture(secondLaunch, [process.execPath, ...argv], process.cwd())
          await writeFile(resolve(directory, 'second-launch-identity.json'), JSON.stringify(identity, null, 2))
          assert.equal(identity.ownership, 'owned')
          await writeFile(release, 'accurate original spawn captured\n')
          const exit = await closed
          assert.equal(exit, 0)
          const result = JSON.parse(await readFile(secondResult, 'utf8'))
          assert.equal(result.step, 'loaded')
          assert.equal(result.pid, secondLaunch.pid)
          const report = await observer.wait(5000)
          assert.equal(report.complete, true)
          await log('second-launch', { launcher: secondLaunch.pid, exit, result, report })
        } finally {
          if (secondLaunch.exitCode === null && secondLaunch.signalCode === null) {
            await writeFile(process.env.MYRK_TEST_NORMAL_EXIT_FLAG, 'normal exit after failed second launch\n')
            await closed
          }
          await observer.close()
        }
      } else reopen.click()
      window = await attach()
      assert.notEqual(window.id, previousId)
      const snapshot = (await events(window)).find(event => event.event === 'snapshot')
      assert.equal(snapshot.configured, true)
      assert.equal(snapshot.conversations.length, 2)
      assert.equal(snapshot.conversations.find(item => item.path === first.path).queue.length, 1)
      for (const [index, path] of [first.path, second.path].entries()) {
        const session = snapshot.conversations.find(item => item.path === path)
        assert.equal(session.running, true)
        assert.equal(session.turns[0].task.id, [first.task_id, second.task_id][index])
        assert.equal(session.turns[0].task.requests, 1)
        assert.ok(session.turns[0].answer.includes('进展'))
      }
      await log('reopened', { count, window: window.id, snapshot })
    }
    await command(window, { action: 'configure', active_seconds_limit: 0.15 })
    await command(window, { action: 'new' })
    await command(window, { action: 'send', prompt: '关闭窗口期间继续工作' })
    await waitFor(async () => (await events(window)).some(event => event.event === 'started' && event.prompt === '关闭窗口期间继续工作'))
    const backgroundStart = (await events(window)).find(event => event.event === 'started' && event.prompt === '关闭窗口期间继续工作')
    window.close()
    await waitFor(() => window.isDestroyed())
    await pause(450)
    Menu.getApplicationMenu().getMenuItemById('myrk-open').click()
    window = await attach()
    const background = (await events(window)).find(event => event.event === 'snapshot').conversations.find(item => item.path === backgroundStart.path)
    assert.equal(background.running, true)
    assert.equal(background.turns[0].status, 'running')
    assert.equal(background.turns[0].task.id, backgroundStart.task_id)
    assert.equal(background.turns[0].task.requests, 1)
    assert.ok(background.turns[0].task.active_seconds >= 0.15)
    await log('background-running', { session: background })
    await command(window, { action: 'stop', path: backgroundStart.path })
    await waitFor(async () => (await events(window)).some(event => event.event === 'finished' && event.path === backgroundStart.path))
    const stopped = (await events(window)).find(event => event.event === 'finished' && event.path === backgroundStart.path)
    assert.equal(stopped.turn.status, 'cancelled')
    assert.equal(stopped.turn.task.id, backgroundStart.task_id)
    assert.equal(stopped.turn.task.requests, 1)
    assert.ok(stopped.turn.task.active_seconds >= background.turns[0].task.active_seconds)
    assert.equal(stopped.saved, true)
    const saved = JSON.parse(await readFile(backgroundStart.path, 'utf8')).turns[0]
    assert.equal(saved.status, 'cancelled')
    assert.equal(saved.task.id, backgroundStart.task_id)
    assert.equal(saved.task.requests, 1)
    assert.ok(saved.task.active_seconds >= 0.15)
    await log('background-stopped', { stopped, saved })
  }
  if (failedSave) {
    assert.equal(resolve(dataDirectory), resolve(directory, 'records'))
    await rename(dataDirectory, savedDirectory)
    await writeFile(dataDirectory, 'unrelated blocker retained until test repairs it')
    const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
    assert.ok(quit, 'User needs the actual quit menu entry')
    quit.click()
    await waitFor(() => window.webContents.executeJavaScript(`document.body.textContent.includes('退出未完成')`))
    assert.equal(window.isDestroyed(), false)
    await log('save-blocked', { text: await window.webContents.executeJavaScript('document.body.textContent') })
    await rm(dataDirectory)
    await rename(savedDirectory, dataDirectory)
  }
  await log('exit-requested')
  const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
  assert.ok(quit, 'User needs the actual quit menu entry')
  quit.click()
} catch (error) {
  await log('failed', { error: String(error), stack: error.stack })
  if (failedSave && existsSync(savedDirectory)) {
    if (existsSync(dataDirectory) && await readFile(dataDirectory, 'utf8').catch(() => '') === 'unrelated blocker retained until test repairs it') {
      await rm(dataDirectory)
    }
    await rename(savedDirectory, dataDirectory)
  }
  app.quit()
}
}
void run()
