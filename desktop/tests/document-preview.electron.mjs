import { app, BrowserWindow, dialog } from 'electron'
import assert from 'node:assert/strict'
import { access, copyFile, mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'
import { ProcessObservation } from './process-observation.mjs'

const exitControl = installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const directory = process.env.MYRK_DOCUMENT_CASE
const task = join(directory, 'task')
const rendererErrors = []
const pdfResources = []
const outputs = JSON.parse(process.env.MYRK_DOCUMENT_OUTPUTS)
const mode = process.env.MYRK_DOCUMENT_MODE ?? 'pdf'
const screenshots = join(directory, 'screenshots')
let window
const execute = code => window.webContents.executeJavaScript(code)
const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 25000
  while (!await predicate()) { assert.ok(Date.now() < deadline, `${label}: ${await execute('document.body.textContent')}`); await pause(30) }
}
const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
const click = label => execute(`(() => { const button = [...document.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === ${JSON.stringify(label)} || button.textContent === ${JSON.stringify(label)}); if (!button || button.disabled) throw new Error('Missing enabled control: ' + ${JSON.stringify(label)}); button.click(); })()`)
const pageReady = () => execute(`!!document.querySelector('.document-preview canvas[data-rendered="true"]')`)
const capture = async name => {
  await execute(`document.querySelector('.document-preview').scrollIntoView({block:'start'}); document.fonts.ready`)
  await execute('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
  await waitFor(pageReady, 'capture has a completed canvas')
  await writeFile(join(screenshots, `${name}.png`), (await window.webContents.capturePage()).toPNG())
}
const save = async result => {
  assert.deepEqual(rendererErrors, [])
  const nativeWorkers = await execute('window.documentWorkers')
  assert.ok(nativeWorkers.some(item => item.url.startsWith('file:') && item.url.includes('pdf.worker') && item.type === 'module' && item.name === 'myrk-document-preview' && item.messages > 0), 'the native local PDF worker exchanged actual messages')
  await writeFile(join(directory, 'result.json'), JSON.stringify({ allPassed: true, ...result, rendererErrors, pdfResources, nativeWorkers }, null, 2))
  if (process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY) {
    await mkdir(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY)
    await copyFile(join(directory, 'result.json'), join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'result.json'))
    for (const name of await readdir(screenshots)) await copyFile(join(screenshots, name), join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, name))
  }
  await execute('window.myrk.quit()')
}

async function checkCleanupRetry(conversationId, artifactId) {
  const ready = await execute(`window.myrk.openArtifactPreview(${JSON.stringify({ conversationId, artifactId, requestId: randomUUID() })})`)
  assert.equal(ready.status, 'ready')
  let snapshot
  for (const name of await readdir(process.env.MYRK_RUNTIME_ROOT)) {
    if (!name.startsWith('document-preview-')) continue
    const candidate = join(process.env.MYRK_RUNTIME_ROOT, name)
    const owner = await readFile(join(candidate, 'owner.json'), 'utf8').then(JSON.parse)
    if (owner.snapshot_id === ready.snapshotId) snapshot = candidate
  }
  assert.ok(snapshot)
  const controls = join(directory, 'snapshot-lock')
  await mkdir(controls)
  const locked = join(controls, 'locked.flag')
  const release = join(controls, 'release.flag')
  const literal = path => `'${path.replaceAll("'", "''")}'`
  const script = `$handle = [IO.File]::Open(${literal(join(snapshot, 'source.docx'))}, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None); try { [IO.File]::WriteAllText(${literal(locked)}, 'locked'); while (-not [IO.File]::Exists(${literal(release)})) { Start-Sleep -Milliseconds 25 } } finally { $handle.Dispose() }`
  const executable = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe'
  const argv = ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')]
  const observer = await ProcessObservation.start(join(controls, 'observer'))
  const child = spawn(executable, argv, { cwd: controls, env: process.env, windowsHide: true, stdio: 'ignore' })
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    assert.equal((await observer.capture(child, [executable, ...argv], controls)).ownership, 'owned')
    await waitFor(() => access(locked).then(() => true, () => false), 'snapshot lock held by the owned helper')
    const refused = await execute(`window.myrk.closeArtifactPreview(${JSON.stringify({ snapshotId: ready.snapshotId })}).then(() => '', reason => String(reason))`)
    assert.match(refused, /释放|cleanup/i)
    assert.equal(JSON.parse(await readFile(join(snapshot, 'owner.json'), 'utf8')).snapshot_id, ready.snapshotId, 'cleanup failure retains its ownership evidence')
  } finally {
    await writeFile(release, 'release this test-owned file handle')
    await closed
    const final = await observer.wait(5000)
    await writeFile(join(controls, 'result.json'), JSON.stringify(final, null, 2))
    await observer.close()
    assert.equal(final.complete, true)
  }
  await execute(`window.myrk.closeArtifactPreview(${JSON.stringify({ snapshotId: ready.snapshotId })})`)
  assert.equal(await access(snapshot).then(() => true, () => false), false, 'cleanup can be retried after the owned lock is released')
}

