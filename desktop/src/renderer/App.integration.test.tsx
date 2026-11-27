import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, test } from 'vitest'
import { ConnectionManager } from '../main/connection-manager'
import { ConnectionStore } from '../main/connection-store'
import { PythonService } from '../main/python-service'
import type { ServiceEvent } from '../shared/protocol'
import { decodeCommand } from '../shared/decode'
import { App } from './App'
import { testDirectory } from '../../tests/run-context.mjs'

test('desktop saves its request approval default without a service password and keeps it after restarting the service', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-approval-default-')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(decodeCommand(command)),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => null, selectDocument: async () => null,
    importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  let root = createRoot(element)
  const launch = () => new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  service = launch()
  act(() => root.render(<App />))
  const button = (label: string) => [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.getAttribute('aria-label') === label || button.textContent === label)!
  try {
    await expect.poll(() => events.some(event => event.event === 'snapshot'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    await act(async () => button('工作偏好').click())
    await act(async () => button('保存默认批准方式').click())
    await expect.poll(() => element.textContent, { timeout: 10000 }).toContain('默认批准方式已保存')
    expect(events.find(event => event.event === 'configured')).toMatchObject({ configured: false, approval_mode: 'request_approval' })
    await service.shutdown()
    act(() => root.unmount())
    events.length = 0
    root = createRoot(element)
    service = launch()
    act(() => root.render(<App />))
    await expect.poll(() => events.some(event => event.event === 'snapshot' && event.approval_mode === 'request_approval' && !event.configured), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    await act(async () => button('工作偏好').click())
    expect(element.querySelector<HTMLSelectElement>('#approval-mode')!.value).toBe('request_approval')
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 25000)

test('desktop keeps saved chat choices apart from the global default and reopens them without a password', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-chat-choice-')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(decodeCommand(command)),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => null, selectDocument: async () => null,
    importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  let root = createRoot(element)
  const launch = () => new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  service = launch()
  act(() => root.render(<App />))
  const button = (label: string) => [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.getAttribute('aria-label') === label || button.textContent === label)!
  const choose = async (selector: string, value: string) => act(async () => {
    const select = element.querySelector<HTMLSelectElement>(selector)!
    select.value = value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  try {
    await expect.poll(() => events.some(event => event.event === 'snapshot'), { timeout: 10000 }).toBe(true)
    const snapshot = events.find(event => event.event === 'snapshot')!
    if (snapshot.event !== 'snapshot') throw new Error('Missing current chat')
    const oldPath = snapshot.active_path
    await act(async () => button('打开设置').click())
    await act(async () => button('工作偏好').click())
    await choose('#approval-mode', 'full_access')
    await act(async () => button('保存默认批准方式').click())
    await expect.poll(() => element.textContent).toContain('本机默认：完全访问权限；会话已保存：请求批准')
    await act(async () => button('修改会话批准方式').click())
    await choose('#conversation-approval-mode', 'auto_review')
    await act(async () => button('保存会话批准方式').click())
    await expect.poll(() => element.textContent).toContain('本机默认：完全访问权限；会话已保存：帮我批准')
    expect(events.some(event => event.event === 'conversation_approval' && event.saved && event.approval_settings.reviewer === 'assistant')).toBe(true)
    await act(async () => button('新建会话').click())
    await expect.poll(() => element.textContent).toContain('本机默认：完全访问权限；会话已保存：完全访问权限')
    await service.shutdown()
    act(() => root.unmount())
    events.length = 0
    root = createRoot(element)
    service = launch()
    act(() => root.render(<App />))
    await expect.poll(() => events.some(event => event.event === 'snapshot' && !event.configured), { timeout: 10000 }).toBe(true)
    await expect.poll(() => Boolean(button('打开会话：新会话'))).toBe(true)
    await act(async () => button('打开会话：新会话').click())
    await expect.poll(() => events.some(event => event.event === 'conversation' && event.path === oldPath && event.approval_settings?.mode === 'auto_review')).toBe(true)
    expect(element.textContent).toContain('本机默认：完全访问权限；会话已保存：帮我批准')
    expect(events.some(event => event.event === 'started')).toBe(false)
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 25000)

test('a new chat inherits saved default approval settings after configuring a temporary connection', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-temporary-approval-')
  const records = join(folder, 'records')
  const preferences = join(records, 'settings', 'permissions.json')
  const deniedPath = join(folder, 'private-material')
  const revision = 'saved-permission-revision'
  const savedDefault = JSON.stringify({ mode: 'request_approval', access: { denied_paths: [deniedPath] }, revision })
  await mkdir(join(records, 'settings'), { recursive: true })
  await writeFile(preferences, savedDefault, 'utf8')

  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  function receive(event: ServiceEvent): void {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  }
  const service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
    root: resolve('..'), dataDirectory: records, module: 'tests.desktop_fixture' }, receive)
  const refuseCredentialStorage = async () => { throw new Error('Temporary connections must stay in memory') }
  const manager = new ConnectionManager(new ConnectionStore(join(folder, 'unused-credentials'), {
    isAsyncEncryptionAvailable: refuseCredentialStorage,
    encryptStringAsync: refuseCredentialStorage,
    decryptStringAsync: refuseCredentialStorage,
  }), service, receive)
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(decodeCommand(command)),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => null, selectDocument: async () => null,
    importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  try {
    act(() => root.render(<App />))
    await expect.poll(() => events.some(event => event.event === 'snapshot'), { timeout: 10000 }).toBe(true)
    await act(async () => manager.configureTemporary({
      provider_config: { name: 'DeepSeek', protocol: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
      key: 'synthetic-temporary-connection',
    }))
    const eventsBeforeNewChat = events.length
    const newChat = element.querySelector<HTMLButtonElement>('button[aria-label="新建会话"]')!
    await act(async () => newChat.click())
    const findNewConversationEvent = () => events.slice(eventsBeforeNewChat).find(event => event.event === 'conversation')
    await expect.poll(findNewConversationEvent, { timeout: 5000 }).toMatchObject({
      event: 'conversation',
      approval_settings: { mode: 'request_approval', access: { denied_paths: [deniedPath] }, revision },
    })
    expect(await readFile(preferences, 'utf8')).toBe(savedDefault)
    expect(events.some(event => event.event === 'started')).toBe(false)
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 25000)

test.each([false, true])('the desktop screen authorizes a real folder, shows sourced results, and reopens its evidence (long material: %s)', async longMaterial => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-ui-')
  await writeFile(join(folder, '采购.md'), '采购金额800以上需审批。\n', 'utf8')
  await writeFile(join(folder, '报销.txt'), '报销需在30天内提交。\n', 'utf8')
  if (longMaterial) await writeFile(join(folder, 'long.txt'), Array.from({ length: 600 }, (_, index) => `needle ${index + 1}: ${'详细规定'.repeat(25)}`).join('\n'), 'utf8')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let omissionShown = false
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(command),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => folder,
    selectDocument: async () => null,
    importConversation: async () => {},
    copy: async () => {},
    revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
    root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture',
  }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
    if (event.event === 'status' && event.text.includes('全文证据编号：')) omissionShown = element.textContent!.includes('全文证据编号：')
  })
  act(() => root.render(<App />))
  const button = (label: string) => element.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  const fill = (control: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(control, value)
    control.dispatchEvent(new Event('input', { bubbles: true }))
  }
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, longMaterial ? 'context-controlled' : 'scope-controlled'))
    await act(async () => element.querySelector<HTMLFormElement>('.settings form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    await expect.poll(() => events.some(event => event.event === 'configured')).toBe(true)
    await act(async () => button('选择任务文件夹').click())
    if (element.querySelector('dialog.folder-trust')) await act(async () => button('信任文件夹').click())
    await expect.poll(() => element.querySelector('dialog.folder-trust')).toBeNull()
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, longMaterial ? '搜索并回查第300行，保留未完事项' : '比较采购和报销规定'))
    await act(async () => button('发送消息').click())
    await expect.poll(() => events.some(event => event.event === 'finished'), { timeout: 10000 }).toBe(true)
    if (longMaterial) {
      expect(element.textContent).toContain('[long.txt:300]')
      expect(element.textContent).toContain('[long.txt:600]')
      expect(element.textContent).toContain('其余结果已省略；全文位置：')
      expect(omissionShown).toBe(true)
    } else {
      expect(element.textContent).toContain('[采购.md:1] 采购金额800以上需审批。')
      expect(element.textContent).toContain('[报销.txt:1] 报销需在30天内提交。')
    }
    expect(element.textContent).toContain('真实动作记录')
    expect(element.textContent).toContain('接收方：DeepSeek（理解和回答服务）')
    const finished = events.find(event => event.event === 'finished')!
    if (finished.event !== 'finished') throw new Error('Missing result')
    const original = await readFile(finished.path, 'utf8')
    expect(JSON.parse(original).turns[0].actions).toHaveLength(longMaterial ? 1 : 4)
    await act(async () => window.myrk.command({ action: 'new' }))
    await expect.poll(() => element.querySelector('[aria-label="对话内容"]')).toBeNull()
    await act(async () => window.myrk.command({ action: 'open', path: finished.path }))
    await expect.poll(() => element.textContent).toContain(longMaterial ? '[long.txt:300]' : '采购金额800以上需审批。')
    expect(element.textContent).toContain(folder)
    expect(await readFile(finished.path, 'utf8')).toBe(original)
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 20000)

test('the real desktop routes concurrent file work, acknowledges supplements, queues work and stops only its conversation', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-control-')
  await writeFile(join(folder, 'first.txt'), '第一聊天原始资料', 'utf8')
  await writeFile(join(folder, 'second.txt'), '另一份真实资料', 'utf8')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(command),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => folder, selectDocument: async () => null,
    importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  let root = createRoot(element)
  service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  act(() => root.render(<App />))
  const button = (label: string) => element.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  const fill = (control: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(control, value)
    control.dispatchEvent(new Event('input', { bubbles: true }))
  }
  const choose = async () => {
    await act(async () => button('选择任务文件夹').click())
    if (element.querySelector('dialog.folder-trust')) await act(async () => button('信任文件夹').click())
    await expect.poll(() => element.querySelector('dialog.folder-trust')).toBeNull()
  }
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'parallel-controlled'))
    await act(async () => element.querySelector<HTMLFormElement>('.settings form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    await expect.poll(() => events.some(event => event.event === 'configured')).toBe(true)
    await choose()
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '慢读补充'))
    await act(async () => button('发送消息').click())
    await expect.poll(() => events.some(event => event.event === 'started')).toBe(true)
    const first = events.find(event => event.event === 'started')!.path!
    await expect.poll(() => events.some(event => event.event === 'text' && event.path === first), { timeout: 10000 }).toBe(true)
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '补充：改读second.txt'))
    await act(async () => button('发送补充').click())
    await expect.poll(() => element.textContent).toContain('补充已收到')
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '排队读取'))
    await act(async () => button('排队新工作').click())
    await expect.poll(() => element.textContent).toContain('排队工作：1项')
    await act(async () => button('新建会话').click())
    await expect.poll(() => element.querySelector('[aria-label="对话内容"]')).toBeNull()
    await choose()
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '另一聊天读取并等待'))
    await act(async () => button('发送消息').click())
    await expect.poll(() => events.filter(event => event.event === 'started' && event.path !== first).length).toBe(1)
    const second = events.find(event => event.event === 'started' && event.path !== first)!.path!
    await expect.poll(() => events.some(event => event.event === 'action' && event.path === second && event.action.status === 'completed'), { timeout: 10000 }).toBe(true)
    await expect.poll(() => events.some(event => event.event === 'supplement' && event.path === first && event.state === 'applied'), { timeout: 10000 }).toBe(true)
    await expect.poll(() => events.filter(event => event.event === 'finished' && event.path === first).length, { timeout: 10000 }).toBe(2)
    expect(element.querySelector('[aria-label="对话内容"]')!.textContent).not.toContain('慢读补充')
    expect(element.textContent).toContain('另一份真实资料')
    const startedBeforeReopen = events.filter(event => event.event === 'started').length
    act(() => root.unmount())
    root = createRoot(element)
    act(() => root.render(<App />))
    await expect.poll(() => button('停止回答')).toBeTruthy()
    expect(events.filter(event => event.event === 'started')).toHaveLength(startedBeforeReopen)
    expect(element.textContent).toContain('另一份真实资料')
    await act(async () => button('停止回答').click())
    expect(element.textContent).toContain('正在停止')
    await expect.poll(() => events.some(event => event.event === 'finished' && event.path === second)).toBe(true)
    expect(element.textContent).toContain('已停止')
    expect(events.filter(event => event.event === 'finished' && event.path === first).every(event => event.event === 'finished' && event.turn.status === 'completed')).toBe(true)
    await act(async () => window.myrk.command({ action: 'open', path: first }))
    await expect.poll(() => element.textContent).toContain('第2次请求准备时已采用')
    expect(element.textContent).toContain('请求尝试：')
    expect(element.textContent).toContain('服务实际用量：')
    const stored = JSON.parse(await readFile(first, 'utf8'))
    expect(stored.queue).toEqual([])
    expect(stored.turns).toHaveLength(2)
    expect(stored.turns[0].actions[0].target).toBe('second.txt')
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 20000)

