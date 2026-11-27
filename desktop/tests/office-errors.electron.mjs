import { app, BrowserWindow, dialog, shell } from 'electron'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { copyFile, mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const exitControl = installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const directory = process.env.MYRK_DOCUMENT_CASE
const task = join(directory, 'task')
const outputs = JSON.parse(process.env.MYRK_DOCUMENT_OUTPUTS)
const rendererErrors = []
const reveals = []
const cases = []
let window
const execute = code => window.webContents.executeJavaScript(code)
const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 20000
  while (!await predicate()) { assert.ok(Date.now() < deadline, label); await pause(30) }
}
const click = label => execute(`(() => { const button=[...document.querySelectorAll('button')].find(button=>button.getAttribute('aria-label')===${JSON.stringify(label)} || button.textContent===${JSON.stringify(label)}); if(!button || button.disabled) throw new Error('Missing enabled control: '+${JSON.stringify(label)});button.click(); })()`)
const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
const sha256 = async path => createHash('sha256').update(await readFile(path)).digest('hex')
const previews = async () => (await readdir(process.env.MYRK_RUNTIME_ROOT).catch(error => { if (error.code === 'ENOENT') return []; throw error })).filter(name => name.startsWith('document-preview-'))
const officeProcesses = () => JSON.parse(execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', "ConvertTo-Json -Compress -InputObject @(Get-Process -Name WINWORD,EXCEL,POWERPNT -ErrorAction SilentlyContinue | Sort-Object Id | Select-Object Id,ProcessName,@{n='created_at';e={$_.StartTime.ToUniversalTime().ToString('o')}})"], { encoding: 'utf8', windowsHide: true }))

async function save(allPassed, error) {
  const body = window && !window.isDestroyed() ? await execute('document.body.textContent').catch(() => '') : ''
  await writeFile(join(directory, 'result.json'), JSON.stringify({ allPassed, cases, rendererErrors, reveals, error: error ? String(error) : null, body }, null, 2))
  if (process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY) {
    await mkdir(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY)
    await copyFile(join(directory, 'result.json'), join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'result.json'))
    for (const name of await readdir(directory)) if (name.endsWith('.png')) await copyFile(join(directory, name), join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, name))
  }
}

async function run() {
  if (exitControl.requested) return
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [task] })
  shell.showItemInFolder = path => { reveals.push(path) }
  await import(builtMain())
  await app.whenReady()
  await waitFor(async () => { window=BrowserWindow.getAllWindows()[0]; return window && !window.webContents.isLoading() }, 'production window loaded')
  window.webContents.on('console-message', details => { if (details.level === 'error') rendererErrors.push(details.message) })
  await execute('window.officeErrorEvents=[];window.myrk.subscribe(event=>window.officeErrorEvents.push(event));true')
  await waitFor(() => execute(`window.officeErrorEvents.some(event=>event.event==='ready') || document.querySelector('[aria-label="发送消息"]')?.disabled===false`), 'service ready')
  await execute('window.myrk.selectWorkspace()')
  await command({ action: 'set_project_trust', root: task, trusted: true })
  await command({ action: 'configure', key: 'synthetic-document-preview' })
  await waitFor(() => execute(`window.officeErrorEvents.some(event=>event.event==='configured')`), 'offline connection configured')
  await command({ action: 'send', prompt: '明确交付本任务的合成异常 Office 文档，用于只读预览验收。', approval_mode: 'full_access', scope: { root: task, allowed_actions: ['read', 'edit', 'check'], provider: 'deepseek', recipients: ['deepseek'] } })
  await waitFor(() => execute(`window.officeErrorEvents.some(event=>event.event==='finished')`), 'synthetic delivery completed')
  const finished = await execute(`window.officeErrorEvents.find(event=>event.event==='finished')`)
  assert.equal(finished.turn.status, 'completed')
  assert.equal(finished.turn.artifacts.length, outputs.length)
  assert.ok(finished.turn.actions.some(action => action.tool_name === 'run_command_with_outputs' && action.file_changes.length === outputs.length))
  for (const [, name] of outputs) {
    const artifact = finished.turn.artifacts.find(item => item.name === name)
    const path = join(task, name)
    const beforeHash = await sha256(path)
    const beforeOffice = officeProcesses()
    const observation = { name, deliveredSha256: artifact.delivered_sha256, beforeHash, officeBefore: beforeOffice }
    cases.push(observation)
    await click('查看会话成果')
    await click(`预览 ${name}`)
    await waitFor(() => execute(`!!document.querySelector('.artifact-preview [role="status"]') && !document.querySelector('.artifact-preview').textContent.includes('正在读取文件快照')`), `${name}: unreadable explanation appeared`)
    observation.message = await execute(`document.querySelector('.artifact-preview [role="status"]').textContent`)
    await execute('document.fonts.ready.then(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))')
    await writeFile(join(directory, `${name}.png`), (await window.webContents.capturePage()).toPNG())
    await click('在文件夹中定位成果')
    await waitFor(() => reveals.includes(path), `${name}: located by the validated artifact identity`)
    observation.located = true
    await waitFor(async () => (await previews()).length === 0, `${name}: failed conversion released its snapshot`)
    observation.resourcesReleased = true
    observation.afterHash = await sha256(path)
    observation.sourceUnchanged = observation.afterHash === beforeHash && beforeHash === artifact.delivered_sha256
    observation.officeAfter = officeProcesses()
    observation.existingOfficeUnchanged = JSON.stringify(observation.officeAfter) === JSON.stringify(beforeOffice)
    assert.equal(observation.sourceUnchanged, true)
    assert.equal(observation.existingOfficeUnchanged, true)
    assert.match(observation.message, /损坏|密码|格式/, 'the actual document error must describe the unreadable input honestly')
    assert.doesNotMatch(observation.message, /请检查 Office 是否可用/, 'an unreadable input must not be presented as an Office installation failure')
    await click('返回聊天')
    await waitFor(() => execute(`!document.querySelector('.artifact-workspace')`), `${name}: the previous preview is unmounted before opening the next artifact`)
  }
  assert.deepEqual(await previews(), [])
  assert.deepEqual(rendererErrors, [])
  await save(true)
  await execute('window.myrk.quit()')
}

run().catch(async error => {
  await save(false, error)
  console.error(error)
  app.quit()
})
