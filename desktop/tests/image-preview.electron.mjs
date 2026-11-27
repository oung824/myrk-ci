import { app, BrowserWindow, dialog } from 'electron'
import assert from 'node:assert/strict'
import { copyFile, readFile, writeFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const exitControl = installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const directory = process.env.MYRK_IMAGE_CASE
const task = join(directory, 'task')
const outputs = JSON.parse(process.env.MYRK_DOCUMENT_OUTPUTS)
const rows = []
const rendererErrors = []
const externalRequests = []
let window
const execute = code => window.webContents.executeJavaScript(code)
const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 20000
  while (!await predicate()) { assert.ok(Date.now() < deadline, label); await pause(25) }
}
const click = label => execute(`(() => { const button = [...document.querySelectorAll('button')].find(item => item.getAttribute('aria-label') === ${JSON.stringify(label)} || item.textContent === ${JSON.stringify(label)}); if (!button || button.disabled) throw new Error('Missing enabled control: ' + ${JSON.stringify(label)}); button.click(); })()`)

async function run() {
  if (exitControl.requested) return
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [task] })
  await import(builtMain())
  await app.whenReady()
  await waitFor(async () => {
    window = BrowserWindow.getAllWindows()[0]
    return window && !window.isDestroyed() && await execute('!!window.myrk').catch(() => false)
  }, 'real application window and preload')
  window.webContents.on('console-message', (_event, details) => { if (details.level === 'error') rendererErrors.push(details.message) })
  window.webContents.session.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*'] }, (details, callback) => { externalRequests.push(details.url); callback({ cancel: true }) })
  await execute('window.imageEvents=[]; window.myrk.subscribe(event => window.imageEvents.push(event)); window.myrk.command({action:"snapshot"})')
  await waitFor(() => execute(`window.imageEvents.some(event => event.event === 'snapshot')`), 'current service snapshot received')
  await execute('window.myrk.selectWorkspace()')
  await execute(`window.myrk.command(${JSON.stringify({ action: 'set_project_trust', root: task, trusted: true })})`)
  await execute(`window.myrk.command(${JSON.stringify({ action: 'configure', key: 'synthetic-images-only' })})`)
  await waitFor(() => execute(`window.imageEvents.some(event => event.event === 'configured')`), 'offline connection configured')
  await execute(`window.myrk.command(${JSON.stringify({ action: 'send', prompt: '制作并明确交付四种合成图片。', approval_mode: 'full_access', scope: { root: task, allowed_actions: ['read', 'edit', 'check'], provider: 'deepseek', recipients: ['deepseek'] } })})`)
  await waitFor(() => execute(`window.imageEvents.some(event => event.event === 'finished')`), 'real declared output and delivery actions')
  const finished = await execute(`window.imageEvents.find(event => event.event === 'finished')`)
  assert.equal(finished.turn.status, 'completed', JSON.stringify(finished))
  assert.equal(finished.turn.artifacts.length, 4)
  const hashes = []
  for (const [, name] of outputs) {
    const artifact = finished.turn.artifacts.find(item => item.name === name)
    const request = { conversationId: finished.path, artifactId: artifact.id, requestId: randomUUID() }
    const preview = await execute(`window.myrk.openArtifactPreview(${JSON.stringify(request)})`)
    const extension = name.split('.').at(-1)
    rows.push({ extension, status: preview.status, ...(preview.status === 'ready' ? { width: preview.content.width, height: preview.content.height } : { message: preview.message }) })
    if (preview.status === 'ready') {
      assert.equal(preview.content.kind, 'image')
      const original = await readFile(join(task, name))
      assert.deepEqual(Buffer.from(preview.content.base64, 'base64'), original, 'preview preserves the exact delivered file bytes')
      hashes.push({ name, sha256: createHash('sha256').update(original).digest('hex') })
      await execute(`window.myrk.closeArtifactPreview(${JSON.stringify({ snapshotId: preview.snapshotId })})`)
    }
  }
  assert.deepEqual(rows, ['png', 'jpg', 'gif', 'webp'].map(extension => ({ extension, status: 'ready', width: 3, height: 2 })))
  for (const [, name] of outputs) {
    await waitFor(() => execute(`(() => { const image = [...document.querySelectorAll('.artifact-thumbnail img')].find(item => item.alt === ${JSON.stringify(name)}); return image?.complete && image.naturalWidth === 3 && image.naturalHeight === 2; })()`), `${name} actual inline image decoded`)
    await click(`放大图片 ${name}`)
    await waitFor(() => execute(`!!document.querySelector('dialog[open] img')?.complete`), `${name} actual overlay image decoded`)
    assert.deepEqual(await execute(`(() => { const image=document.querySelector('dialog[open] img'); return {width:image.naturalWidth,height:image.naturalHeight}; })()`), { width: 3, height: 2 })
    await click('关闭大图')
    await waitFor(() => execute(`document.activeElement.getAttribute('aria-label') === ${JSON.stringify(`放大图片 ${name}`)}`), `${name} closing restores the thumbnail focus`)
  }
  await click('查看会话成果')
  await click('预览 交付图片.gif')
  await waitFor(() => execute(`!!document.querySelector('.artifact-preview .artifact-thumbnail img')?.complete`), 'right workspace image is decoded')
  await execute(`window.originalFilePanel=document.querySelector('[aria-label="右侧文件查看区"]'); document.querySelector('.artifact-preview .artifact-thumbnail').click(); true`)
  await waitFor(() => execute(`!!document.querySelector('dialog[open]')`), 'right workspace image modal opened')
  window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'ESC' })
  window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'ESC' })
  await waitFor(() => execute(`!document.querySelector('dialog[open]')`), 'native Escape closes only the image modal')
  assert.equal(await execute(`document.querySelector('[aria-label="右侧文件查看区"]') === window.originalFilePanel && !!window.originalFilePanel?.isConnected`), true, 'Escape preserves the original right workspace')
  await waitFor(() => execute(`document.activeElement === document.querySelector('.artifact-preview .artifact-thumbnail')`), 'right workspace trigger focus restored')
  assert.deepEqual(rendererErrors, [])
  assert.deepEqual(externalRequests, [])
  const result = { allPassed: true, electron: process.versions.electron, rows, hashes, workspaceEscape: true, rendererErrors, externalRequests }
  await writeFile(join(directory, 'result.json'), JSON.stringify(result, null, 2))
  if (process.env.MYRK_IMAGE_EVIDENCE_DIRECTORY) {
    await copyFile(join(directory, 'result.json'), join(process.env.MYRK_IMAGE_EVIDENCE_DIRECTORY, 'result.json'))
  }
  await execute('window.myrk.quit()')
}

run().catch(async error => {
  await writeFile(join(directory, 'failure.json'), JSON.stringify({ error: String(error), stack: error.stack, rows, rendererErrors, externalRequests }, null, 2))
  console.error(error)
  app.quit()
})
