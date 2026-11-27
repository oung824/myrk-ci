import { app, BrowserWindow, dialog, Menu, screen, shell } from 'electron'
import assert from 'node:assert/strict'
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const exitControl = installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const directory = process.env.MYRK_APPROVAL_ACCEPTANCE_ROOT
const project = join(directory, '项目')
const evidence = join(directory, 'screenshots')
const checks = []
const reveals = []
const rendererErrors = []
const layoutEvidenceEnabled = process.env.MYRK_UI_LAYOUT_EVIDENCE === '1'
const layoutEvidence = []
let window
let selection = project
const waitFor = async (condition, message) => {
  const deadline = Date.now() + 25000
  while (!await condition()) { assert.ok(Date.now() < deadline, message); await pause(25) }
}
const execute = code => Promise.race([window.webContents.executeJavaScript(code), pause(10000).then(() => { throw new Error(`Renderer operation did not return: ${code.slice(0, 120)}`) })])
const progress = stage => appendFile(join(directory, 'progress.jsonl'), JSON.stringify({ stage, at: new Date().toISOString() }) + '\n')
const events = () => execute('window.approvalEvents')
const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
const click = async label => {
  await execute(`(() => { const control = [...document.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === ${JSON.stringify(label)} || button.textContent === ${JSON.stringify(label)}); if (!control || control.disabled) throw new Error('Missing enabled button: ' + ${JSON.stringify(label)}); control.click(); })()`)
  await pause(25)
}
const fill = async (selector, value) => {
  await execute(`(() => { const control = document.querySelector(${JSON.stringify(selector)}); const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype,'value').set.call(control,${JSON.stringify(value)}); control.dispatchEvent(new Event('input',{bubbles:true})); })()`)
  await pause(25)
}
const choose = async (selector, value) => {
  await execute(`(() => { const control = document.querySelector(${JSON.stringify(selector)}); control.value=${JSON.stringify(value)}; control.dispatchEvent(new Event('change',{bubbles:true})); })()`)
  await pause(25)
}
const text = () => execute('document.body.textContent')
const check = (name, detail = {}) => { checks.push({ name, passed: true, ...detail }) }
const capture = async name => {
  await execute('document.fonts.ready')
  await pause(150)
  await writeFile(join(evidence, `${name}.png`), (await window.webContents.capturePage()).toPNG())
}
const captureLayout = async (name, width, height, pageZoom, controls, codeSelector) => {
  const originalBounds = window.getBounds()
  const originalZoom = window.webContents.getZoomFactor()
  try {
    window.setSize(width, height)
    window.webContents.setZoomFactor(pageZoom)
    await execute('document.fonts.ready')
    await execute('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    const reachable = []
    for (const { selector, text: controlText, label } of controls) {
      const observation = await execute(`(async () => {
        const control = [...document.querySelectorAll(${JSON.stringify(selector)})].find(element => ${controlText ? `element.textContent === ${JSON.stringify(controlText)}` : 'true'});
        if (!control) throw new Error('Missing layout control: ' + ${JSON.stringify(label)});
        control.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
        await new Promise(resolve => requestAnimationFrame(resolve));
        const rect = control.getBoundingClientRect();
        const center = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        const target = document.elementFromPoint(center.x, center.y);
        return { label: ${JSON.stringify(label)}, disabled: Boolean(control.disabled),
          rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, center,
          inViewport: center.x >= 0 && center.x < innerWidth && center.y >= 0 && center.y < innerHeight,
          hit: target === control || control.contains(target),
          hitTarget: target ? { tag: target.tagName, label: target.getAttribute('aria-label'), text: target.textContent?.slice(0, 100) } : null };
      })()`)
      assert.equal(observation.disabled, false, `${name}: ${label} is enabled`)
      assert.ok(observation.rect.width > 0 && observation.rect.height > 0, `${name}: ${label} has visible bounds`)
      assert.equal(observation.inViewport, true, `${name}: ${label} scrolls into the viewport`)
      assert.equal(observation.hit, true, `${name}: ${label} is not covered: ${JSON.stringify(observation)}`)
      reachable.push(observation)
    }
    const code = codeSelector ? await execute(`(() => {
      const block = document.querySelector(${JSON.stringify(codeSelector)});
      if (!block) throw new Error('Missing long code block');
      const originalLeft = block.scrollLeft;
      block.scrollLeft = block.scrollWidth;
      const observation = { clientWidth: block.clientWidth, scrollWidth: block.scrollWidth,
        overflowX: getComputedStyle(block).overflowX, scrolledLeft: block.scrollLeft };
      block.scrollLeft = originalLeft;
      return observation;
    })()`) : null
    if (code) {
      assert.ok(code.scrollWidth > code.clientWidth, `${name}: controlled long code exceeds its visible region`)
      assert.ok(['auto', 'scroll'].includes(code.overflowX), `${name}: long code has its own horizontal scroll region`)
      assert.ok(code.scrolledLeft > 0, `${name}: long code can scroll horizontally inside its region`)
    }
    const viewport = await execute(`({ width: innerWidth, height: innerHeight, devicePixelRatio,
      documentWidth: document.documentElement.scrollWidth, documentHeight: document.documentElement.scrollHeight })`)
    const bounds = window.getBounds()
    const observation = { name, screenshot: `${name}.png`, requestedWindowSize: { width, height },
      windowBounds: bounds, contentBounds: window.getContentBounds(), viewport,
      scaleMode: 'page_zoom', pageZoom: window.webContents.getZoomFactor(),
      displayScaleFactor: screen.getDisplayMatching(bounds).scaleFactor, controls: reachable, ...(code ? { code } : {}) }
    assert.equal(bounds.width, width, `${name}: actual window width`)
    assert.equal(bounds.height, height, `${name}: actual window height`)
    assert.ok(Math.abs(observation.pageZoom - pageZoom) < .001, `${name}: actual page zoom`)
    assert.ok(viewport.documentWidth <= viewport.width + 1, `${name}: no document overflow across the window`)
    assert.ok(viewport.documentHeight <= viewport.height + 1, `${name}: no document overflow below the window`)
    await capture(name)
    layoutEvidence.push(observation)
    check(`native layout controls are reachable: ${name}`, { scaleMode: 'page_zoom', width, height, pageZoom })
  } finally {
    window.webContents.setZoomFactor(originalZoom)
    window.setBounds(originalBounds)
    await execute('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
  }
}
const attach = async () => {
  await waitFor(() => BrowserWindow.getAllWindows().length === 1, 'desktop window exists')
  window = BrowserWindow.getAllWindows()[0]
  await waitFor(() => window.webContents.getURL().startsWith('file:') && !window.webContents.isLoading(), 'desktop renderer loaded')
  await progress('renderer-loaded')
  window.webContents.on('console-message', event => { if (event.level === 'error') rendererErrors.push(event.message) })
  await execute(`window.approvalEvents=[]; window.myrk.subscribe(event => window.approvalEvents.push(event)); window.myrk.command({action:'snapshot'})`)
  await waitFor(() => execute(`window.approvalEvents.some(event => event.event === 'snapshot')`), 'current settings received')
  await progress('snapshot-received')
}
const clear = () => execute('window.approvalEvents=[]')
const finished = async () => {
  await waitFor(async () => (await events()).some(event => event.event === 'finished'), 'native task finished')
  const event = (await events()).find(event => event.event === 'finished')
  assert.equal(event.turn.status, 'completed', JSON.stringify(event))
  assert.equal(event.saved, true)
  return event
}
const newTask = async (mode, scenario) => {
  await clear()
  await command({ action: 'new' })
  await waitFor(async () => (await events()).some(event => event.event === 'conversation'), 'new chat acknowledged')
  await waitFor(() => execute(`!document.querySelector('[aria-label="对话内容"]')`), 'new chat visible')
  await click('选择任务文件夹')
  await waitFor(() => execute(`!!document.querySelector('.task-scope')`), 'saved project trust used')
  assert.equal(await execute(`!!document.querySelector('dialog.folder-trust')`), false)
  await choose('[aria-label="新一轮批准方式"]', mode)
  await fill('[aria-label="输入消息"]', `桌面验收：${scenario}`)
  await clear()
  await click('发送消息')
}

async function run() {
  if (exitControl.requested) return
  await mkdir(evidence, { recursive: true })
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selection] })
  shell.showItemInFolder = path => { reveals.push(path) }
  await import(builtMain())
  await progress('main-imported')
  await app.whenReady()
  await attach()
  await progress('attached')
  await click('打开设置')
  await fill('#connection-password', 'synthetic-no-paid-model')
  await click('保存设置')
  await waitFor(() => execute(`!document.querySelector('dialog.settings-dialog')`), 'synthetic connection configured')
  if (layoutEvidenceEnabled) {
    assert.equal(await execute(`!!document.querySelector('[aria-label="对话内容"]')`), false)
    await captureLayout('ui-new-conversation-default', 1180, 830, 1, [
      { selector: '[aria-label="输入消息"]', label: '新会话输入消息' },
      { selector: '[aria-label="选择任务文件夹"]', label: '新会话任务文件夹' },
    ])
  }

  await fill('[aria-label="输入消息"]', '保留中文草稿')
  for (const exit of ['取消文件夹确认', '关闭文件夹确认', 'Escape']) {
    await click('选择任务文件夹')
    await waitFor(() => execute(`!!document.querySelector('dialog.folder-trust[open]')`), 'one trust modal opens')
    assert.equal(await execute(`document.querySelectorAll('dialog[open]').length`), 1)
    assert.equal(await execute(`document.querySelectorAll('dialog.folder-trust input[type="checkbox"]').length`), 0)
    if (exit === 'Escape') {
      window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'ESC' })
      window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'ESC' })
    } else await click(exit)
    await waitFor(() => execute(`!document.querySelector('dialog.folder-trust')`), 'trust cancellation closes modal')
    assert.equal(await execute(`document.querySelector('[aria-label="输入消息"]').value`), '保留中文草稿')
    assert.equal(await execute(`!!document.querySelector('.task-scope')`), false)
    check(`folder ${exit} preserves draft and selection`)
  }
  await click('选择任务文件夹')
  await waitFor(() => execute(`!!document.querySelector('dialog.folder-trust[open]')`), 'project trust opens')
  await capture('01-project-trust')
  const trustHeight = await execute(`document.querySelector('dialog.folder-trust').getBoundingClientRect().height`)
  assert.ok(trustHeight <= 380, `Compact trust confirmation is ${trustHeight}px high`)
  assert.equal(await execute(`document.querySelector('.trust-details').open`), false)
  await execute(`document.querySelector('.trust-details summary').click()`)
  assert.equal(await execute(`document.querySelector('.trust-details').open`), true)
  await capture('01b-project-details')
  await execute(`document.querySelector('.trust-details summary').click()`)
  check('compact trust confirmation keeps complete details expandable', { trustHeight })
  await click('信任文件夹')
  await waitFor(() => execute(`!!document.querySelector('.task-scope')`), 'project trust saved before selection')
  assert.ok((await events()).some(event => event.event === 'project_trust' && event.saved && event.root === project))
  check('project trust saved independently')
  selection = join(directory, '替换项目')
  for (const exit of ['取消文件夹确认', '关闭文件夹确认', 'Escape']) {
    await click('选择任务文件夹')
    await waitFor(() => execute(`!!document.querySelector('dialog.folder-trust[open]')`), 'replacement folder opened')
    if (exit === 'Escape') { window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'ESC' }); window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'ESC' }) }
    else await click(exit)
    await waitFor(() => execute(`!document.querySelector('dialog.folder-trust')`), 'replacement discarded')
    assert.ok((await text()).includes('项目 ·') || await execute(`document.querySelector('.scope-summary strong').textContent === '项目'`))
    assert.equal(await execute(`document.querySelector('[aria-label="输入消息"]').value`), '保留中文草稿')
    check(`replacement ${exit} preserves original project`)
  }
  selection = project
  await click('打开设置')
  await click('工作偏好')
  await choose('#approval-mode', 'full_access')
  await click('关闭设置')
  await waitFor(() => execute(`!document.querySelector('dialog.settings-dialog')`), 'unsaved preferences discarded')
  await click('打开设置')
  await click('工作偏好')
  assert.equal(await execute(`document.querySelector('#approval-mode').value`), 'request_approval')
  await choose('#approval-mode', 'auto_review')
  await click('保存默认批准方式')
  await waitFor(() => execute(`!document.querySelector('dialog.settings-dialog')`), 'default saved')
  await click('修改会话批准方式')
  await choose('#conversation-approval-mode', 'full_access')
  await click('取消会话批准方式')
  await click('修改会话批准方式')
  assert.equal(await execute(`document.querySelector('#conversation-approval-mode').value`), 'request_approval')
  await choose('#conversation-approval-mode', 'auto_review')
  await click('保存会话批准方式')
  await waitFor(() => execute(`!document.querySelector('dialog.conversation-approval-dialog')`), 'conversation saved')
  await capture('02-saved-settings')
  check('settings cancel and explicit save remain separate')
  if (layoutEvidenceEnabled) {
    await click('打开设置')
    const controls = [
      { selector: '#connection-password', label: '连接密码' },
      { selector: '#service-model', label: '回答型号' },
      { selector: 'dialog.settings-dialog form > footer button[type="button"]', label: '设置底部关闭或取消' },
      { selector: 'dialog.settings-dialog [aria-label="保存设置"]', label: '设置底部保存' },
    ]
    await captureLayout('ui-settings-default', 1180, 830, 1, controls)
    await captureLayout('ui-settings-minimum', 680, 540, 1, controls)
    await captureLayout('ui-settings-minimum-page-zoom-125', 680, 540, 1.25, controls)
    await click('关闭设置')
    await waitFor(() => execute(`!document.querySelector('dialog.settings-dialog')`), 'layout inspection leaves settings closed')
  }

  await newTask('request_approval', 'inside')
  const inside = await finished()
  assert.equal(await readFile(join(project, '中文产物.md'), 'utf8'), '真实桌面结果：inside\n')
  assert.equal(inside.turn.actions[0].sandbox_type, 'windowsMxc')
  if (layoutEvidenceEnabled) {
    assert.ok(inside.turn.answer.includes('受控离线界面验收'))
    await waitFor(() => execute(`!!document.querySelector('.answer-body pre')`), 'controlled long answer has rendered its code block')
    const controls = [
      { selector: '[aria-label="输入消息"]', label: '长回答后的输入消息' },
      { selector: '[aria-label="复制回答"]', label: '复制长回答' },
      { selector: '.turn-records > summary', label: '长回答的本轮记录展开入口' },
      { selector: '.answer-body pre', label: '长代码内部滚动区域' },
    ]
    await captureLayout('ui-long-answer-default', 1180, 830, 1, controls, '.answer-body pre')
    await captureLayout('ui-long-answer-minimum-page-zoom-125', 680, 540, 1.25, controls, '.answer-body pre')
  }
  const location = join(project, '中文产物.md')
  await execute(`[...document.querySelectorAll('a')].find(link => link.getAttribute('aria-label') === ${JSON.stringify(`在文件夹中查看：${location}`)}).click()`)
  await waitFor(() => reveals.length === 1, 'actual file location revealed')
  assert.equal(reveals[0], location)
  const refused = await execute(`window.myrk.revealFile(${JSON.stringify(join(directory, 'not-an-action.exe'))}).then(()=>false,()=>true)`)
  assert.equal(refused, true)
  await capture('03-chinese-file-location')
  check('Chinese native file and clickable observed file location', { actualFile: location })

  await newTask('request_approval', 'human_allow')
  await waitFor(() => execute(`!![...document.querySelectorAll('button')].find(button => button.textContent === '批准这次动作')`), 'human approval is pending')
  await capture('04-human-pending')
  if (layoutEvidenceEnabled) {
    const controls = [
      { selector: '[aria-label="输入消息"]', label: '输入消息' },
      { selector: '[aria-label="待处理事项"] button', text: '批准这次动作', label: '待处理事项中的批准动作' },
    ]
    await captureLayout('ui-pending-default', 1180, 830, 1, controls)
    await captureLayout('ui-pending-minimum', 680, 540, 1, controls)
    await captureLayout('ui-pending-default-page-zoom-125', 1180, 830, 1.25, controls)
    await captureLayout('ui-pending-minimum-page-zoom-125', 680, 540, 1.25, controls)
  }
  await click('批准这次动作')
  const humanAllowed = await finished()
  assert.equal(await readFile(join(directory, 'human_allow.txt'), 'utf8'), '真实桌面结果：human_allow\n')
  assert.equal(humanAllowed.turn.actions[0].approval_basis, 'user')
  check('human allowed outside write has real native effect')
  await newTask('request_approval', 'human_deny')
  await waitFor(() => execute(`!![...document.querySelectorAll('button')].find(button => button.textContent === '拒绝这次动作')`), 'human denial is possible')
  await click('拒绝这次动作')
  const humanDenied = await finished()
  assert.equal(humanDenied.turn.actions[0].status, 'denied')
  assert.equal(await readFile(join(directory, 'human_deny.txt')).then(() => true, () => false), false)
  check('human denial has no file effect')

  await newTask('auto_review', 'auto_allow')
  const autoAllowed = await finished()
  assert.equal(autoAllowed.turn.actions[0].review.status, 'allowed')
  assert.equal(autoAllowed.turn.actions[0].review.attempts, 2)
  assert.equal(await readFile(join(directory, 'auto_allow.txt'), 'utf8'), '真实桌面结果：auto_allow\n')
  assert.ok(autoAllowed.turn.task.review_requests === 2 && autoAllowed.turn.task.requests >= 4)
  await capture('05-independent-allowed')
  check('independent read-only inspection and allow have real native effect')
  await newTask('auto_review', 'auto_deny')
  const autoDenied = await finished()
  assert.equal(autoDenied.turn.actions[0].review.status, 'denied')
  assert.equal(await readFile(join(directory, 'auto_deny.txt')).then(() => true, () => false), false)
  await capture('06-independent-denied')
  check('independent denial does not create file')
  await clear()
  await click('授权此动作再审核一次')
  const override = await finished()
  assert.equal(override.turn.task.id, autoDenied.turn.task.id)
  assert.equal(override.turn.actions[0].review.user_override, true)
  assert.equal(override.turn.actions[0].review.status, 'allowed')
  assert.equal(await readFile(join(directory, 'auto_deny.txt'), 'utf8'), '真实桌面结果：auto_deny\n')
  check('one exact denial receives one new independent review')

  await newTask('full_access', 'full')
  const full = await finished()
  assert.equal(full.turn.actions[0].approval_basis, 'task_scope')
  assert.equal(full.turn.actions[0].sandbox_type, 'none')
  assert.equal(full.turn.task.review_requests, 0)
  assert.equal(await readFile(join(directory, 'full.txt'), 'utf8'), '真实桌面结果：full\n')
  await capture('07-full-access')
  check('full access outside write uses no inner sandbox or ordinary review')

  await newTask('request_approval', 'human_stop')
  await waitFor(() => execute(`!![...document.querySelectorAll('button')].find(button => button.textContent === '批准这次动作')`), 'stoppable human approval pending')
  const stale = (await events()).findLast(event => event.event === 'action' && event.action.status === 'awaiting_approval')
  await click('停止回答')
  await waitFor(async () => (await events()).some(event => event.event === 'finished'), 'stopped pending approval settled')
  await command({ action: 'approve_action', path: stale.path, task_id: stale.action.task_id, action_id: stale.action.id, approval_id: stale.action.approval_id, approved: true })
  await waitFor(async () => (await events()).some(event => event.event === 'error'), 'late allow is rejected')
  assert.equal(await readFile(join(directory, 'human_stop.txt')).then(() => true, () => false), false)
  check('stop invalidates late human allow')
  await newTask('auto_review', 'auto_stop')
  await waitFor(() => execute(`document.body.textContent.includes('独立助理正在审核')`), 'independent review starts')
  await click('停止回答')
  await waitFor(async () => (await events()).some(event => event.event === 'finished'), 'independent review stops')
  assert.equal(await readFile(join(directory, 'auto_stop.txt')).then(() => true, () => false), false)
  check('stop cancels active independent review without file effect')

  await newTask('request_approval', 'command_stop')
  await waitFor(async () => readFile(join(project, 'stop-child.pid'), 'utf8').then(() => true, () => false), 'real native command created a child')
  await appendFile(join(directory, 'child-pids.jsonl'), JSON.stringify({ pid: Number(await readFile(join(project, 'stop-child.pid'), 'utf8')) }) + '\n')
  await click('停止回答')
  await waitFor(async () => (await events()).some(event => event.event === 'finished'), 'native command stop settled')
  const stopped = (await events()).find(event => event.event === 'finished')
  assert.equal(stopped.turn.actions[0].cleanup_complete, true)
  await capture('08-command-stop')
  check('stop confirms real native command and descendants ended')

  await newTask('request_approval', 'inside_reopen')
  await waitFor(() => execute(`!![...document.querySelectorAll('button')].find(button => button.textContent === '批准这次动作')`), 'background pending write captured')
  const background = (await events()).find(event => event.event === 'started')
  window.close()
  await waitFor(() => window.isDestroyed(), 'closing renderer keeps core alive')
  Menu.getApplicationMenu().getMenuItemById('myrk-open').click()
  await attach()
  assert.ok((await events()).find(event => event.event === 'snapshot').conversations.some(item => item.path === background.path && item.running))
  assert.ok((await events()).find(event => event.event === 'snapshot').trusted_projects.includes(project))
  await click('批准这次动作')
  const reopened = await finished()
  assert.equal(await readFile(join(directory, 'inside_reopen.txt'), 'utf8'), '真实桌面结果：inside_reopen\n')
  assert.equal(reopened.turn.task.id, background.task.id)
  await capture('09-reopened-real-work')
  check('window close and reopen preserve live work and project trust')
  assert.deepEqual(rendererErrors, [])
  await writeFile(join(directory, 'result.json'), JSON.stringify({ allPassed: true, checks, reveals, rendererErrors, actualModes: ['request_approval', 'auto_review', 'full_access'], controlledModels: true }, null, 2))
  await writeFile(join(directory, 'last-events.json'), JSON.stringify(await events(), null, 2))
  if (layoutEvidenceEnabled) await writeFile(join(directory, 'ui-layout.json'), JSON.stringify({
    scaleMode: 'page_zoom', note: '125% is webContents page zoom; displayScaleFactor records the existing system display scale without changing it.',
    captures: layoutEvidence,
  }, null, 2))
  await execute('window.myrk.quit()')
}

run().catch(async error => {
  await writeFile(join(directory, 'failure.json'), JSON.stringify({ error: String(error), stack: error.stack, checks, rendererErrors, events: window && !window.isDestroyed() ? await events().catch(() => []) : [] }, null, 2))
  console.error(error)
  app.quit()
})
