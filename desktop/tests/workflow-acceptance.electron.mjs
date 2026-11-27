import { app, BrowserWindow, dialog, nativeImage, shell } from 'electron'
import assert from 'node:assert/strict'
import { access, copyFile, mkdir, mkdtemp, readFile, readdir, unlink, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const exitControl = installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const directory = process.env.MYRK_WORKFLOW_CASE
const task = join(directory, 'task')
const mode = process.env.MYRK_WORKFLOW_MODE
const phase = process.env.MYRK_WORKFLOW_PHASE ?? 'run'
const outputPrefix = phase === 'run' ? '' : 'reopen-'
const prefix = mode === 'programming' ? '编程' : '资料'
const screenshots = join(directory, `${outputPrefix}screenshots`)
const rendererErrors = []
const checks = []
const reveals = []
const layout = []
let window
const execute = code => window.webContents.executeJavaScript(code)
const events = () => execute('window.workflowEvents')
const body = () => execute('document.body.innerText')
const waitFor = async (predicate, label, timeout = 25000) => {
  const deadline = Date.now() + timeout
  while (!await predicate()) { assert.ok(Date.now() < deadline, `${label}: ${await body()}`); await pause(30) }
}
const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
const check = (name, detail = {}) => checks.push({ name, passed: true, ...detail })
const click = label => execute(`(() => { const button = [...document.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === ${JSON.stringify(label)} || button.textContent === ${JSON.stringify(label)}); if (!button || button.disabled) throw new Error('Missing enabled button: ' + ${JSON.stringify(label)}); button.click(); })()`)
const fill = (selector, value) => execute(`(() => { const control=document.querySelector(${JSON.stringify(selector)}); if (!control) throw new Error('Missing input'); const prototype=control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype,'value').set.call(control,${JSON.stringify(value)}); control.dispatchEvent(new Event('input',{bubbles:true})); })()`)
const choose = (selector, value) => execute(`(() => { const control=document.querySelector(${JSON.stringify(selector)}); control.value=${JSON.stringify(value)}; control.dispatchEvent(new Event('change',{bubbles:true})); })()`)
const capture = async name => {
  await execute('document.fonts.ready')
  await execute('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
  await writeFile(join(screenshots, `${name}.png`), (await window.webContents.capturePage()).toPNG())
  layout.push({ screenshot: `${name}.png`, windowBounds: window.getBounds(), contentBounds: window.getContentBounds(), pageZoom: window.webContents.getZoomFactor(), ...await execute(`(() => { const bounds=selector=>{const element=document.querySelector(selector);if(!element)return null;const rect=element.getBoundingClientRect();return {x:rect.x,y:rect.y,width:rect.width,height:rect.height,visible:getComputedStyle(element).visibility!=='hidden' && getComputedStyle(element).display!=='none'};};return {viewport:{width:innerWidth,height:innerHeight},conversation:bounds('.conversation'),composer:bounds('.composer-region'),files:bounds('.conversation-files')};})()`) })
}
const finishedCount = async () => (await events()).filter(event => event.event === 'finished').length
async function send(prompt) {
  const count = await finishedCount()
  await fill('[aria-label="输入消息"]', prompt)
  await click('发送消息')
  return count
}
async function finishedAfter(count) {
  await waitFor(async () => await finishedCount() > count, 'task has finished')
  return (await events()).filter(event => event.event === 'finished')[count]
}
async function attach() {
  await waitFor(async () => {
    window = BrowserWindow.getAllWindows()[0]
    return window && !window.webContents.isLoading()
  }, 'production window loaded')
  assert.ok(window.webContents.getURL().startsWith('file:'), 'the actual built renderer is loaded')
  window.webContents.on('console-message', details => { if (details.level === 'error') rendererErrors.push(details.message) })
  await execute('window.workflowEvents=[]; window.myrk.subscribe(event=>window.workflowEvents.push(event)); window.myrk.command({action:"snapshot"}); true')
  await waitFor(() => execute('window.workflowEvents.some(event=>event.event === "snapshot")'), 'current service snapshot received')
}
async function selectArtifact(finished, name) {
  if (!await execute('!!document.querySelector(".artifact-workspace")')) await click('查看会话成果')
  if (await execute('!!document.querySelector("[aria-label=\\"切换会话成果\\"]")')) {
    await choose('[aria-label="切换会话成果"]', finished.turn.artifacts.find(artifact => artifact.name === name).id)
  } else await click(`预览 ${name}`)
  await execute('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
  await waitFor(() => execute('!!document.querySelector(".artifact-preview .preview-state") || !!document.querySelector(".artifact-preview [role=alert]") || !!document.querySelector(".artifact-preview [role=status]") && !document.querySelector(".artifact-preview").innerText.includes("正在读取")'), `preview of ${name} opens`)
}
async function sharedControls() {
  const allowedCount = await send(`${prefix}批准验收`)
  await waitFor(() => execute('[...document.querySelectorAll("button")].some(button=>button.textContent === "批准这次动作")'), 'one exact outside write awaits approval')
  const awaiting = (await events()).findLast(event => event.event === 'action' && event.action.status === 'awaiting_approval')
  assert.ok(awaiting.action.target.includes('outside-allowed.txt'))
  await click('批准这次动作')
  const allowed = await finishedAfter(allowedCount)
  assert.equal(allowed.turn.status, 'completed')
  assert.equal(allowed.turn.actions.find(action => action.target.includes('outside-allowed.txt')).approval_basis, 'user')
  assert.equal(await readFile(join(directory, 'outside-allowed.txt'), 'utf8'), '只对这次确切动作批准\n')
  check('approval executes only the exact pending real file write')
  const deniedCount = await send(`${prefix}拒绝验收`)
  await waitFor(() => execute('[...document.querySelectorAll("button")].some(button=>button.textContent === "拒绝这次动作")'), 'outside write awaits a real decision')
  const pending = (await events()).findLast(event => event.event === 'action' && event.action.status === 'awaiting_approval')
  assert.ok(pending.action.target.includes('outside.txt'))
  await capture(`${mode}-pending-approval`)
  await click('拒绝这次动作')
  const denied = await finishedAfter(deniedCount)
  assert.equal(denied.turn.actions.find(action => action.tool_call_id === 'outside')?.status ?? denied.turn.actions.find(action => action.status === 'denied')?.status, 'denied')
  assert.equal(await access(join(directory, 'outside.txt')).then(() => true, () => false), false)
  const discussion = await finishedAfter(await send(`${prefix}讨论验收`))
  assert.equal(discussion.turn.status, 'completed')
  assert.match(discussion.turn.answer, /后续讨论/)
  check('real approval can be refused without a write and discussion continues')

  const stopCount = await send(`${prefix}停止验收`)
  await waitFor(() => execute('document.querySelector(".answer:last-of-type") !== null && document.body.innerText.includes("可停止的真实流式回答")'), 'partial answer visible before stopping')
  await click('停止回答')
  const stopped = await finishedAfter(stopCount)
  assert.equal(stopped.turn.status, 'cancelled')
  assert.equal(stopped.saved, true)
  assert.match(stopped.turn.answer, /可停止/)
  assert.ok((await body()).includes('已停止'))
  check('stop waits for the real backend and keeps its partial answer saved')

  const failed = await finishedAfter(await send(`${prefix}故障恢复验收`))
  assert.equal(failed.turn.status, 'failed')
  assert.equal(failed.turn.service_failure, true)
  assert.match(await body(), /controlled workflow service outage|503/)
  await waitFor(() => execute('[...document.querySelectorAll("button")].some(button=>button.textContent === "继续原工作" && !button.disabled)'), 'failed work can be explicitly resumed')
  const before = await finishedCount()
  await click('继续原工作')
  const recovered = await finishedAfter(before)
  assert.equal(recovered.turn.status, 'completed')
  assert.equal(recovered.turn.task.id, failed.turn.task.id)
  assert.match(recovered.turn.answer, /原工作已恢复/)
  check('a real offline service error stays visible and an explicit resume keeps the task identity')
}
async function reopen(owner, title, artifactCount) {
  await click('新建会话')
  await waitFor(() => execute('!document.querySelector("[aria-label=\\"对话内容\\"]")'), 'new conversation is blank')
  await click('查看会话成果')
  assert.match(await body(), /还没有|尚未|没有已交付/)
  await click('返回聊天')
  await fill('[aria-label="搜索会话标题"]', title)
  await click(`打开会话：${title}`)
  await waitFor(() => execute('!!document.querySelector("[aria-label=\\"对话内容\\"]")'), 'saved conversation reopened from history')
  const opened = (await events()).findLast(event => event.event === 'conversation' && event.path === owner)
  assert.ok(opened)
  assert.equal(opened.turns.flatMap(turn => turn.artifacts ?? []).length, artifactCount)
  assert.equal(opened.running, false)
  check('a clean conversation excludes these files and history reopens their saved ownership')
}
async function narrowReading(pageZoom = 1) {
  window.setSize(680, 540)
  window.webContents.setZoomFactor(pageZoom)
  await execute('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
  const contentBounds = window.getContentBounds()
  await waitFor(() => execute(`Math.abs(innerWidth-${contentBounds.width / pageZoom}) < 1 && Math.abs(innerHeight-${contentBounds.height / pageZoom}) < 1`), 'the native window and page zoom settle to their actual viewport')
  const geometry = await execute(`(() => { const chat=document.querySelector('.conversation-pane'); const files=document.querySelector('.conversation-files'); const visible=element=>!!element && element.getBoundingClientRect().width>0 && getComputedStyle(element).display!=='none' && getComputedStyle(element).visibility!=='hidden'; return {width:innerWidth,documentWidth:document.documentElement.scrollWidth,chatVisible:visible(chat),filesVisible:visible(files)};})()`)
  assert.ok(geometry.documentWidth <= geometry.width + 1)
  assert.equal(geometry.filesVisible, true)
  assert.equal(geometry.chatVisible, false)
  if (mode !== 'programming') {
    await waitFor(async () => await pageReady() && await execute(`(() => {const area=document.querySelector('.document-pages');const style=getComputedStyle(area);return document.querySelector('.document-preview canvas').getBoundingClientRect().width <= area.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)+1;})()`), 'the resized document page finishes rendering at its fitted width')
    const readingArea = await execute(`(() => {const area=document.querySelector('.document-pages');const rect=area.getBoundingClientRect();const style=getComputedStyle(area);return {height:rect.height,contentHeight:area.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom),viewportHeight:innerHeight,canvasWidth:document.querySelector('.document-preview canvas').getBoundingClientRect().width};})()`)
    assert.ok(readingArea.contentHeight >= readingArea.viewportHeight / 2, `a narrow document keeps at least half the visible window for its actual page content: ${JSON.stringify(readingArea)}`)
    check('the narrow document page content occupies at least half the visible window', readingArea)
    for (const label of ['上一页', '下一页', '放大', '缩小']) {
      const reachable = await execute(`(() => { const control=document.querySelector('[aria-label="${label}"]'); const rect=control.getBoundingClientRect(); const x=rect.left+rect.width/2,y=rect.top+rect.height/2;const hit=document.elementFromPoint(x,y); return rect.width>0 && rect.height>0 && x>=0 && x<innerWidth && y>=0 && y<innerHeight && (hit===control || control.contains(hit)); })()`)
      assert.equal(reachable, true, `narrow document toolbar control is reachable: ${label}`)
    }
  }
  await capture(`${mode}-narrow-preview`)
  await click('返回聊天')
  await waitFor(() => execute('!!document.querySelector("[aria-label=\\"输入消息\\"]") && !document.querySelector(".artifact-workspace")'), 'narrow preview returns to chat')
  assert.equal(await execute('document.activeElement.getAttribute("aria-label")'), '查看会话成果', 'closing the narrow reading panel restores the now-visible file button focus')
  await execute('document.querySelector("[aria-label=\\"输入消息\\"]").focus()')
  for (const keyCode of ['LEFT', 'RIGHT', 'UP', 'DOWN']) {
    window.webContents.sendInputEvent({ type: 'keyDown', keyCode })
    window.webContents.sendInputEvent({ type: 'keyUp', keyCode })
  }
  assert.equal(await execute('document.activeElement.getAttribute("aria-label")'), '输入消息')
  assert.equal(await execute('!!document.querySelector(".artifact-workspace")'), false)
  await capture(`${mode}-narrow-chat`)
  const chatReading = await execute(`(() => {const area=document.querySelector('.conversation');const style=getComputedStyle(area);return {height:area.getBoundingClientRect().height,contentHeight:area.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom),paneHeight:document.querySelector('.conversation-pane').clientHeight,composerHeight:document.querySelector('.composer-region').getBoundingClientRect().height};})()`)
  assert.ok(chatReading.contentHeight >= chatReading.composerHeight, `conversation-first narrow chat gives readable content at least as much space as composer controls: ${JSON.stringify(chatReading)}`)
  check('returning to narrow chat gives readable content at least as much space as composer controls', chatReading)
  if (mode === 'layout') {
    const draft = '窄窗阅读时保留这段未发送的草稿。'
    await fill('[aria-label="输入消息"]', draft)
    await execute(`([...document.querySelectorAll('.answer-body p')].find(paragraph=>paragraph.textContent.includes('交付后的阅读进度 20'))).scrollIntoView({block:'start',behavior:'instant'})`)
    await pause(150)
    const readingTop = await execute('document.querySelector(".conversation").scrollTop')
    await capture('layout-narrow-reading-middle')
    await click('查看会话成果')
    await click('预览 分页资料.pdf')
    await documentReady(1)
    await click('返回聊天')
    assert.equal(await execute('document.querySelector("[aria-label=\\"输入消息\\"]").value'), draft)
    assert.ok(Math.abs(await execute('document.querySelector(".conversation").scrollTop') - readingTop) <= 1, 'opening and closing the narrow document preserves the conversation reading position')
    const controls = [
      '[aria-label="任务文件夹详情"] > summary', '[aria-label="选择文本资料"]', '[aria-label="选择任务文件夹"]',
      '[aria-label="选择模型"]', '[aria-label="选择推理强度"]', '[aria-label="新一轮批准方式"]',
      '[aria-label="发送消息"]', '.pending-work button', '.follow-latest',
    ]
    for (const selector of controls) {
      const reachable = await execute(`(() => {const control=document.querySelector(${JSON.stringify(selector)});if(!control || control.disabled)return false;control.focus();control.scrollIntoView({block:'nearest',behavior:'instant'});const rect=control.getBoundingClientRect();const x=rect.left+rect.width/2,y=rect.top+rect.height/2;const hit=document.elementFromPoint(x,y);return x>=0 && x<innerWidth && y>=0 && y<innerHeight && (hit===control || control.contains(hit));})()`)
      assert.equal(reachable, true, `narrow chat control remains reachable by scrolling and focus: ${selector}`)
    }
    for (const label of ['选择模型', '选择推理强度']) {
      await click(label)
      assert.equal(await execute('!!document.querySelector(".model-controls-popup")'), true)
      window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'ESC' })
      window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'ESC' })
      await waitFor(() => execute('!document.querySelector(".model-controls-popup")'), `${label} closes with Escape`)
    }
    await execute('document.querySelector("[aria-label=\\"输入消息\\"]").focus()')
    for (const keyCode of ['LEFT', 'RIGHT', 'UP', 'DOWN']) {
      window.webContents.sendInputEvent({ type: 'keyDown', keyCode })
      window.webContents.sendInputEvent({ type: 'keyUp', keyCode })
    }
    assert.equal(await execute('document.activeElement.getAttribute("aria-label")'), '输入消息')
    assert.equal(await execute('document.querySelector("[aria-label=\\"输入消息\\"]").value'), draft)
    assert.ok(Math.abs(await execute('document.querySelector(".conversation").scrollTop') - readingTop) <= 1, 'using narrow composer controls preserves the conversation reading position')
    await capture('layout-narrow-chat-draft')
    check('narrow chat preserves its draft and reading position while all composer controls, model popups and arrow keys remain usable', { readingTop, controls })
  }
  window.webContents.setZoomFactor(1)
  window.setSize(1180, 830)
  check('a narrow window shows one reading panel and arrows preserve the composer focus', geometry)
}
async function runReadingLayout() {
  const completed = await finishedAfter(await send('资料完整验收'))
  assert.equal(completed.turn.status, 'completed')
  await click('查看会话成果')
  await selectArtifact(completed, '分页资料.pdf')
  await documentReady(1)
  await narrowReading(1.25)
}
async function runProgramming() {
  const original = await readFile(join(task, 'calculator.py'), 'utf8')
  await execute('document.querySelector("[aria-label=\\"任务文件夹详情\\"] > summary").click()')
  await execute('document.querySelector(".scope-check > summary").click()')
  await fill('[aria-label="指定检查程序与参数"]', JSON.stringify([process.env.MYRK_PYTHON, join(task, 'check.py')]))
  await execute('document.querySelector(".scope-check > summary").click();document.querySelector("[aria-label=\\"任务文件夹详情\\"] > summary").click()')
  await send('编程完整验收')
  await waitFor(() => execute('document.body.innerText.includes("正在核对程序")'), 'the real streaming answer is visible')
  await fill('[aria-label="输入消息"]', '补充：回答保留核对结果')
  await click('发送补充')
  await fill('[aria-label="输入消息"]', '编程排队验收')
  await click('排队新工作')
  await waitFor(async () => await finishedCount() === 2, 'the original work and queued work completed')
  const completed = (await events()).find(event => event.event === 'finished')
  assert.equal(completed.turn.status, 'completed', JSON.stringify(completed))
  assert.equal(completed.turn.artifacts.length, 2)
  assert.equal(completed.turn.artifacts.find(item => item.name === 'calculator.py').change_kind, 'modified')
  assert.ok(completed.turn.artifacts.find(item => item.name === '分工说明.md').delegation_id)
  assert.equal(completed.turn.delegations[0].verification_status, 'passed')
  assert.equal(completed.turn.checks_passed, true)
  assert.ok((await events()).some(event => event.event === 'supplement' && event.state === 'applied'))
  assert.equal((await events()).filter(event => event.event === 'finished')[1].turn.status, 'completed')
  assert.match(await readFile(join(task, 'calculator.py'), 'utf8'), /return left \+ right/)
  assert.ok(!(await body()).includes('程序记录的事实'), 'technical facts start collapsed in actual visible text')
  await execute('document.querySelector(".turn-records > summary").click()')
  assert.ok((await body()).includes('程序记录的事实'))
  await execute('document.querySelector(".turn-records > summary").click()')
  await capture('programming-conversation')
  check('real edit, delegated explicit delivery, main verification and actual check complete; supplement and queue are honored')
  check('the answer is visible while complete program facts become visible only after expanding records')
  await click('查看会话成果')
  await selectArtifact(completed, 'calculator.py')
  const expected = await readFile(join(task, 'calculator.py'), 'utf8')
  assert.equal(await execute('document.querySelector("[aria-label=\\"成果全文\\"]").textContent'), expected)
  await click('修改对照')
  assert.equal(await execute('document.querySelector("[aria-label=\\"修改前\\"]").textContent'), original)
  assert.equal(await execute('document.querySelector("[aria-label=\\"修改后\\"]").textContent'), expected)
  await capture('programming-reliable-diff')
  await click('全文')
  await writeFile(join(task, 'calculator.py'), expected + '# 外部变化\n', 'utf8')
  assert.equal(await execute('document.querySelector("[aria-label=\\"成果全文\\"]").textContent'), expected)
  await click('刷新预览')
  await waitFor(() => execute('document.querySelector("[aria-label=\\"成果全文\\"]")?.textContent.includes("外部变化")'), 'refresh re-reads the changed file')
  assert.match(await body(), /交付后已有变化/)
  assert.equal(await execute('[...document.querySelectorAll("button")].some(button=>button.textContent === "修改对照")'), false)
  await click('在文件夹中定位成果')
  await waitFor(() => reveals.length > 0, 'native file location is revealed')
  assert.equal(reveals.at(-1), join(task, 'calculator.py'))
  check('full code, reliable action diff, stable snapshot, changed history fallback and native location are real')
  await narrowReading()
  await sharedControls()
  await reopen(completed.path, '编程完整验收', 2)
  await click('查看会话成果')
  await selectArtifact(completed, '分工说明.md')
  assert.match(await body(), /独立核对加法边界/)
  await unlink(join(task, '分工说明.md'))
  assert.match(await body(), /独立核对加法边界/)
  await click('刷新预览')
  await waitFor(() => execute('document.querySelector(".artifact-preview").innerText.includes("不存在") || document.querySelector(".artifact-preview").innerText.includes("找不到") || document.querySelector(".artifact-preview").innerText.includes("丢失")'), 'a missing file is honestly reported')
  await capture('programming-missing-after-refresh')
  check('a reopened delegated Markdown delivery remains readable and refresh reports its real removal')
}
async function runDocuments() {
  await send('资料完整验收')
  await waitFor(() => execute('document.body.innerText.includes("正在处理合成资料")'), 'document work is actually streaming')
  await fill('[aria-label="输入消息"]', '补充：保留原始输入')
  await click('发送补充')
  await fill('[aria-label="输入消息"]', '资料排队验收')
  await click('排队新工作')
  await waitFor(() => execute('!!document.querySelector("[aria-label=\\"放大图片 图形资料.png\\"]")'), 'the real delivered image appears inline')
  await overlayWhileStreaming(true)
  await waitFor(async () => await finishedCount() === 2, 'document delivery and queued task complete')
  const completed = (await events()).find(event => event.event === 'finished')
  assert.equal(completed.turn.status, 'completed', JSON.stringify(completed))
  assert.equal(completed.turn.artifacts.length, 6)
  assert.deepEqual(completed.turn.artifacts.map(artifact => artifact.name).sort(), ['分页资料.pdf', '分页资料.docx', '多表资料.xlsx', '幻灯资料.pptx', '图形资料.png', '完整资料.md'].sort())
  assert.ok(completed.turn.actions.some(action => action.tool_name === 'run_command_with_outputs' && action.file_changes.length === 6))
  assert.equal(await access(join(task, 'processing.log')).then(() => true, () => false), true)
  assert.ok((await events()).some(event => event.event === 'supplement' && event.state === 'applied'))
  check('a real declared command creates six explicitly delivered files; inputs and its real log are excluded')
  const hashes = await Promise.all(completed.turn.artifacts.map(async artifact => createHash('sha256').update(await readFile(artifact.path)).digest('hex')))
  await click('查看会话成果')
  await selectArtifact(completed, '完整资料.md')
  const markdown = await execute('document.querySelector("[aria-label=\\"成果全文\\"]").innerText')
  assert.match(markdown, /完整资料首行/)
  assert.match(markdown, /资料第 75 段/)
  for (let index=1;index<=150;index++) assert.ok(markdown.includes(`资料第 ${index} 段：原始内容完整保留。`))
  assert.match(markdown, /完整资料末行/)
  await capture('documents-full-markdown')
  await click('返回聊天')
  const readingCount = await send('资料图片阅读验收')
  await waitFor(() => execute('document.body.innerText.includes("阅读段落 99")'), 'the long reading answer is visible')
  await overlayWhileStreaming(false)
  await click('停止回答')
  const imageStopped = await finishedAfter(readingCount)
  assert.equal(imageStopped.turn.status, 'cancelled')
  await click('查看会话成果')
  await selectArtifact(completed, '分页资料.pdf')
  await documentReady(1)
  const pdfFirstWidth = await execute('document.querySelector(".document-preview canvas").width')
  await click('下一页')
  await documentReady(2)
  await click('放大')
  await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').width > ${pdfFirstWidth}`), 'a real PDF page zooms')
  await capture('documents-pdf-page-2-zoom')
  await selectArtifact(completed, '分页资料.docx')
  await documentReady(1)
  assert.match(await execute('document.querySelector(".document-preview").innerText'), /1\s*\/\s*5/)
  await click('下一页')
  await documentReady(2)
  await capture('documents-word-page-2')
  await narrowReading(1.25)
  await click('查看会话成果')
  await selectArtifact(completed, '多表资料.xlsx')
  await documentReady(1)
  assert.equal(await execute('document.querySelector("[aria-label=\\"工作表\\"]").options.length'), 2)
  await execute('(() => {const select=document.querySelector("[aria-label=\\"工作表\\"]");select.selectedIndex=1;select.dispatchEvent(new Event("change",{bubbles:true}));})()')
  await documentReady(1)
  assert.equal(await execute('document.querySelector("[aria-label=\\"工作表\\"]").selectedIndex'), 1)
  await capture('documents-excel-sheet-2')
  await selectArtifact(completed, '幻灯资料.pptx')
  await documentReady(1)
  assert.equal(await execute('document.querySelector("[aria-label=\\"幻灯片\\"]").options.length'), 2)
  await execute('(() => {const select=document.querySelector("[aria-label=\\"幻灯片\\"]");select.selectedIndex=1;select.dispatchEvent(new Event("change",{bubbles:true}));})()')
  await documentReady(1)
  assert.equal(await execute('document.querySelector("[aria-label=\\"幻灯片\\"]").selectedIndex'), 1)
  await capture('documents-ppt-slide-2')
  assert.deepEqual(await Promise.all(completed.turn.artifacts.map(async artifact => createHash('sha256').update(await readFile(artifact.path)).digest('hex'))), hashes)
  check('delivered PDF and Office files render real pages, zoom and navigate without changing the originals')
  await click('返回聊天')
  await sharedControls()
  await reopen(completed.path, '资料完整验收', 6)
  await click('查看会话成果')
  await selectArtifact(completed, '图形资料.png')
  await waitFor(() => execute('document.querySelector(".artifact-preview img")?.complete && document.querySelector(".artifact-preview img").naturalWidth===800'), 'reopened image bytes are actually decoded')
  await capture('documents-reopened-image')
  check('reopened document deliveries keep their six identities and an actual decoded image')
}
const pageReady = () => execute('!!document.querySelector(".document-preview canvas[data-rendered=\\"true\\"]")')
async function documentReady(page) {
  await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').getAttribute('aria-label')==='第 ${page} 页'`), `real document page ${page} finishes rendering`)
}
async function overlayWhileStreaming(nearBottom) {
  await execute(`(() => { const trigger=document.querySelector('[aria-label="放大图片 图形资料.png"]'); trigger.scrollIntoView({block:'center',behavior:'instant'}); })()`)
  await pause(80)
  if (nearBottom) await execute('document.querySelector(".conversation").scrollTop=document.querySelector(".conversation").scrollHeight')
  const before = await execute('(() => {const area=document.querySelector(".conversation");return {top:area.scrollTop,height:area.scrollHeight,gap:area.scrollHeight-area.scrollTop-area.clientHeight};})()')
  if (nearBottom) assert.ok(before.gap<=96, 'the first overlay starts while following the bottom')
  await click('放大图片 图形资料.png')
  await waitFor(() => execute('!!document.querySelector("dialog.image-preview-dialog[open]")'), 'image modal opens')
  const opened = await execute('document.querySelector(".conversation").scrollTop')
  await click('放大图片')
  assert.equal(await execute('document.querySelector("[aria-label=\\"图片缩放\\"]").textContent'), '125%')
  await waitFor(() => execute(`document.querySelector('.conversation').scrollHeight > ${before.height}+100`), 'the actual answer grows behind the image')
  await capture(nearBottom ? 'documents-image-following-bottom' : 'documents-image-reading-history')
  window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'ESC' })
  window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'ESC' })
  await waitFor(() => execute('!document.querySelector("dialog.image-preview-dialog")'), 'Escape closes the image')
  await waitFor(async () => (await body()).includes('回到最新内容'), 'closing the image commits the paused reading controls')
  assert.equal(await execute('document.activeElement.getAttribute("aria-label")'), '放大图片 图形资料.png')
  assert.ok(Math.abs(await execute('document.querySelector(".conversation").scrollTop') - opened)<=1, 'closing preserves the image trigger reading position despite streamed growth')
  await execute('document.querySelector(".conversation").scrollTop=0;document.querySelector(".conversation").dispatchEvent(new Event("scroll"))')
  await pause(250)
  assert.equal(await execute('document.querySelector(".conversation").scrollTop'), 0)
  check(`image zoom and Escape restore actual focus and scrolling during streamed growth: ${nearBottom ? 'following bottom' : 'reading history'}`, { before, opened })
}
async function runReopen() {
  const state = JSON.parse(await readFile(join(directory, 'workflow-state.json'), 'utf8'))
  await fill('[aria-label="搜索会话标题"]', state.title)
  await waitFor(() => execute(`[...document.querySelectorAll('button')].some(button=>button.getAttribute('aria-label')===${JSON.stringify(`打开会话：${state.title}`)})`), 'saved conversation appears after full process restart')
  await click(`打开会话：${state.title}`)
  await waitFor(() => execute('!!document.querySelector(".conversation")'), 'a fresh backend opens the saved conversation')
  const opened = (await events()).findLast(event => event.event === 'conversation' && event.path === state.path)
  assert.deepEqual(opened.turns.flatMap(turn => turn.artifacts ?? []).map(artifact => artifact.id), state.artifacts.map(artifact => artifact.id))
  assert.equal(opened.running, false)
  assert.equal((await events()).some(event => event.event === 'started'), false, 'reopening never reruns work')
  await click('查看会话成果')
  await selectArtifact({ turn: { artifacts: state.artifacts } }, mode === 'programming' ? 'calculator.py' : '图形资料.png')
  if (mode === 'programming') {
    assert.equal(await execute('document.querySelector("[aria-label=\\"成果全文\\"]").textContent'), await readFile(join(task, 'calculator.py'), 'utf8'))
    assert.match(await body(), /交付后已有变化/)
  } else {
    await waitFor(() => execute('document.querySelector(".artifact-preview img")?.complete && document.querySelector(".artifact-preview img").naturalWidth===800'), 'the fresh process decodes the saved image delivery')
  }
  await capture(`${mode}-after-full-restart`)
  check('a completely new Electron and Python process reloads the same delivered identities and reads current bytes without rerunning the task')
}
async function run() {
  if (exitControl.requested) return
  await mkdir(screenshots)
  if (mode === 'documents') {
    const bitmap = Buffer.alloc(800 * 600 * 4)
    for (let y=0;y<600;y++) for (let x=0;x<800;x++) {
      const offset=(y*800+x)*4
      bitmap[offset]=x<400?190:65;bitmap[offset+1]=y<300?140:200;bitmap[offset+2]=x<400?75:220;bitmap[offset+3]=255
    }
    await writeFile(join(task, 'input.png'), nativeImage.createFromBitmap(bitmap, {width:800,height:600}).toPNG())
  }
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [task] })
  shell.showItemInFolder = value => { reveals.push(value) }
  await import(builtMain())
  await app.whenReady()
  await attach()
  if (phase === 'reopen') await runReopen()
  else {
  await click('打开设置')
  await fill('#connection-password', 'synthetic-offline-workflow')
  await click('保存设置')
  await waitFor(() => execute('!document.querySelector("dialog.settings-dialog")'), 'offline settings saved')
  await click('选择任务文件夹')
  await waitFor(() => execute('!!document.querySelector("dialog.folder-trust[open]")'), 'folder trust is requested')
  await click('信任文件夹')
  await waitFor(() => execute('!!document.querySelector(".task-scope")'), 'the chosen task folder is saved')
  await choose('[aria-label="新一轮批准方式"]', 'request_approval')
  if (mode === 'layout') await runReadingLayout()
  else if (mode === 'programming') await runProgramming()
  else await runDocuments()
  const initial = (await events()).find(event => event.event === 'finished' && event.turn.artifacts?.length)
  await writeFile(join(directory, 'workflow-state.json'), JSON.stringify({ path: initial.path, title: `${prefix}完整验收`, artifacts: initial.turn.artifacts }, null, 2))
  }
  assert.deepEqual(rendererErrors, [])
  const result = { allPassed: true, mode, phase, checks, layout, rendererErrors, finished: (await events()).filter(event => event.event === 'finished').map(event => ({ path: event.path, status: event.turn.status, taskId: event.turn.task?.id })), at: new Date().toISOString() }
  await writeFile(join(directory, `${outputPrefix}result.json`), JSON.stringify(result, null, 2))
  if (process.env.MYRK_WORKFLOW_EVIDENCE_DIRECTORY) {
    const evidence = await mkdtemp(join(process.env.MYRK_WORKFLOW_EVIDENCE_DIRECTORY, `${mode}-${phase}-`))
    result.evidenceDirectory = evidence
    await writeFile(join(directory, `${outputPrefix}result.json`), JSON.stringify(result, null, 2))
    await writeFile(join(evidence, 'owner.json'), JSON.stringify({ owner: 'Codex / workflow acceptance 06', project_root: 'E:\\Myrk', source_root: fileURLToPath(new URL('../../', import.meta.url)), purpose: 'Review actual built desktop workflow captures', created_at: new Date().toISOString(), delete_when: 'Delete after the workflow and visual acceptance is complete' }, null, 2))
    await copyFile(join(directory, `${outputPrefix}result.json`), join(evidence, 'result.json'))
    for (const name of await readdir(screenshots)) await copyFile(join(screenshots, name), join(evidence, name))
  }
  await execute('window.myrk.quit()')
}
run().catch(async error => {
  await writeFile(join(directory, `${outputPrefix}failure.json`), JSON.stringify({ error: String(error), stack: error.stack, rendererErrors, checks, layout, body: window && !window.isDestroyed() ? await body().catch(() => '') : '' }, null, 2))
  if (window && !window.isDestroyed()) await capture(`${mode}-failure`).catch(() => {})
  console.error(error)
  app.quit()
})
