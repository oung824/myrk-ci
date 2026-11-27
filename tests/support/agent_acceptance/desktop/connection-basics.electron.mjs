import assert from 'node:assert/strict'
import { app, BrowserWindow, Menu } from 'electron'
import { writeFileSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'

const exitControl = installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const phase = process.env.MYRK_CONNECTION_BASIC_PHASE
const root = join(process.env.MYRK_CONNECTION_BASIC_ROOT, phase)
const result = { phase, pid: process.pid, observed_at: new Date().toISOString(), passed: false }
const until = async (check, label) => { const end = Date.now() + 20000; while (!await check()) { assert.ok(Date.now() < end, label); await pause(25) } }
async function run() { let execute; try {
  await mkdir(root, { recursive: true })
  await import(builtMain())
  await app.whenReady()
  result.actual_profile = app.getPath("userData")
  result.isolated_profile = result.actual_profile === process.env.MYRK_USER_DATA_DIRECTORY
  assert.equal(result.isolated_profile,true)
  await until(() => BrowserWindow.getAllWindows().length === 1, 'actual Main window')
  let window = BrowserWindow.getAllWindows()[0]
  await until(() => !window.webContents.isLoading(), 'renderer loaded')
  execute = script => window.webContents.executeJavaScript(script)
  const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
  const attach = async () => {
    await execute('window.connectionBasicEvents=[];window.myrk.subscribe(value=>window.connectionBasicEvents.push(value));window.myrk.command({action:"snapshot"})')
    await until(async () => (await execute('window.connectionBasicEvents')).some(row => row.event === 'snapshot'), 'real service snapshot')
  }
  const events = () => execute('window.connectionBasicEvents')
  if (phase === 'startup-failure') {
    await until(() => execute('Boolean(document.querySelector(".caption [role=status]") && document.querySelector(".topbar .main-status") && document.querySelector(".composer textarea") && document.querySelector(".composer button[type=submit]"))'), 'startup status and composer rendered')
    const startup = await execute(`(async () => {
      const caption = document.querySelector('.caption [role=status]')
      const header = document.querySelector('.topbar .main-status')
      const input = document.querySelector('.composer textarea')
      const send = document.querySelector('.composer button[type=submit]')
      const states = []
      const observe = () => {
        const state = { caption: caption.textContent, header: header.textContent, input_disabled: input.disabled, send_disabled: send.disabled }
        if (JSON.stringify(states.at(-1)) !== JSON.stringify(state)) states.push(state)
      }
      const observer = new MutationObserver(observe)
      for (const status of [caption, header]) observer.observe(status, { childList: true, characterData: true, subtree: true })
      for (const control of [input, send]) observer.observe(control, { attributes: true, attributeFilter: ['disabled'] })
      observe()
      let timeout
      const snapshotReply = await Promise.race([
        window.myrk.command({ action: 'snapshot' }).then(() => 'resolved', () => 'rejected'),
        new Promise(resolve => { timeout = setTimeout(() => resolve('no_reply'), 2000) }),
      ])
      clearTimeout(timeout)
      await new Promise(resolve => setTimeout(resolve, 100))
      observe()
      observer.disconnect()
      return { snapshot_reply: snapshotReply, states }
    })()`)
    const final = startup.states.at(-1)
    result.startup_status_observation = startup
    result.startup_failure_visible = ['无法启动办事程序', '办事程序已退出', '连接恢复未完成', '与办事程序通信失败']
      .some(text => final.caption.includes(text))
    result.original_startup_error = final.caption === '无法启动办事程序，请检查开发环境和 Python（执行办事逻辑的程序）路径。'
    result.no_ready_success = startup.snapshot_reply === 'rejected' && final.header === '办事程序不可用' && startup.states.every(state =>
      state.header !== '准备就绪' && state.input_disabled && state.send_disabled)
    await writeFile(join(root, 'startup-rendered-text.txt'), await execute('document.body.innerText'))
    await execute('window.startupEvents=[];window.myrk.subscribe(event=>window.startupEvents.push(event));void 0')
  } else {
    await attach()
    result.service_ready = true
    if (phase === 'save') {
      await until(() => execute('document.querySelector(".topbar .main-status")?.textContent === "准备就绪" && document.querySelector(".composer textarea")?.disabled === false'), 'ready state in the actual new chat')
      result.normal_startup_ready_visible = true
    }
    if (['save','restore','forgotten'].includes(phase)) {
      await command({ action: 'get_connection_storage' })
      await until(async () => (await events()).some(row => row.event === 'connection_storage'), 'real encrypted connection owner')
      await execute('document.querySelector("button[aria-label=打开设置]").click()')
      await until(() => execute('Boolean(document.querySelector("#connection-password"))'), 'password field')
      assert.equal(await execute('document.querySelector("#connection-password").value'), '')
      result.password_not_refilled = true
      if (phase === 'save') {
        await execute('const input=document.querySelector("#connection-password");Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(input,"synthetic-native-input-only-16");input.dispatchEvent(new Event("input",{bubbles:true}));input.dispatchEvent(new Event("change",{bubbles:true}))')
        result.new_input_cleared = await execute('const form=document.querySelector(".settings form");form.requestSubmit();document.querySelector("#connection-password").value === ""')
        assert.equal(result.new_input_cleared, true)
        await until(async () => (await events()).some(row => row.event === 'connection_storage' && row.saved.length === 1 && row.error === null), 'real encrypted save')
      } else if (phase === 'restore') {
        assert.equal((await events()).findLast(row => row.event === 'snapshot').configured, true)
        await execute('Array.from(document.querySelectorAll("button")).find(button=>button.textContent==="忘记此连接密码").click()')
        await until(async () => (await events()).findLast(row => row.event === 'snapshot')?.configured === false, 'forgotten connection not active')
        result.credential_forgotten = true
      } else {
        assert.equal((await events()).findLast(row => row.event === 'snapshot').configured, false)
        assert.equal((await events()).findLast(row => row.event === 'connection_storage').saved.length, 0)
      }
      assert.equal(JSON.stringify(await events()).includes('synthetic-native-input-only-16'), false)
      result.no_secret_events = true
      result.no_automatic_requests = !(await events()).some(row => row.event === 'started')
    } else if (phase === 'invalid') {
      await command({ action: 'configure', key: 'synthetic-rejected-only-16' })
      await command({ action: 'send', prompt: 'Check the explicitly configured synthetic connection with one local native request.' })
      await until(async () => (await events()).some(row => row.event === 'finished'), 'original rejected connection result')
      const finished = (await events()).find(row => row.event === 'finished')
      result.wrong_connection_not_success = finished.turn.status === 'failed' && finished.turn.service_failure === true
      result.connection_error = finished.turn.error
      assert.equal(result.wrong_connection_not_success, true)
      await command({ action: 'verify_recovery', path: finished.path, task_id: finished.turn.task.id })
      await until(async () => (await events()).some(row => row.event === 'recovery'), 'actual failed-task recovery check')
      const target = { ...finished.turn.task.provider_config, name: 'Synthetic explicitly approved new recipient', protocol: 'openai-chat', base_url: 'https://api.openai.com/v1', model: 'gpt-5.4' }
      const preview = async () => {
        const count = (await events()).filter(row => row.event === 'provider_preview').length
        await command({ action: 'preview_provider', path: finished.path, task_id: finished.turn.task.id, provider_config: target })
        await until(async () => (await events()).filter(row => row.event === 'provider_preview').length > count, 'explicit source and receiver preview')
        return (await events()).filter(row => row.event === 'provider_preview').at(-1)
      }
      const declined = await preview()
      await command({ action: 'decide_provider', path: finished.path, change_id: declined.change.id, approved: false })
      const approved = await preview()
      await command({ action: 'decide_provider', path: finished.path, change_id: approved.change.id, approved: true })
      result.recipient_consent = { source: finished.turn.task.provider_config, target, declined_change: declined.change, approved_change: approved.change,
        approved_before_resuming: true, resumed: false }
      const exchange = JSON.parse(await (await import('node:fs/promises')).readFile(join(root,'exchange.json'),'utf8'))
      assert.equal(exchange.exchanges.length,1,'Preview and both explicit decisions must not send source data')
      result.recipient_preview_and_decisions_zero_send = true
    } else {
      await command({ action: 'configure', key: 'synthetic-close-only-16' })
      await command({ action: 'send', prompt: 'Keep this explicitly authorized local task active until I stop it.' })
      await until(async () => (await events()).some(row => row.event === 'text'), 'useful native partial response')
      const started = (await events()).find(row => row.event === 'started')
      const previousWindow = window.id
      window.close()
      await until(() => window.isDestroyed(), 'window actually closed')
      await pause(300)
      Menu.getApplicationMenu().getMenuItemById('myrk-open').click()
      await until(() => BrowserWindow.getAllWindows().length === 1, 'reopened Main window')
      window = BrowserWindow.getAllWindows()[0]
      await until(() => !window.webContents.isLoading(), 'reopened renderer')
      await attach()
      const session = (await events()).find(row => row.event === 'snapshot').conversations.find(row => row.path === started.path)
      assert.equal(session.running, true)
      assert.equal(session.turns[0].task.id, started.task_id)
      assert.equal(session.turns[0].task.requests, 1)
      assert.ok(session.turns[0].answer.includes('进展'))
      assert.notEqual(window.id, previousWindow)
      result.close_continued = true; result.reopened_without_duplicate = true
      result.task_id = started.task_id; result.active_seconds_after_reopen = session.turns[0].task.active_seconds
      await command({ action: 'stop', path: started.path })
      await until(async () => (await events()).some(row => row.event === 'finished'), 'accepted stop')
      const finished = (await events()).find(row => row.event === 'finished')
      assert.equal(finished.turn.status, 'cancelled'); assert.equal(finished.saved, true)
      result.stopped_task_saved = true
    }
    await writeFile(join(root, 'public-events.json'), JSON.stringify(await events(), null, 2))
  }
  result.passed = true; if (phase !== "startup-failure") result.normal_exit = true
} catch (error) { result.error = error.stack }
finally {
  await writeFile(join(root, 'connection-observation.json'), JSON.stringify(result, null, 2))
  const quitItem = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
  if (phase === 'startup-failure') {
    const exitPath = join(root, 'startup-exit-observation.json')
    const quitSource = quitItem ? 'application-menu' : 'app.quit'
    let willQuit = false
    const timer = setTimeout(async () => {
      const publicEvents = execute ? await execute('window.startupEvents').catch(() => []) : []
      if (willQuit) return
      writeFileSync(exitPath, JSON.stringify({ pid: process.pid, observed_at: new Date().toISOString(), quit_requested: true,
        quit_source: quitSource, will_quit: false, external_exit_requested: exitControl.requested, public_events: publicEvents,
        planned_driver_exit: true, reason: 'Normal Main quit did not finish after startup failure; the driver exits after preserving UI evidence.' }), { flag: 'wx' })
      app.exit(0)
    },1500)
    app.once('will-quit', () => {
      willQuit = true
      clearTimeout(timer)
      writeFileSync(exitPath, JSON.stringify({ pid: process.pid, observed_at: new Date().toISOString(), quit_requested: true,
        quit_source: quitSource, will_quit: true, external_exit_requested: exitControl.requested, planned_driver_exit: false }), { flag: 'wx' })
    })
  }
  if (quitItem) quitItem.click(); else app.quit()
}
}
void run()