async function run() {
  if (exitControl.requested) return
  await mkdir(screenshots)
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [task] })
  await import(builtMain())
  await app.whenReady()
  await waitFor(async () => {
    window = BrowserWindow.getAllWindows()[0]
    return window && !window.webContents.isLoading()
  }, 'production window loaded')
  window.webContents.on('console-message', details => {
    if (details.level === 'error') rendererErrors.push(details.message)
  })
  window.webContents.session.webRequest.onCompleted(details => {
    if (details.url.includes('pdf')) pdfResources.push({ url: details.url, resourceType: details.resourceType, statusCode: details.statusCode })
  })
  await execute(`window.documentWorkers=[]; window.Worker=class extends window.Worker { constructor(url,options) { super(url,options); const record={url:String(url),type:options?.type,name:options?.name,messages:0};window.documentWorkers.push(record);this.addEventListener('message',()=>record.messages++); } }; true`)
  await execute('window.documentEvents=[]; window.myrk.subscribe(event => window.documentEvents.push(event)); true')
  await waitFor(() => execute(`window.documentEvents.some(event => event.event === 'ready') || document.querySelector('[aria-label="发送消息"]')?.disabled === false`), 'service ready')
  await execute('window.myrk.selectWorkspace()')
  await command({ action: 'set_project_trust', root: task, trusted: true })
  await command({ action: 'configure', key: 'synthetic-document-preview' })
  await waitFor(() => execute(`window.documentEvents.some(event => event.event === 'configured')`), 'offline connection configured')
  await command({ action: 'send', prompt: '生成并明确交付本任务的合成文档。', approval_mode: 'full_access', scope: { root: task, allowed_actions: ['read', 'edit', 'check'], provider: 'deepseek', recipients: ['deepseek'] } })
  await waitFor(() => execute(`window.documentEvents.some(event => event.event === 'finished')`), 'real delivery completed')
  const finished = await execute(`window.documentEvents.find(event => event.event === 'finished')`)
  assert.equal(finished.turn.status, 'completed', JSON.stringify(finished))
  assert.equal(finished.turn.artifacts.length, outputs.length)
  assert.ok(finished.turn.actions.some(action => action.tool_name === 'run_command_with_outputs' && action.file_changes.length === outputs.length))
  await click('查看会话成果')
  const selectArtifact = async name => {
    if (await execute(`!!document.querySelector('[aria-label="切换会话成果"]')`)) {
      const artifact = finished.turn.artifacts.find(item => item.name === name)
      await execute(`(() => { const select=document.querySelector('[aria-label="切换会话成果"]'); select.value=${JSON.stringify(artifact.id)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`)
    } else await click(`预览 ${name}`)
  }
  if (mode === 'office') {
    const sourceHashes = await Promise.all(outputs.map(async ([, target]) => createHash('sha256').update(await readFile(join(task, target))).digest('hex')))
    const word = finished.turn.artifacts.find(item => item.name.endsWith('.docx'))
    const requestId = randomUUID()
    await execute(`window.pendingDocumentPreview = window.myrk.openArtifactPreview(${JSON.stringify({ conversationId: finished.path, artifactId: word.id, requestId })}).then(result => ({ result }), reason => ({ error: String(reason) })); true`)
    await waitFor(async () => {
      const roots = await readdir(process.env.MYRK_RUNTIME_ROOT).catch(() => [])
      return (await Promise.all(roots.filter(name => name.startsWith('document-preview-')).map(name => access(join(process.env.MYRK_RUNTIME_ROOT, name, 'conversion', 'office-owner.json')).then(() => true, () => false)))).some(Boolean)
    }, 'owned Office conversion is pending')
    await execute(`window.myrk.closeArtifactPreview(${JSON.stringify({ requestId })})`)
    const cancelled = await execute('window.pendingDocumentPreview')
    assert.match(cancelled.error ?? cancelled.result?.message ?? '', /取消|关闭/)
    assert.deepEqual((await readdir(process.env.MYRK_RUNTIME_ROOT)).filter(name => name.startsWith('document-preview-')), [])

    await selectArtifact('分页表格.docx')
    await waitFor(pageReady, 'Word first page rendered')
    assert.match(await execute(`document.querySelector('.document-preview').textContent`), /1\s*\/\s*5/)
    await capture('word-page-1')
    await checkCleanupRetry(finished.path, word.id)
    await click('下一页')
    await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').getAttribute('aria-label') === '第 2 页'`), 'Word table continuation rendered')
    await capture('word-page-2')

    await selectArtifact('多表图表.xlsx')
    await waitFor(pageReady, 'Excel first sheet rendered')
    const worksheets = await execute(`document.querySelector('[aria-label="工作表"]').options.length`)
    assert.equal(worksheets, 2)
    await capture('excel-sheet-1')
    await execute(`(() => {const select=document.querySelector('[aria-label="工作表"]');select.selectedIndex=1;select.dispatchEvent(new Event('change',{bubbles:true}));})()`)
    await waitFor(async () => await pageReady() && await execute(`document.querySelector('[aria-label="工作表"]').selectedIndex === 1`), 'Excel second sheet rendered')
    await capture('excel-sheet-2')

    await selectArtifact('母版幻灯片.pptx')
    await waitFor(pageReady, 'PowerPoint first slide rendered')
    const slides = await execute(`document.querySelector('[aria-label="幻灯片"]').options.length`)
    assert.equal(slides, 2)
    await capture('ppt-slide-1')
    await execute(`(() => {const select=document.querySelector('[aria-label="幻灯片"]');select.selectedIndex=1;select.dispatchEvent(new Event('change',{bubbles:true}));})()`)
    await waitFor(async () => await pageReady() && await execute(`document.querySelector('[aria-label="幻灯片"]').selectedIndex === 1`), 'PowerPoint second slide rendered')
    await capture('ppt-slide-2')
    assert.deepEqual(await Promise.all(outputs.map(async ([, target]) => createHash('sha256').update(await readFile(join(task, target))).digest('hex'))), sourceHashes)
    await click('返回聊天')
    await waitFor(async () => !(await readdir(process.env.MYRK_RUNTIME_ROOT)).some(name => name.startsWith('document-preview-')), 'closed workspace released document resources')
    await save({ wordPages: 5, worksheets, slides, pendingCancelled: true, cleanupRetry: true, sourceUnchanged: true })
    return
  }
  await waitFor(() => execute(`document.body.textContent.includes('中文分页.pdf')`), 'delivered PDF listed')
  await click('预览 中文分页.pdf')
  await waitFor(pageReady, 'first PDF canvas completed')
  const first = await execute(`(() => { const canvas = document.querySelector('.document-preview canvas'); const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; let marks=0; for(let i=0;i<pixels.length;i+=4) if(pixels[i]<220 || pixels[i+1]<220 || pixels[i+2]<220) marks++; return {width:canvas.width,height:canvas.height,marks,text:document.querySelector('.document-preview').textContent}; })()`)
  assert.ok(first.marks > 1000, 'real PDF page has rendered non-white content')
  assert.match(first.text, /1\s*\/\s*5/)
  await click('下一页')
  await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').getAttribute('aria-label') === '第 2 页'`), 'second PDF page completed')
  const width = await execute(`document.querySelector('.document-preview canvas').width`)
  await click('放大')
  await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').width > ${width}`), 'PDF zoom rendered')
  await click('上一页')
  await waitFor(async () => await pageReady() && await execute(`document.querySelector('.document-preview canvas').getAttribute('aria-label') === '第 1 页'`), 'first page restored')
  await capture('pdf-page')
  await save({ pages: 5, first })
}

run().catch(async error => {
  await writeFile(join(directory, 'failure.json'), JSON.stringify({ error: String(error), stack: error.stack, rendererErrors, pdfResources, nativeWorkers: window && !window.isDestroyed() ? await execute('window.documentWorkers').catch(() => []) : [], body: window && !window.isDestroyed() ? await execute('document.body.textContent').catch(() => '') : '' }, null, 2))
  console.error(error)
  app.quit()
})