test('the user trusts a folder, edits real text, calculates, and accepts work after the selected check actually ran', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-delivery-')
  await writeFile(join(folder, 'notes.txt'), '苹果 3\n梨子 2\n', 'utf8')
  await writeFile(join(folder, 'amounts.csv'), 'item,amount\na,0.10\nb,0.20\nc,-0.05\n', 'utf8')
  const script = join(folder, 'check.py')
  await writeFile(script, "import pathlib\nassert '苹果 5' in pathlib.Path('notes.txt').read_text(encoding='utf-8')\nprint('real-desktop-check-passed')\n", 'utf8')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(command), subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => folder, selectDocument: async () => null, importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  act(() => root.render(<App />))
  const button = (label: string) => Array.from(element.querySelectorAll<HTMLButtonElement>('button')).find(button => button.getAttribute('aria-label') === label || button.textContent === label)!
  const fill = (control: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(control, value)
    control.dispatchEvent(new Event('input', { bubbles: true }))
  }
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'delivery-controlled'))
    await act(async () => element.querySelector<HTMLFormElement>('.settings form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    await expect.poll(() => events.some(event => event.event === 'configured')).toBe(true)
    await act(async () => button('选择任务文件夹').click())
    if (element.querySelector('dialog.folder-trust')) await act(async () => button('信任文件夹').click())
    await expect.poll(() => element.querySelector('dialog.folder-trust')).toBeNull()
    act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="指定检查程序与参数"]')!, JSON.stringify([process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', script])))
    act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '修改数量、复算表格并检查'))
    await act(async () => button('发送消息').click())
    await expect.poll(() => events.some(event => event.event === 'finished'), { timeout: 10000 }).toBe(true)
    expect(await readFile(join(folder, 'notes.txt'), 'utf8')).toBe('苹果 5\n梨子 2\n')
    expect(element.textContent).toContain('0.25')
    expect(element.textContent).toContain('real-desktop-check-passed')
    expect(element.textContent).toContain('实际检查通过')
    expect(element.textContent).toContain('待验收')
    await act(async () => button('我已接受成果').click())
    await expect.poll(() => element.textContent).toContain('用户已接受')
    const finished = events.find(event => event.event === 'finished')!
    expect(JSON.parse(await readFile(finished.path!, 'utf8')).turns[0].delivery_status).toBe('accepted')
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 20000)

