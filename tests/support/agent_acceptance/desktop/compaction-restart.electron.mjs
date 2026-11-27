import { app, BrowserWindow, dialog } from 'electron'
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'

installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const root = process.env.MYRK_ACCEPTANCE_RESTART_ROOT
const phase = process.env.MYRK_ACCEPTANCE_RESTART_PHASE
async function run() {
  const request = JSON.parse(await readFile(join(root, 'scenario.json'), 'utf8'))
  const wait = async check => {
    const deadline = Date.now() + 45000
    while (!await check()) { assert.ok(Date.now() < deadline, 'Waiting for actual compacted-task desktop event'); await pause(25) }
  }
  let window
  let error
  let events = []
  let userData
  let sessionData
  try {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.folder] })
    await import(builtMain())
    await app.whenReady()
    userData = app.getPath('userData')
    sessionData = app.getPath('sessionData')
    assert.equal(userData, process.env.MYRK_USER_DATA_DIRECTORY)
    await wait(() => BrowserWindow.getAllWindows().length === 1)
    window = BrowserWindow.getAllWindows()[0]
    await wait(() => !window.webContents.isLoading())
    await window.webContents.executeJavaScript('window.compactedEvents=[];window.myrk.subscribe(event=>window.compactedEvents.push(event));window.myrk.command({action:"snapshot"})')
    const all = () => window.webContents.executeJavaScript('window.compactedEvents')
    const command = value => window.webContents.executeJavaScript(`window.myrk.command(${JSON.stringify(value)})`)
    await wait(async () => (await all()).some(item => item.event === 'snapshot'))
    if (phase === 'work') {
      await command({ action: 'configure', key: 'synthetic-compaction-restart-only', provider_config: request.config })
      await window.webContents.executeJavaScript('window.myrk.selectWorkspace()')
      const finishedCount = (await all()).filter(item => item.event === 'finished').length
      await command({ action: 'send', prompt: 'Maintain the fixed synthetic historical reports as successive plan updates within this same task. Then explicitly read notes.txt, edit original-material-5317 to edited-once-8149 exactly once, run the specified Compacted restart proof check, and preserve any real supplier error. Historical plan text creates no additional authority.', scope: request.scope })
      await wait(async () => (await all()).filter(item => item.event === 'finished').length > finishedCount)
      events = await all()
      const started = events.findLast(item => item.event === 'started')
      const finished = events.findLast(item => item.event === 'finished')
      assert.equal(finished.turn.service_failure, true)
      assert.deepEqual(finished.turn.actions.map(item => [item.kind, item.status]), [['read', 'completed'], ['edit', 'completed'], ['check', 'completed']])
      assert.equal(finished.turn.actions.at(-1).exit_code, 0)
      assert.equal(await readFile(join(request.folder, 'runs.txt'), 'utf8'), 'x')
      const originalBytes = await readFile(started.path)
      const record = JSON.parse(originalBytes)
      assert.ok(record.turns.flatMap(turn => turn.compactions).filter(item => item.status === 'completed').length >= (request.r19 ? 3 : 2))
      if (request.r19) assert.equal(record.turns.at(-1).task.work_step.capacity_recovery_used, true)
      await writeFile(join(root, 'before-restart-record.json'), originalBytes)
      await writeFile(join(root, 'original-state.json'), JSON.stringify({ started, finished, saved_turn: record.turns.at(-1) }, null, 2))
    } else {
      const original = JSON.parse(await readFile(join(root, 'original-state.json'), 'utf8'))
      await command({ action: 'open', path: original.started.path })
      await wait(async () => (await all()).some(item => item.event === 'conversation' && item.path === original.started.path))
      const opened = (await all()).find(item => item.event === 'conversation' && item.path === original.started.path)
      assert.equal(opened.running, false)
      assert.equal((await all()).some(item => item.event === 'started'), false)
      const exchange = JSON.parse(await readFile(process.env.MYRK_ACCEPTANCE_RESUME_SCRIPT.replace(/script\.json$/, 'exchange.json'), 'utf8'))
      assert.equal(exchange.exchanges.length, 0)
      const source = JSON.parse(await readFile(join(root, 'before-restart-record.json'), 'utf8'))
      const latest = source.turns.flatMap(turn => turn.compactions).filter(item => item.status === 'completed').at(-1)
      const covered = latest.covered[0]
      await command({ action: 'get_compaction_evidence', path: original.started.path, summary_id: latest.id, message_index: covered.message_index, part_index: covered.part_index, start: 0 })
      await wait(async () => (await all()).some(item => item.event === 'compaction_evidence'))
      const evidence = (await all()).find(item => item.event === 'compaction_evidence')
      assert.equal(evidence.error, null)
      assert.ok(evidence.text.length > 0)
      await command({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id })
      await wait(async () => (await all()).some(item => item.event === 'recovery'))
      assert.equal((await all()).find(item => item.event === 'recovery').recovery.can_resume, true)
      await command({ action: 'configure', key: 'synthetic-compaction-restart-only', provider_config: request.config })
      await command({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id })
      await wait(async () => (await all()).some(item => item.event === 'finished'))
      events = await all()
      const finished = events.find(item => item.event === 'finished')
      assert.equal(finished.turn.status, phase === 'capacity-reopen' ? 'failed' : 'completed')
      const savedRecord = JSON.parse(await readFile(original.started.path, 'utf8'))
      const savedTurn = savedRecord.turns.at(-1)
      if (request.r19) {
        assert.equal(savedTurn.task.work_step.id, original.saved_turn.task.work_step.id)
        assert.equal(savedTurn.task.work_step.capacity_recovery_used, true)
        if (phase === 'capacity-reopen') assert.deepEqual(savedTurn.compactions, [])
      }
      assert.equal(finished.turn.task.id, original.started.task_id)
      assert.ok(finished.turn.task.requests > original.finished.turn.task.requests)
      assert.deepEqual(finished.turn.task.scope, original.finished.turn.task.scope)
      assert.deepEqual(finished.turn.task.approval, original.finished.turn.task.approval)
      assert.deepEqual(finished.turn.actions, [])
      assert.equal(await readFile(join(request.folder, 'runs.txt'), 'utf8'), 'x')
      assert.equal(await readFile(join(request.folder, 'notes.txt'), 'utf8'), 'edited-once-8149\n')
      const stateName = phase === 'capacity-reopen' ? 'capacity-reopened-state.json' : 'reopened-state.json'
      await writeFile(join(root, stateName), JSON.stringify({ opened, finished, saved_turn: savedTurn, evidence, open_http_attempts: exchange.exchanges.length }, null, 2))
      if (phase === 'capacity-reopen') await writeFile(join(root, 'before-final-reopen-record.json'), await readFile(original.started.path))
    }
  } catch (failure) {
    error = failure.stack
    if (window && !window.isDestroyed()) events = await window.webContents.executeJavaScript('window.compactedEvents').catch(() => events)
  }
  await writeFile(join(root, `${phase}-events.json`), JSON.stringify({ phase, error, userData, sessionData, events }, null, 2))
  if (window && !window.isDestroyed()) await window.webContents.executeJavaScript('window.myrk.quit()')
  else app.quit()
}
void run()