test('the real desktop shows on-demand skill selection delegation and main artifact verification', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-delivery-')
  await writeFile(join(folder, 'notes.txt'), '苹果 3\n', 'utf8')
  await writeFile(join(folder, 'amounts.csv'), 'item,amount\na,0.10\nb,0.20\nc,-0.05\n', 'utf8')
  const skill = join(folder, '.myrk', 'skills', 'edit-notes')
  await mkdir(skill, { recursive: true })
  await writeFile(join(skill, 'SKILL.md'), '---\nname: edit-notes\ndescription: 精确修改后读回\n---\n只改获准的一处，并核验。', 'utf8')
  await writeFile(join(skill, 'details.txt'), '保留原件。', 'utf8')
  const script = join(folder, 'check.py')
  await writeFile(script, "import pathlib\nassert '苹果 5' in pathlib.Path('notes.txt').read_text(encoding='utf-8')\nprint('real-delegation-check')\n", 'utf8')
  const listeners = new Set<(event: ServiceEvent) => void>()
  const events: ServiceEvent[] = []
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(command), subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => folder, selectDocument: async () => null, importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(folder, 'records'), module: 'tests.desktop_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  act(() => root.render(<App />))
  const button = (label: string) => Array.from(element.querySelectorAll<HTMLButtonElement>('button')).find(button => button.getAttribute('aria-label') === label || button.textContent === label)!
  const fill = (control: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(control, value)
    control.dispatchEvent(new Event('input', { bubbles: true }))
  }
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'delegation-controlled'))
    await act(async () => element.querySelector<HTMLFormElement>('.settings form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    await expect.poll(() => events.some(event => event.event === 'configured')).toBe(true)
    await act(async () => button('选择任务文件夹').click())
    if (element.querySelector('dialog.folder-trust')) await act(async () => button('信任文件夹').click())
    await expect.poll(() => element.querySelector('dialog.folder-trust')).toBeNull()
    act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="指定检查程序与参数"]')!, JSON.stringify([process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', script])))
    act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '修改数量、复算表格并检查'))
    await act(async () => button('发送消息').click())
    await expect.poll(() => events.some(event => event.event === 'finished'), { timeout: 10000 }).toBe(true)
    expect(await readFile(join(folder, 'notes.txt'), 'utf8')).toBe('苹果 5\n')
    expect(element.textContent).toContain('按需分工与主助理核验')
    expect(element.textContent).toContain('主助理不设办事轮数上限')
    expect(element.textContent).toContain('已办7轮；实际请求尝试：7次')
    expect(element.textContent).toContain('本助理已办3/250轮；实际请求尝试：3次')
    expect(element.textContent).toContain('整项累计请求次数：10次，仅作统计')
    expect(element.textContent).toContain('技能目录（可复用的办事步骤，选中后才读正文）')
    expect(element.textContent).toContain('edit-notes（步骤名称）：精确修改后读回')
    expect(element.textContent).toContain('主助理已核对所列真实原文')
    expect(element.textContent).toContain('分工处理结束')
    expect(element.textContent).toContain('real-delegation-check')
    expect(element.textContent).toContain('实际检查通过')
    expect(element.textContent).toContain('待验收')
    await act(async () => button('我已接受成果').click())
    await expect.poll(() => element.textContent).toContain('用户已接受')
    const finished = events.find(event => event.event === 'finished')!
    expect(JSON.parse(await readFile(finished.path!, 'utf8')).turns[0].delivery_status).toBe('accepted')
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 20000)
