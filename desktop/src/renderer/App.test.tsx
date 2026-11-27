import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, test } from 'vitest'
import type { Action, Command, ServiceEvent } from '../shared/protocol'
import { decodeCommand, decodeEvent } from '../shared/decode'
import { App } from './App'

let root: Root
let element: HTMLDivElement
let listener: (event: ServiceEvent) => void
let commands: Command[]
let copied: string
let imports: number
let selectedDocument: string | null

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  commands = []
  copied = ''
  imports = 0
  selectedDocument = 'E:\\Myrk\\试用资料.txt'
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: async (command) => {
      commands.push(command)
      if (command.action === 'set_project_trust') listener({ event: 'project_trust', root: command.root, trusted: command.trusted, saved: true })
      if (command.action === 'configure') listener(command.approval_mode ? { event: 'configured', approval_mode: command.approval_mode, configured: false } : { event: 'configured' })
      if (command.action === 'send') listener({ event: 'started', prompt: command.prompt })
    },
    subscribe: (receive) => { listener = receive; return () => {} },
    copy: async (text) => { copied = text },
    importConversation: async () => { imports += 1 },
    selectDocument: async () => selectedDocument,
    selectWorkspace: async () => 'E:\\task-materials',
    revealFile: async () => {}, quit: async () => {},
  }
  element = document.createElement('div')
  document.body.append(element)
  root = createRoot(element)
  act(() => root.render(<App />))
  act(() => listener({ event: 'ready', path: 'records/current.json' }))
})

afterEach(() => {
  act(() => root.unmount())
  element.remove()
})

function button(label: string): HTMLButtonElement {
  const result = Array.from(element.querySelectorAll('button')).find(
    (entry) => entry.getAttribute('aria-label') === label || entry.textContent === label,
  )
  if (!result) throw new Error(`找不到按钮：${label}`)
  return result
}

function fill(control: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(control, value)
  control.dispatchEvent(new Event('input', { bubbles: true }))
}

test('restored passwords remain blank and forgetting one connection keeps another chat usable', async () => {
  const other = { name: 'Service B', base_url: 'https://service-b.example/v1', model: 'model-b', protocol: 'openai-chat' as const }
  const current = { name: 'Service A', base_url: 'https://service-a.example/v1', model: 'model-a', protocol: 'openai-chat' as const }
  expect(commands).toContainEqual({ action: 'get_connection_storage' })
  act(() => listener({ event: 'configured', configured: true, provider_config: other }))
  act(() => listener({ event: 'connection_storage', available: [current, other], saved: [current, other], error: null }))
  act(() => listener({ event: 'conversation', path: 'records/chat-a.json', model_selection: { provider_config: current, reasoning: 'high' }, turns: [], running: false }))
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('#connection-password')!.value).toBe('')
  expect(element.textContent).toContain('已保存到本机')
  await act(async () => button('忘记此连接密码').click())
  expect(commands.findLast(command => command.action === 'forget_connection')).toEqual({ action: 'forget_connection', provider_config: current })
  act(() => listener({ event: 'connection_storage', available: [other], saved: [other], error: null }))
  expect(element.querySelector('.settings')).not.toBeNull()
  expect(element.querySelector<HTMLInputElement>('#connection-password')!.required).toBe(true)
  await act(async () => button('取消').click())
  act(() => listener({ event: 'conversation', path: 'records/chat-b.json', model_selection: { provider_config: other, reasoning: 'high' }, turns: [], running: false }))
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '继续使用另一连接'))
  await act(async () => button('发送消息').click())
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ prompt: '继续使用另一连接', model_selection: { provider_config: other } })
})

test('a missing future connection does not disable resuming work whose password is still available', async () => {
  const original = { name: 'Service A', base_url: 'https://service-a.example/v1', model: 'model-a', protocol: 'openai-chat' as const }
  const future = { ...original, name: 'Service B', base_url: 'https://service-b.example/v1' }
  const task = { id: 'paused-a', goal: '继续原工作', scope: null, request_limit: null, provider_config: original }
  act(() => listener({ event: 'configured', configured: false, provider_config: future }))
  act(() => listener({ event: 'connection_storage', available: [original], saved: [original], error: null }))
  act(() => listener({ event: 'conversation', path: 'records/current.json', model_selection: { provider_config: future, reasoning: 'high' }, turns: [{ prompt: task.goal, answer: '', status: 'failed', service_failure: true, error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task }], running: false }))
  expect(button('继续原工作').disabled).toBe(false)
  await act(async () => button('继续原工作').click())
  expect(commands.findLast(command => command.action === 'resume_task')).toMatchObject({ task_id: 'paused-a' })
})

test('an empty restored store does not claim that a connection password was saved', () => {
  act(() => listener({ event: 'connection_storage', available: [], saved: [], error: null }))
  act(() => listener({ event: 'configured', configured: false }))
  expect(element.textContent).not.toContain('连接密码已保存到本机')
  expect(element.textContent).toContain('请在设置中填写连接密码')
})

test('a reasoning change waits for confirmation and sends its exact per-chat selection', async () => {
  const provider = { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' as const }
  const models = [{ id: provider.model, name: 'DeepSeek Flash', capability: 'chat' as const, reasoning_options: [{ id: 'low', label: '低' }, { id: 'high', label: '高' }], default_reasoning: 'high', reasoning_note: null, tools_supported: true, unavailable_reason: null }]
  act(() => listener({ event: 'configured', configured: true, provider_config: provider }))
  const loading = commands.findLast(command => command.action === 'get_model_controls')!
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: loading.request_id, selection: { provider_config: provider, reasoning: 'low' }, models, error: null }))
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '按新强度整理'))
  await act(async () => button('选择推理强度').click())
  await act(async () => fill(document.querySelector<HTMLInputElement>('[aria-label="推理强度"]')!, '1'))
  const change = commands.findLast(command => command.action === 'select_model')!
  expect(change).toMatchObject({ model: provider.model, reasoning: 'high' })
  act(() => element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  expect(commands.some(command => command.action === 'send')).toBe(false)
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: change.request_id, selection: { provider_config: provider, reasoning: 'high' }, models, error: null }))
  await act(async () => button('发送消息').click())
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ model_selection: { provider_config: provider, reasoning: 'high' } })
})

test('dragging through several reasoning positions ignores older acknowledgements and keeps the latest value', async () => {
  const provider = { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' as const }
  const models = [{ id: provider.model, name: 'DeepSeek Flash', capability: 'chat' as const, reasoning_options: [{ id: 'low', label: '低' }, { id: 'high', label: '高' }, { id: 'max', label: '最高' }], default_reasoning: 'high', reasoning_note: null, tools_supported: true, unavailable_reason: null }]
  act(() => listener({ event: 'configured', configured: true, provider_config: provider }))
  const loading = commands.findLast(command => command.action === 'get_model_controls')!
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: loading.request_id, selection: { provider_config: provider, reasoning: 'high' }, models, error: null }))
  await act(async () => button('选择推理强度').click())
  const slider = document.querySelector<HTMLInputElement>('[aria-label="推理强度"]')!
  await act(async () => fill(slider, '0'))
  const first = commands.findLast(command => command.action === 'select_model')!
  expect(slider.disabled).toBe(false)
  await act(async () => fill(slider, '2'))
  const last = commands.findLast(command => command.action === 'select_model')!
  expect(last).toMatchObject({ reasoning: 'max' })
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: first.request_id, selection: { provider_config: provider, reasoning: 'low' }, models, error: null }))
  expect(slider.value).toBe('2')
  expect(element.querySelector('.model-controls')?.getAttribute('aria-busy')).toBe('true')
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: last.request_id, selection: { provider_config: provider, reasoning: 'max' }, models, error: null }))
  expect(button('选择推理强度').textContent).toContain('最高')
  expect(element.querySelector('.model-controls')?.getAttribute('aria-busy')).toBe('false')
})

test('changing chats restores its model and rejects a late response from the former chat', async () => {
  const provider = { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-flash', protocol: 'google' as const }
  const selected = { provider_config: provider, reasoning: 'low' }
  const firstRequest = commands.findLast(command => command.action === 'get_model_controls')!
  act(() => listener({ event: 'conversation', path: 'records/other.json', model_selection: selected, turns: [], running: false }))
  expect(button('选择模型').textContent).toContain(provider.model)
  act(() => listener({ event: 'model_controls', path: 'records/current.json', request_id: firstRequest.request_id, selection: { provider_config: { ...provider, model: 'obsolete-model' }, reasoning: 'high' }, models: [], error: null }))
  expect(button('选择模型').textContent).toContain(provider.model)
  expect(element.textContent).not.toContain('obsolete-model')
})

test('a manually chosen model reuses the password for the same connection', async () => {
  const provider = { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-flash', protocol: 'google' as const }
  act(() => listener({ event: 'configured', configured: true, provider_config: provider }))
  await act(async () => button('打开设置').click())
  await act(async () => fill(element.querySelector<HTMLInputElement>('#service-model')!, 'gemini-2.5-pro'))
  expect(element.querySelector<HTMLInputElement>('#connection-password')!.required).toBe(false)
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toMatchObject({ provider_config: { ...provider, model: 'gemini-2.5-pro' } })
  expect(commands.findLast(command => command.action === 'configure')).not.toHaveProperty('key')
})

test('opening settings for another chat uses that chats provider and preserves its reasoning choice', async () => {
  const globalProvider = { name: 'Service B', base_url: 'http://127.0.0.1:9002/v1', model: 'model-b', protocol: 'openai-chat' as const }
  const chatProvider = { name: 'Service A', base_url: 'http://127.0.0.1:9001/v1', model: 'model-a', protocol: 'openai-chat' as const }
  act(() => listener({ event: 'configured', configured: true, provider_config: globalProvider }))
  act(() => listener({ event: 'conversation', path: 'records/chat-a.json', model_selection: { provider_config: chatProvider, reasoning: 'high' }, turns: [], running: false }))
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('#service-name')!.value).toBe('Service A')
  expect(element.querySelector<HTMLInputElement>('#service-model')!.value).toBe('model-a')
  await act(async () => button('工作偏好').click())
  act(() => fill(element.querySelector<HTMLInputElement>('#delegation-max-turns')!, '7'))
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toEqual({ action: 'configure', delegation_max_turns: 7, provider_config: chatProvider })
  const loading = commands.findLast(command => command.action === 'get_model_controls')!
  act(() => listener({ event: 'model_controls', path: 'records/chat-a.json', request_id: loading.request_id, selection: { provider_config: chatProvider, reasoning: 'high' }, models: [], error: null }))
  expect(button('选择推理强度').textContent).toContain('高')
})

test('Gemini settings save the native service and a manually entered model', async () => {
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  expect(element.textContent).toContain('Google Gemini Developer API')
  await act(async () => fill(element.querySelector<HTMLInputElement>('#connection-password')!, 'synthetic-google-key'))
  await act(async () => fill(element.querySelector<HTMLInputElement>('#service-model')!, 'gemini-manual'))
  await act(async () => button('保存设置').click())
  expect(decodeCommand(commands.findLast(command => command.action === 'configure'))).toMatchObject({
    action: 'configure', key: 'synthetic-google-key', provider_config: {
      name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-manual', protocol: 'google',
    },
  })
})

test('Gemini catalog loads after entering a key and saves the selected model ID', async () => {
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  await act(async () => {
    const key = element.querySelector<HTMLInputElement>('#connection-password')!
    fill(key, 'synthetic-google-key')
    key.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  const request = commands.findLast(command => command.action === 'fetch_model_catalog')!
  expect(request).toMatchObject({ action: 'fetch_model_catalog', key: 'synthetic-google-key', provider_config: { protocol: 'google' } })
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: request.request_id, models: [
    { id: 'gemini-catalog-one', name: 'Gemini Catalog One', capability: 'unknown' },
    { id: 'gemini-catalog-two', name: 'Gemini Catalog Two', capability: 'unknown' },
  ], error: null }))))
  const select = element.querySelector<HTMLSelectElement>('#google-model-catalog')!
  expect(select.textContent).toContain('Gemini Catalog Two · gemini-catalog-two · 能力未确认')
  expect(element.querySelector<HTMLInputElement>('#service-model')!.value).toBe('')
  await act(async () => { select.value = 'gemini-catalog-two'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await act(async () => button('保存设置').click())
  expect(decodeCommand(commands.findLast(command => command.action === 'configure'))).toMatchObject({ provider_config: { protocol: 'google', model: 'gemini-catalog-two' } })
})

test('a previous Gemini connection cannot supply a catalog after switching services', async () => {
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  await act(async () => {
    const key = element.querySelector<HTMLInputElement>('#connection-password')!
    fill(key, 'first-synthetic-key')
    key.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  const first = commands.findLast(command => command.action === 'fetch_model_catalog')!
  await act(async () => button('DeepSeek 回答服务').click())
  await act(async () => button('Gemini 官方回答服务').click())
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: first.request_id, models: [{ id: 'old-model', name: 'Old connection model', capability: 'unknown' }], error: null }))))
  expect(element.textContent).not.toContain('Old connection model')
  expect(element.querySelector('#google-model-catalog')).toBeNull()
})

test('Gemini work explicitly identifies Google as the task material recipient', async () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'configured', configured: true, provider_config: {
    name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-manual', protocol: 'google',
  } }))))
  await act(async () => button('选择任务文件夹').click())
  await act(async () => button('信任文件夹').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '用 Gemini 整理资料'))
  await act(async () => button('发送消息').click())
  expect(decodeCommand(commands.findLast(command => command.action === 'send'))).toMatchObject({ scope: { provider: 'google', recipients: ['google'] } })
})

test('Gemini refresh is available for an entered key and distinguishes catalog failure from an empty result', async () => {
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  await act(async () => fill(element.querySelector<HTMLInputElement>('#connection-password')!, 'synthetic-key'))
  expect(button('刷新模型目录').disabled).toBe(false)
  await act(async () => button('刷新模型目录').click())
  const failed = commands.findLast(command => command.action === 'fetch_model_catalog')!
  expect(decodeCommand(failed)).toEqual(failed)
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: failed.request_id, models: [], error: '目录读取失败：HTTP 403，权限不足。' }))))
  expect(element.querySelector('.settings')?.textContent).toContain('目录读取失败：HTTP 403，权限不足。')
  expect(element.querySelector('.settings')?.textContent).not.toContain('目录读取成功')
  await act(async () => button('刷新模型目录').click())
  const empty = commands.findLast(command => command.action === 'fetch_model_catalog')!
  expect(empty.request_id).not.toBe(failed.request_id)
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: empty.request_id, models: [], error: null }))))
  expect(element.querySelector('.settings')?.textContent).toContain('目录读取成功，没有可列出的生成型号')
  expect(element.querySelector('.settings')?.textContent).not.toContain('HTTP 403')
  await act(async () => fill(element.querySelector<HTMLInputElement>('#service-model')!, 'manual-after-empty'))
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toMatchObject({ provider_config: { model: 'manual-after-empty', protocol: 'google' } })
})

test('Gemini catalog replies remain attached to their key and settings opening', async () => {
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  await act(async () => {
    const key = element.querySelector<HTMLInputElement>('#connection-password')!
    fill(key, 'first-synthetic-key')
    key.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  const first = commands.findLast(command => command.action === 'fetch_model_catalog')!
  await act(async () => {
    const key = element.querySelector<HTMLInputElement>('#connection-password')!
    fill(key, 'second-synthetic-key')
    key.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  const second = commands.findLast(command => command.action === 'fetch_model_catalog')!
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: second.request_id, models: [{ id: 'current-model', name: 'Current key model', capability: 'unknown' }], error: null }))))
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: first.request_id, models: [{ id: 'old-model', name: 'Old key model', capability: 'unknown' }], error: null }))))
  expect(element.querySelector('#google-model-catalog')?.textContent).toContain('Current key model')
  expect(element.textContent).not.toContain('Old key model')
  await act(async () => button('关闭设置').click())
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: second.request_id, models: [{ id: 'late-model', name: 'Closed settings model', capability: 'unknown' }], error: null }))))
  await act(async () => button('打开设置').click())
  await act(async () => button('Gemini 官方回答服务').click())
  await act(async () => {
    const key = element.querySelector<HTMLInputElement>('#connection-password')!
    fill(key, 'third-synthetic-key')
    key.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  const third = commands.findLast(command => command.action === 'fetch_model_catalog')!
  expect(new Set([first.request_id, second.request_id, third.request_id]).size).toBe(3)
  expect(element.textContent).not.toContain('Closed settings model')
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: second.request_id, models: [{ id: 'late-model', name: 'Closed settings model', capability: 'unknown' }], error: null }))))
  expect(element.querySelector('#google-model-catalog')).toBeNull()
})

test('saved Gemini settings refresh the catalog with the in-memory connection key', async () => {
  const provider = { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'saved-model', protocol: 'google' }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'configured', configured: true, provider_config: provider }))))
  await act(async () => button('打开设置').click())
  const request = commands.findLast(command => command.action === 'fetch_model_catalog')!
  expect(decodeCommand(request)).toEqual({ action: 'fetch_model_catalog', request_id: request.request_id, provider_config: { name: 'Gemini', base_url: provider.base_url, protocol: 'google' } })
  act(() => listener(decodeEvent(JSON.stringify({ event: 'model_catalog', request_id: request.request_id, models: [{ id: 'different-model', name: 'Different model', capability: 'unknown' }], error: null }))))
  expect(element.querySelector<HTMLInputElement>('#service-model')!.value).toBe('saved-model')
  expect(element.querySelector<HTMLButtonElement>('[aria-label="Gemini 官方回答服务"]')!.getAttribute('aria-pressed')).toBe('true')
})

test('work preferences configure each child assistant turn limit and omit the former whole-task deadline', async () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'configured', configured: true, request_limit: null, delegation_max_turns: 250, active_seconds_limit: 600 }))))
  await act(async () => button('打开设置').click())
  await act(async () => button('工作偏好').click())
  expect(element.textContent).toContain('子助理办事轮数上限')
  expect(element.querySelector('#active-limit')).toBeNull()
  expect(element.textContent).not.toContain('默认600秒')
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toEqual({ action: 'configure', delegation_max_turns: 250 })
})

test('reopened compaction records show the actual result and read the registered original text', async () => {
  const source = { message_index: 0, part_index: 0, sha256: 'a'.repeat(64) }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', running: false, turns: [{ prompt: '原始目标', answer: '原始回答', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false }], compactions: [{
    id: 'summary-1', version: 1, task_id: 'original', status: 'completed', trigger: 'capacity_compression', model: 'controlled', layout_version: 2, replaced_id: null,
    source: [source], covered: [source], retained: [], summary: '工作摘要：保留原目标，尚待核验。', before_tokens: 900, after_tokens: 400, input_budget_tokens: 1000, measurement_source: 'estimate:text', target_reached: true, request_numbers: [2], error: null,
  }] }))))
  expect(element.textContent).toContain('已整理较早记录')
  expect(element.textContent).toContain('工作摘要：保留原目标，尚待核验。')
  expect(element.querySelector('.answer-body')!.textContent).toBe('原始回答')
  expect(element.textContent).toContain('估算')
  await act(async () => button('回查整理原文').click())
  expect(commands.at(-1)).toMatchObject({ action: 'get_compaction_evidence', path: 'records/current.json', summary_id: 'summary-1', message_index: 0, part_index: 0, start: 0 })
  act(() => listener(decodeEvent(JSON.stringify({ event: 'compaction_evidence', path: 'records/current.json', summary_id: 'summary-1', message_index: 0, part_index: 0, start: 0, text: '原始工具证据中间段：严禁覆盖已拒绝决定。', next_start: null, error: null }))))
  expect(element.querySelector('[aria-label="整理来源原文"]')!.textContent).toContain('原始工具证据中间段：严禁覆盖已拒绝决定。')
  expect(element.querySelector('[aria-label="整理来源原文"] a')).toBeNull()
})

test('compaction receipts keep their owner and unknown usage without adding another work round', () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '继续原工作', answer: '已有进度', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'usage', goal: '继续原工作', scope: null, request_limit: null, request_policy: 'independent', turns_used: 1, requests: 2, total_requests: 2 },
    request_attempts: [{ number: 2, own_number: 2, purpose: 'compaction', actor: 'delegation', model: 'controlled', provider: 'offline', delegation_id: 'child-1', status: 'failed', http_status: 503, error: '摘要服务未完成', decision: '保留原始记录', input_tokens: null, output_tokens: null }],
  } }))))
  expect(element.textContent).toContain('用途：整理较早记录')
  expect(element.textContent).toContain('分工：child-1')
  expect(element.textContent).toContain('主助理不设办事轮数上限；已办1轮；实际请求尝试：2次')
  expect(element.textContent).toContain('此次服务实际用量：未知')
  expect(element.textContent).toContain('摘要服务未完成')
})

test('a historical interrupted compaction does not become active when a different task starts', () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', running: false, turns: [{ prompt: '旧工作', answer: '', status: 'failed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false }], compactions: [{
    id: 'old-candidate', version: 1, task_id: 'old-task', status: 'preparing', trigger: 'capacity_compression', model: 'controlled', layout_version: 2, replaced_id: null, source: [], covered: [], retained: [], summary: '', before_tokens: 900, after_tokens: null, input_budget_tokens: 1000, measurement_source: 'local_text_estimate', target_reached: null, request_numbers: [1], error: null,
  }] }))))
  act(() => listener({ event: 'started', prompt: '另一项工作', task: { id: 'new-task', goal: '另一项工作', scope: null, request_limit: null } }))
  expect(element.querySelector('[aria-label="较早记录整理情况"]')!.textContent).toContain('整理曾中断，未采用候选摘要')
  expect(element.querySelector('[aria-label="较早记录整理情况"]')!.textContent).not.toContain('正在整理较早记录')
})

test.each([
  ['preparing', false, '整理曾中断，未采用候选摘要'],
  ['preparing', true, '正在整理较早记录'],
  ['failed', false, '整理失败，原始记录保留'],
  ['cancelled', false, '整理已取消，未采用候选摘要'],
] as const)('compaction status %s with running=%s never presents an uncommitted candidate as a success', (status, running, label) => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'snapshot', configured: false, active_path: 'records/current.json', conversations: [{ path: 'records/current.json', running, stopping: false, queue: [], turns: [{ prompt: '原任务', task: { id: 'original', goal: '原任务', scope: null, request_limit: null }, answer: '', status: running ? 'running' : 'failed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false }], compactions: [{
    id: 'candidate', version: 1, task_id: 'original', status, trigger: 'capacity_compression', model: 'controlled', layout_version: 2, replaced_id: null, source: [], covered: [], retained: [], summary: '未提交候选内容不可当作正式摘要', before_tokens: 900, after_tokens: null, input_budget_tokens: 1000, measurement_source: 'local_text_estimate', target_reached: null, request_numbers: [1], error: status === 'failed' ? '摘要没有有效减量，原文保留。' : null,
  }] }] }))))
  expect(element.querySelector('[aria-label="较早记录整理情况"]')!.textContent).toContain(label)
  expect(element.textContent).not.toContain('未提交候选内容不可当作正式摘要')
  if (status === 'failed') expect(element.textContent).toContain('摘要没有有效减量，原文保留。')
})

test.each(['independent', 'shared'] as const)('request statistics show the %s policy without counting delegations twice', policy => {
  const scope = { root: 'E:\\materials', allowed_actions: ['read'], provider: 'deepseek', recipients: ['deepseek'] }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '整理两份成果', answer: '请验收已有成果', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'usage', goal: '整理两份成果', scope, request_limit: policy === 'independent' ? null : 10, max_turns: null, turns_used: 2, request_policy: policy, requests: policy === 'independent' ? 2 : 10, total_requests: policy === 'independent' ? 15 : 10, delegation_max_turns: 250, active_seconds: 700, active_seconds_limit: null },
    delegations: [{ id: 'report', goal: '报告', context: '', scope, evidence_requirements: '', expected_files: [], record_path: 'records/report.json', status: 'limited', verification_status: 'pending', verified_files: [], report: '已有部分', error: null, request_limit: null, max_turns: 250, turns_used: 10, requests: policy === 'independent' ? 13 : 3, input_tokens: null, output_tokens: null }],
  } }))))
  expect(element.textContent).toContain(policy === 'independent' ? '主助理不设办事轮数上限；已办2轮；实际请求尝试：2次' : '旧记录共用次数：10/10次')
  expect(element.textContent).toContain(policy === 'independent' ? '整项累计请求次数：15次' : '整项累计请求次数：10次')
  expect(element.textContent).toContain(policy === 'independent' ? '本助理已办10/250轮；实际请求尝试：13次' : '本助理请求尝试：3次，已包含在旧记录共用次数中')
  expect(element.textContent).toContain('主动运行：700.0秒')
  expect(element.textContent).not.toContain('700.0/600')
})

test('an imported child record shows its own rounds and review usage instead of the root account', () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/child.json', turns: [{
    prompt: '受委派工作：整理报告', answer: '已有报告', status: 'limited', error: null, requests: 13, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'main', goal: '完整交付', scope: null, request_policy: 'independent', request_limit: null, max_turns: null, requests: 2, turns_used: 2, total_requests: 15 },
    request_owner_id: 'child', request_account: { request_limit: null, max_turns: 10, turns_used: 10, requests: 13, review_requests: 2, review_seconds: 1.5 },
  }] }))))
  expect(element.textContent).toContain('子助理已办10/10轮；实际请求尝试：13次')
  expect(element.textContent).toContain('其中独立审核：2次，1.5秒')
  expect(element.textContent).toContain('整项累计请求次数：15次')
  expect(element.textContent).not.toContain('主助理不设办事轮数上限')
  expect(element.querySelector('[aria-label="增加额度并继续"]')).toBeNull()
})

test('an unfinished review is shown as unfinished rather than as a refusal', () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '继续处理文件', answer: '保留已有成果', status: 'completed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'main', goal: '继续处理文件', scope: null, request_policy: 'independent', request_limit: null, requests: 1 },
    actions: [{ id: 'not-reviewed', task_id: 'main', kind: 'edit', target: 'notes.txt', status: 'denied', denial_source: 'review_incomplete', started_at: '2026-11-03', finished_at: null, output: '', error: '审核轮数不足，尚未执行。', full_output_path: null }],
  } }))))
  expect(element.textContent).toContain('审核未完成，动作未执行')
  expect(element.querySelector('.action-records')!.textContent).not.toContain('已拒绝')
})

test('program facts remain separate from the model answer and imported child records show their actual scope', () => {
  const scope = { root: 'E:\\materials', allowed_actions: ['read'], provider: 'deepseek', recipients: ['deepseek'] }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/child.json', turns: [{
    prompt: '受委派工作：报告', answer: '模型声称已经全部完成。', status: 'completed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'main', goal: '完整交付', scope, request_policy: 'independent', request_limit: null, requests: 1 },
    request_owner_id: 'child', request_account: { request_limit: null, max_turns: 250, turns_used: 1, requests: 1 },
    execution_scope: { ...scope, root: 'E:\\materials\\report' }, execution_facts: '真实记录：还有1份报告未核验。',
  }] }))))
  expect(element.querySelector('[aria-label="程序记录的事实"]')!.textContent).toContain('真实记录：还有1份报告未核验。')
  expect(element.querySelector('.answer-body')!.textContent).toBe('模型声称已经全部完成。')
  expect(element.querySelector('.answer-body')!.textContent).not.toContain('真实记录')
  expect(element.querySelector('.task-record')!.textContent).toContain('E:\\materials\\report')
})

test.each([null, 'child-1'])('review request receipts retain owner %s and both sequence numbers without adding usage twice', owner => {
  const mainRequests = owner === null ? 4 : 3
  const observation = {
    provider: 'openai', endpoint: 'http://127.0.0.1/v1', protocol: 'openai-chat', model: 'review-program', layout_version: 1,
    input_tokens: 20, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: 0,
    input_source: 'usage.prompt_tokens', output_source: 'usage.completion_tokens', cache_read_source: null, cache_write_source: 'usage.prompt_tokens_details.cache_write_tokens',
    usage_present: true, usage_status: 'conflict', raw_usage: { prompt_tokens: 20, 'prompt_tokens_details.cached_tokens': 15, prompt_cache_hit_tokens: 10 },
    field_presence: { 'usage.prompt_tokens_details.cached_tokens': true, 'usage.prompt_cache_hit_tokens': true }, sdk_usage: { input_tokens: 20, output_tokens: 5, cache_read_tokens: 15, cache_write_tokens: 0 },
    conflicts: ['缓存读取的两份来源不一致'], elapsed_seconds: 0.2, first_output_seconds: null, wire: null,
  }
  const attempts = [
    { number: 6, own_number: 3, actor: 'action_review', delegation_id: owner, model: 'review-program', provider: 'http://127.0.0.1/v1', status: 'failed', http_status: 503, error: '审核服务暂时失败', decision: '同一步重新尝试', input_tokens: null, output_tokens: null },
    { number: 7, own_number: 4, actor: 'action_review', delegation_id: owner, model: 'review-program', provider: 'http://127.0.0.1/v1', status: 'completed', http_status: null, error: null, decision: '审核完成', input_tokens: 20, output_tokens: 5, observation },
  ]
  const decoded = decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '核对后处理文件', answer: '保留原始回答', status: 'completed', error: null, requests: mainRequests, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'main', goal: '核对后处理文件', scope: null, request_policy: 'independent', request_limit: null, requests: mainRequests, total_requests: 7 },
    actions: [{ id: 'reviewed', task_id: 'main', kind: 'edit', target: 'notes.txt', status: 'completed', delegation_id: owner, started_at: '2026-11-03', finished_at: null, output: '已修改', error: null, full_output_path: null,
      review: { status: 'allowed', risk: 'low', authorization: 'high', reason: '已核对所选文件和修改范围。', attempts: 2, seconds: 1.25, input_tokens: null, output_tokens: null, user_override: false, override_used: false, model: 'review-program', provider: 'http://127.0.0.1/v1', request_owner_id: owner, request_attempts: attempts },
    }],
  } }))
  expect(decoded).toMatchObject({ turn: { request_attempts: [], actions: [{ review: { request_owner_id: owner, request_attempts: attempts } }] } })
  act(() => listener(decoded))
  const receipts = element.querySelector('[aria-label="审核每次真实请求"]')!
  expect(receipts.textContent).toContain(owner === null ? '审核所属助理：主助理' : '审核所属助理：分工 child-1')
  expect(receipts.textContent).toContain('整项第6次；本助理第3次')
  expect(receipts.textContent).toContain('整项第7次；本助理第4次')
  expect(receipts.textContent).toContain('动作审核')
  expect(receipts.textContent).toContain('审核服务暂时失败')
  expect(receipts.textContent).toContain('输入20、输出5')
  expect(receipts.querySelector('[aria-label="此次请求缓存观测"]')!.textContent).toContain('输入总量20；缓存读取未知；缓存写入0')
  expect(receipts.textContent).toContain('字段冲突，请查看来源')
  expect(receipts.textContent).toContain('来源冲突：缓存读取的两份来源不一致')
  expect(receipts.textContent).toContain('首段输出：未知')
  expect(receipts.textContent).toContain('缓存观测：未知（旧记录未保存）')
  expect(element.querySelectorAll('[aria-label="此次请求缓存观测"]')).toHaveLength(1)
  expect(element.textContent).toContain('整项累计请求次数：7次，仅作统计')
  expect(element.textContent).toContain(`实际请求尝试：${mainRequests}次`)
})

test('legacy shared review receipts describe the stored shared count without claiming it is child usage', () => {
  const decoded = decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '继续旧工作并审核', answer: '原工作继续办理。', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'legacy-main', goal: '原工作', scope: null, request_policy: 'shared', request_limit: 12, requests: 10, total_requests: 10 },
    actions: [{ id: 'legacy-review', task_id: 'legacy-main', kind: 'edit', target: 'notes.txt', status: 'completed', delegation_id: 'legacy-child', started_at: '2026-11-03', finished_at: null, output: '已修改', error: null, full_output_path: null,
      review: { status: 'allowed', risk: 'low', authorization: 'high', reason: '已核对所选文件和修改范围。', attempts: 1, seconds: 0.2, input_tokens: 20, output_tokens: 5, user_override: false, override_used: false, model: 'review-program', provider: 'http://127.0.0.1/v1', request_owner_id: 'legacy-child',
        request_attempts: [{ number: 10, own_number: 10, actor: 'action_review', delegation_id: 'legacy-child', model: 'review-program', provider: 'http://127.0.0.1/v1', status: 'completed', http_status: null, error: null, decision: '审核完成', input_tokens: 20, output_tokens: 5 }],
      },
    }],
  } }))
  expect(decoded).toMatchObject({ turn: { task: { requests: 10, request_limit: 12 }, actions: [{ review: { request_attempts: [{ number: 10, own_number: 10 }] } }] } })
  act(() => listener(decoded))
  const receipts = element.querySelector('[aria-label="审核每次真实请求"]')!
  expect(receipts.textContent).toContain('审核所属助理：分工 legacy-child')
  expect(receipts.textContent).toContain('整项第10次；共用账本第10次')
  expect(receipts.textContent).not.toContain('本助理第10次')
  expect(element.textContent).toContain('旧记录共用次数：10/12次')
  expect(element.textContent).toContain('整项累计请求次数：10次，仅作统计')
})

test.each(['auto_review', 'full_access'])('user can save default %s without configuring a service', async approvalMode => {
  await act(async () => button('打开设置').click())
  await act(async () => button('工作偏好').click())
  const mode = element.querySelector<HTMLSelectElement>('#approval-mode')!
  expect(mode.value).toBe('request_approval')
  expect([...mode.options].map(option => [option.value, option.disabled])).toEqual([
    ['request_approval', false], ['auto_review', false], ['full_access', false],
  ])
  await act(async () => {
    mode.value = approvalMode
    mode.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await act(async () => button('保存默认批准方式').click())
  expect(commands).toContainEqual({ action: 'configure', approval_mode: approvalMode })
  expect(element.textContent).toContain('默认批准方式已保存')
  expect(element.textContent).not.toContain('连接资料已设置')
})

test('user saves a conversation choice without a service password and can discard unsaved edits', async () => {
  const approval = { mode: 'request_approval', access: { sandbox: 'restricted', writable_roots: [], denied_paths: [], read_only: false, legacy_scope: true }, condition: 'on_request', reviewer: 'user', revision: 'old-conversation' }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', turns: [], approval_settings: approval }))))
  expect(element.textContent).toContain('会话已保存：请求批准')
  window.myrk.command = async value => { commands.push(decodeCommand(value)) }
  await act(async () => button('修改会话批准方式').click())
  const choose = async (value: string) => act(async () => {
    const select = element.querySelector<HTMLSelectElement>('#conversation-approval-mode')!
    select.value = value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await choose('full_access')
  await act(async () => button('取消会话批准方式').click())
  expect(commands.some(command => command.action === 'set_conversation_approval')).toBe(false)
  expect(element.textContent).toContain('会话已保存：请求批准')
  await act(async () => button('修改会话批准方式').click())
  expect(element.querySelector<HTMLSelectElement>('#conversation-approval-mode')!.value).toBe('request_approval')
  await choose('auto_review')
  await act(async () => button('关闭会话批准方式').click())
  expect(commands.some(command => command.action === 'set_conversation_approval')).toBe(false)
  await act(async () => button('修改会话批准方式').click())
  const dialog = element.querySelector<HTMLDialogElement>('[aria-label="会话批准方式"]')!
  await act(async () => dialog.dispatchEvent(new Event('cancel', { cancelable: true })))
  expect(element.querySelector('[aria-label="会话批准方式"]')).toBeNull()
  await act(async () => button('修改会话批准方式').click())
  await act(async () => button('保存会话批准方式').click())
  expect(commands).toContainEqual({ action: 'set_conversation_approval', path: 'records/current.json', approval_mode: 'request_approval' })
  expect(element.textContent).toContain('原记录授权仍保留；明确保存才采用新方式')
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation_approval', path: 'records/current.json', approval_settings: { ...approval, access: { ...approval.access, legacy_scope: false } }, saved: true }))))
  expect(element.querySelector('[aria-label="会话批准方式"]')).toBeNull()
  expect(element.textContent).toContain('会话批准方式已保存')
})

test('changing the running reviewer shows the accepted turn choice and keeps each prepared action original', async () => {
  const approval = { mode: 'request_approval', access: { sandbox: 'restricted', writable_roots: ['E:\\materials'], denied_paths: [], read_only: false, legacy_scope: false }, condition: 'on_request', reviewer: 'user', revision: 'running-context' }
  const task = { id: 'live-task', goal: '逐个处理文件', scope: null, request_limit: 10, approval }
  const action = { id: 'prepared-before', task_id: task.id, kind: 'edit', target: 'E:\\outside\\first.txt', status: 'awaiting_approval', started_at: '2026-11-02', finished_at: null, output: '', error: null, full_output_path: null, approval_settings: approval, approval_id: 'before-approval' }
  const turn = { prompt: task.goal, answer: '', status: 'running', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task, approval_settings: approval, actions: [action] }
  const saved = { ...approval, mode: 'full_access', condition: 'never', reviewer: 'none', access: { ...approval.access, sandbox: 'full_access' } }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', turns: [turn], running: true, approval_settings: saved }))))
  window.myrk.command = async value => { commands.push(decodeCommand(value)) }
  expect(element.textContent).toContain('会话已保存：完全访问权限')
  expect(element.textContent).toContain('本轮实际访问范围：普通系统限制')
  expect(element.querySelector<HTMLSelectElement>('[aria-label="本轮后续新步骤审核者"]')!.value).toBe('user')
  await act(async () => {
    const select = element.querySelector<HTMLSelectElement>('[aria-label="本轮后续新步骤审核者"]')!
    select.value = 'assistant'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(commands).toContainEqual({ action: 'set_turn_reviewer', path: 'records/current.json', task_id: task.id, reviewer: 'assistant' })
  expect(element.querySelector<HTMLSelectElement>('[aria-label="本轮后续新步骤审核者"]')!.value).toBe('user')
  act(() => listener(decodeEvent(JSON.stringify({ event: 'turn_approval', path: 'records/current.json', task_id: task.id, approval_settings: { ...approval, reviewer: 'assistant' }, saved: true }))))
  expect(element.querySelector<HTMLSelectElement>('[aria-label="本轮后续新步骤审核者"]')!.value).toBe('assistant')
  expect(element.textContent).toContain('会话已保存：完全访问权限')
  expect(element.textContent).toContain('本次动作审核者：你本人')
  expect(element.textContent).toContain('本轮审核者已保存；只影响后续新准备的步骤')
  expect(button('批准这次动作').disabled).toBe(false)
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', task_id: task.id, action: { ...action, id: 'prepared-after', target: 'E:\\outside\\second.txt', approval_settings: { ...approval, reviewer: 'assistant' } } }))))
  expect(element.textContent).toContain('本次动作审核者：独立助理')
  expect(element.textContent).toContain('本次动作审核者：你本人')
})

test('a new turn override travels with its send while global and conversation choices stay distinct', async () => {
  const approval = { mode: 'request_approval', access: { sandbox: 'restricted', writable_roots: [], denied_paths: [], read_only: false, legacy_scope: false }, condition: 'on_request', reviewer: 'user', revision: 'saved-chat' }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', turns: [], approval_settings: approval }))))
  act(() => listener({ event: 'configured', configured: true, approval_mode: 'full_access' }))
  expect(element.textContent).toContain('本机默认：完全访问权限；会话已保存：请求批准')
  window.myrk.command = async value => { commands.push(decodeCommand(value)) }
  await act(async () => {
    const select = element.querySelector<HTMLSelectElement>('[aria-label="新一轮批准方式"]')!
    select.value = 'auto_review'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '按这个方式继续工作')
  })
  expect(commands.some(command => command.action === 'set_conversation_approval')).toBe(false)
  await act(async () => button('发送消息').click())
  expect(commands).toContainEqual({ action: 'send', path: 'records/current.json', prompt: '按这个方式继续工作', approval_mode: 'auto_review' })
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation_approval', path: 'records/current.json', approval_settings: { ...approval, mode: 'auto_review', reviewer: 'assistant' }, saved: true }))))
  expect(element.textContent).toContain('本机默认：完全访问权限；会话已保存：帮我批准')
})

test('independent review shows its real progress and decision without human approve controls', async () => {
  const approval = { mode: 'auto_review' as const, access: { sandbox: 'restricted' as const, writable_roots: ['E:\\materials'], denied_paths: [], read_only: false, legacy_scope: false }, condition: 'on_request' as const, reviewer: 'assistant' as const, revision: 'review-context' }
  const task = { id: 'review-task', goal: '核对后处理外部文件', scope: null, request_limit: 10, requests: 3, approval, review_requests: 2, review_seconds: 1.25, review_input_tokens: 42, review_output_tokens: 8 }
  const pending: Action = { id: 'review-action', task_id: task.id, kind: 'delete', target: 'E:\\outside\\record.txt', status: 'awaiting_approval', started_at: '2026-11-02', finished_at: null, output: '', error: null, full_output_path: null, impact: '删除确切文件；保留原件。', approval_id: 'review-approval', approval_basis: 'pending', approval_settings: approval,
    review: { status: 'running', risk: null, authorization: null, reason: '正在只读核对原件。', attempts: 1, seconds: .5, input_tokens: null, output_tokens: null, user_override: false, override_used: false, model: 'review-model', provider: 'http://127.0.0.1:9001/v1' } }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))))
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', task_id: task.id, action: pending }))))
  expect(element.textContent).toContain('独立助理正在审核')
  expect([...element.querySelectorAll('button')].some(item => item.textContent === '批准这次动作' || item.textContent === '拒绝这次动作')).toBe(false)
  const denied: Action = { ...pending, status: 'denied', approval_basis: 'denied', error: '本次动作未获允许。', review: { ...pending.review!, status: 'denied', risk: 'high', authorization: 'low', reason: '删除对象尚未查清，现有要求没有同意这一影响。', attempts: 2, seconds: 1.25 } }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: { prompt: task.goal, answer: '改为先核对资料。', status: 'completed', error: null, requests: 3, input_tokens: null, output_tokens: null, thinking: false, task, actions: [{ ...denied, review: { ...denied.review, messages: [{ parts: [{ part_kind: 'thinking', content: '不应出现在界面的内部思考' }] }] } }] } }))))
  expect(element.textContent).toContain('独立审核：已拒绝')
  expect(element.textContent).toContain('影响风险：高')
  expect(element.textContent).toContain('本人同意程度：低')
  expect(element.textContent).toContain(denied.review!.reason)
  expect(element.textContent).toContain('其中独立审核：2次，1.3秒')
  expect(element.textContent).toContain('输入42、输出8')
  expect(element.textContent).not.toContain('不应出现在界面的内部思考')
  act(() => listener(decodeEvent(JSON.stringify({ event: 'task', path: 'records/current.json', task: { ...task, review_unmeasured_requests: 1 } }))))
  expect(element.textContent).toContain('审核实际用量：未知（有1次未报告用量）')
})

test('user can authorize only a recent unused denial in the latest finished turn for one independent retry', async () => {
  act(() => listener({ event: 'configured', configured: true }))
  window.myrk.command = async value => { commands.push(decodeCommand(value)) }
  const task = { id: 'denials-task', goal: '处理已核对的文件', scope: null, request_limit: 30 }
  const actions: Action[] = Array.from({ length: 12 }, (_, index) => ({ id: `denied-${index}`, task_id: task.id, kind: 'edit', target: `E:\\outside\\file-${index}.txt`, status: 'denied', started_at: '2026-11-02', finished_at: '2026-11-02', output: '', error: '此次修改未获允许。', full_output_path: null, impact: `确切修改：file-${index}.txt。`, review: { status: 'denied', risk: 'high', authorization: 'low', reason: '目前要求没有涵盖修改这个文件的影响。', attempts: 1, seconds: .1, input_tokens: null, output_tokens: null, user_override: false, override_used: index === 11, model: 'controlled-review', provider: 'controlled-local' } }))
  const turn = { prompt: task.goal, answer: '有未获准的动作，请核对。', status: 'completed' as const, error: null, requests: 20, input_tokens: null, output_tokens: null, thinking: false, task, actions }
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{ ...turn, task: { ...task, id: 'earlier-task' }, actions: [{ ...actions[0], id: 'old-denial', task_id: 'earlier-task' }] }, turn], running: false }))
  let retryButtons = [...element.querySelectorAll('button')].filter(item => item.textContent === '授权此动作再审核一次')
  expect(retryButtons).toHaveLength(9)
  expect(element.textContent).toContain('只对这一个动作再审一次，不代表直接执行')
  await act(async () => retryButtons[0].click())
  expect(commands).toContainEqual({ action: 'retry_denied_action', path: 'records/current.json', task_id: task.id, action_id: 'denied-2' })
  expect(commands.some(item => item.action === 'approve_action')).toBe(false)
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{ ...turn, actions: actions.map(item => item.id === 'denied-2' ? { ...item, review: { ...item.review!, override_used: true } } : item) }], running: false }))
  retryButtons = [...element.querySelectorAll('button')].filter(item => item.textContent === '授权此动作再审核一次')
  expect(retryButtons).toHaveLength(8)
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: '继续核对', task }))
  expect([...element.querySelectorAll('button')].some(item => item.textContent === '授权此动作再审核一次')).toBe(false)
})

test('saving work preferences preserves the same provider review metadata received from the service', async () => {
  const provider = { name: '本机预设服务', base_url: 'http://127.0.0.1:9001/v1', model: 'main-model', protocol: 'openai-chat', review_model_override: 'special-review', preferred_review_model: 'codex-auto-review', available_models: ['main-model', 'special-review'], model_catalog_url: 'http://127.0.0.1:9001/catalog', basic_session_source: true, review_reasoning_efforts: ['low'], model_specialty: 'cyber' }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'configured', configured: true, provider_config: provider }))))
  await act(async () => button('打开设置').click())
  await act(async () => button('工作偏好').click())
  await act(async () => fill(element.querySelector<HTMLInputElement>('#delegation-max-turns')!, '7'))
  await act(async () => button('保存设置').click())
  expect(commands).toContainEqual(expect.objectContaining({ action: 'configure', delegation_max_turns: 7, provider_config: provider }))
})

test.each(['memory', 'capacity'])('a specific human-only %s decision keeps its own approve control', async kind => {
  const task = { id: 'human-specific', goal: '完成工作并保存事实', scope: null, request_limit: 10 }
  const action: Action = { id: 'human-specific-action', task_id: task.id, kind: kind === 'memory' ? 'memory' : 'edit', target: '本次已展示的具体对象', status: 'awaiting_approval', started_at: '2026-11-02', finished_at: null, output: '', error: null, full_output_path: null, impact: '只处理这次已经展示的具体内容。', approval_id: 'human-specific-approval', approval_basis: 'pending', memory_deferred: kind === 'memory',
    approval_settings: { mode: 'auto_review', access: { sandbox: 'restricted', writable_roots: [], denied_paths: [], read_only: false, legacy_scope: false }, condition: 'on_request', reviewer: kind === 'memory' ? 'user' : 'assistant', revision: 'human-specific-context' },
    review: kind === 'memory' ? null : { status: 'failed', risk: null, authorization: null, reason: '最少必要资料仍超过可处理的数量。', attempts: 1, seconds: .1, input_tokens: null, output_tokens: null, user_override: false, override_used: false, human_fallback: true, model: 'controlled-review', provider: 'controlled-local' } }
  if (kind === 'memory') act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{ prompt: task.goal, answer: '事实提议待你决定。', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false, task, actions: [action] }], running: false }))
  else {
    act(() => listener({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))
    act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', task_id: task.id, action }))))
    expect(element.textContent).toContain('现交由你本人决定')
  }
  expect(element.textContent).toContain('等待你批准')
  await act(async () => button('批准这次动作').click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: task.id, action_id: action.id, approval_id: action.approval_id, approved: true })
})

test('cross-project memory approval shows the exact prepared note as plain text', async () => {
  const task = { id: 'recall-task', goal: '取用项目甲的笔记', scope: null, request_limit: 10 }
  const content = '第一行  保留空格\n第二行 <strong>原样显示</strong>'
  const action: Action = {
    id: 'recall-action', task_id: task.id, kind: 'memory_recall', target: '项目甲', status: 'awaiting_approval',
    started_at: '2026-11-18', finished_at: null, output: '', error: null, full_output_path: null,
    impact: '跨项目取用笔记：项目甲；内容将提供给本任务已选服务乙。', approval_id: 'recall-approval', approval_basis: 'pending',
    approval_preview: content,
  }
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action }))))

  const card = element.querySelector('[aria-label="待处理事项"] [aria-label="动作：项目甲"]')!
  expect(card.textContent).toContain(action.impact)
  const preview = card.querySelector('pre[aria-label="待提供给模型的笔记内容"]')
  expect(preview?.textContent).toBe(content)
  expect(preview?.childElementCount).toBe(0)
  expect(button('拒绝这次动作').disabled).toBe(false)
  await act(async () => button('批准这次动作').click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: task.id, action_id: action.id, approval_id: action.approval_id, approved: true })

  const legacyAction = { ...action, approval_preview: undefined }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action: legacyAction }))))
  expect(card.textContent).toContain(action.impact)
  expect(card.querySelector('pre[aria-label="待提供给模型的笔记内容"]')).toBeNull()
  expect(button('批准这次动作').disabled).toBe(true)
  expect(button('拒绝这次动作').disabled).toBe(false)

  const conflictedAction = { ...legacyAction, status: 'conflict' }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action: conflictedAction }))))
  expect(card.textContent).toContain('待取用内容或接收方已变化')
})

test('user reviews the exact replacement address and necessary history before accepting or refusing it', async () => {
  const original = { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' as const }
  const target = { name: 'DeepSeek', base_url: 'http://127.0.0.1:9001/v1', model: 'second-model', protocol: 'openai-chat' as const }
  const task = { id: 'service-task', goal: '保留原工作', scope: null, request_limit: 10, requests: 3, active_seconds: 12, provider_config: original }
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: { prompt: task.goal, answer: '已做一部分，尚未完成', status: 'failed', error: '服务暂时不可用；等待你决定', requests: 3, input_tokens: null, output_tokens: null, thinking: false, task, service_failure: true, request_attempts: [{ number: 3, model: original.model, provider: original.base_url, delegation_id: null, status: 'failed', http_status: 503, error: '503（网络回应状态）', decision: '重试用尽，等待用户', input_tokens: null, output_tokens: null }] } }))
  expect(element.textContent).toContain('第3次请求')
  expect(element.textContent).toContain('503（网络回应状态）')
  expect(element.textContent).toContain('服务实际用量：未知')
  await act(async () => button('打开设置').click())
  await act(async () => button('自定义兼容服务').click())
  await act(async () => {
    fill(element.querySelector<HTMLInputElement>('#service-name')!, target.name)
    fill(element.querySelector<HTMLInputElement>('#service-address')!, target.base_url)
    fill(element.querySelector<HTMLInputElement>('#service-model')!, target.model)
    const protocol = element.querySelector<HTMLSelectElement>('#service-protocol')!
    protocol.value = target.protocol
    protocol.dispatchEvent(new Event('change', { bubbles: true }))
    fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'new-target-password')
  })
  await act(async () => button('保存设置').click())
  expect(commands).toContainEqual(expect.objectContaining({ action: 'configure', provider_config: target }))
  await act(async () => button('审阅更换服务').click())
  expect(commands).toContainEqual({ action: 'preview_provider', path: 'records/current.json', task_id: task.id, provider_config: target })
  const change = { id: 'change-one', task_id: task.id, previous: original, target, materials: '必要原话、已完成改写、失败与待办；仍使用原文件范围与已用3次。', compatible: true, reason: '普通文字与动作配对可用；实际服务仍需验证。', approved: null }
  act(() => listener({ event: 'provider_preview', path: 'records/current.json', task_id: task.id, change }))
  expect(element.textContent).toContain(target.base_url)
  expect(element.textContent).toContain(change.materials)
  await act(async () => button('拒绝更换服务').click())
  expect(commands).toContainEqual({ action: 'decide_provider', path: 'records/current.json', change_id: change.id, approved: false })
  act(() => listener({ event: 'provider_decided', path: 'records/current.json', approved: false }))
  expect(element.querySelector('[aria-label="更换服务审阅"]')).toBeNull()
  act(() => listener({ event: 'provider_preview', path: 'records/current.json', task_id: task.id, change: { ...change, id: 'change-two', compatible: false, reason: '额外思考无法可靠接收，停止且保留原件。' } }))
  expect(button('批准更换服务并续办').disabled).toBe(true)
  expect(element.textContent).toContain('额外思考无法可靠接收，停止且保留原件。')
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{ prompt: task.goal, answer: '已做一部分，尚未完成', status: 'failed', error: '503', requests: 3, input_tokens: null, output_tokens: null, thinking: false, task, service_failure: true }], recovery: { task_id: task.id, required: true, can_resume: false, message: '有影响未知动作，须先核查。', actions: [] } }))
  expect(element.textContent).toContain('服务失败后保留原工作，等待你核查并选择续办。')
  expect(button('审阅更换服务').disabled).toBe(true)
})

test('user sees one exact approval impact and rejects it, then accepts delivered work separately', async () => {
  const task = { id: 'task-edit', goal: '改文件', scope: null, request_limit: 10 }
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: '改文件', task }))
  act(() => listener({ event: 'action', path: 'records/current.json', task_id: task.id, action: {
    id: 'action-delete', task_id: task.id, kind: 'delete', target: 'E:\\materials\\notes.txt', status: 'awaiting_approval',
    started_at: '2026-11-02', finished_at: null, output: '', error: null, full_output_path: null,
    impact: '删除单个文件：E:\\materials\\notes.txt；保留原件。', approval_id: 'approval-one', approval_basis: 'pending',
    approval_settings: { mode: 'request_approval', access: { sandbox: 'restricted', writable_roots: ['E:\\materials'], denied_paths: [], read_only: false, legacy_scope: false }, condition: 'on_request', reviewer: 'user', revision: 'original-context' },
  } }))
  expect(element.textContent).toContain('删除单个文件：E:\\materials\\notes.txt')
  expect(element.textContent).toContain('等待你批准')
  expect(element.textContent).toContain('本次动作审核者：你本人')
  await act(async () => button('拒绝这次动作').click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: task.id, action_id: 'action-delete', approval_id: 'approval-one', approved: false })
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: { prompt: '改文件', answer: '处理结束', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false, task, checks_passed: false, delivery_status: 'awaiting_acceptance' } }))
  expect(element.textContent).toContain('实际检查未通过')
  expect(element.textContent).toContain('待验收')
  expect(element.textContent).toContain('本次处理结束，记录已保存；成果仍待你验收。')
  expect(button('发送消息').disabled).toBe(true)
  await act(async () => button('我已接受成果').click())
  expect(commands).toContainEqual({ action: 'accept_delivery', path: 'records/current.json', task_id: task.id })
})

test.each([false, true])('accepting resumed work preserves partial answers, failures and actions; later failure=%s', async (laterFailure) => {
  const task = { id: 'resumed-delivery', goal: '完成两处修改', scope: null, request_limit: 10, requests: 4 }
  const earlier = { prompt: task.goal, answer: '早前只完成第一处', status: 'failed' as const, error: '保留真实服务失败', requests: 3, input_tokens: null, output_tokens: null, thinking: false, task,
    actions: [{ id: 'first-edit', task_id: task.id, kind: 'edit', target: 'first.txt', status: 'completed' as const, started_at: '2026-11-02', finished_at: null, output: '第一处真实改动证据', error: null, full_output_path: null }] }
  const latest = { prompt: '继续原工作', answer: '末次完成第二处', status: 'completed' as const, error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task, checks_passed: true, delivery_status: 'awaiting_acceptance' as const }
  const following = { ...earlier, prompt: '继续核查', answer: '之后失败时的部分回答', error: '之后的真实失败', actions: [] }
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: laterFailure ? [earlier, latest, following] : [earlier, latest] }))
  await act(async () => button('我已接受成果').click())
  expect(commands).toContainEqual({ action: 'accept_delivery', path: 'records/current.json', task_id: task.id })
  act(() => listener({ event: 'delivery', path: 'records/current.json', task_id: task.id, saved: true, turn: { ...latest, delivery_status: 'accepted' } }))
  const answers = [...element.querySelectorAll('article[aria-label="Myrk 的回答"]')]
  expect(answers).toHaveLength(laterFailure ? 3 : 2)
  expect(answers[0].textContent).toContain('早前只完成第一处')
  expect(answers[0].textContent).toContain('保留真实服务失败')
  expect(answers[0].textContent).toContain('第一处真实改动证据')
  expect(answers[0].textContent).not.toContain('末次完成第二处')
  expect(answers[1].textContent).toContain('末次完成第二处')
  expect(answers[1].textContent).toContain('用户已接受')
  if (laterFailure) {
    expect(answers[2].textContent).toContain('之后失败时的部分回答')
    expect(answers[2].textContent).toContain('之后的真实失败')
    expect(answers[2].textContent).not.toContain('末次完成第二处')
  }
})

test('user sets a connection password and sends a plain message, reads formatted streamed answer and copies it', async () => {
  expect(element.textContent).toContain('今天想完成什么？')
  expect(button('发送消息').disabled).toBe(true)
  await act(async () => button('打开设置').click())
  const key = element.querySelector<HTMLInputElement>('input[type="password"]')!
  act(() => fill(key, 'synthetic-secret-never-save'))
  await act(async () => button('保存设置').click())
  expect(element.querySelector('input[type="password"]')).toBeNull()
  const prompt = element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!
  act(() => fill(prompt, '你好'))
  await act(async () => prompt.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  expect(commands).toContainEqual({ action: 'send', path: 'records/current.json', prompt: '你好' })
  const answer = '# 标题\n\n- 要点\n\n> 引用\n\n```js\nconst count = 1\n```'
  act(() => listener({ event: 'text', text: answer }))
  expect(element.querySelector('h1')?.textContent).toBe('标题')
  expect(element.querySelector('li')?.textContent).toBe('要点')
  expect(element.querySelector('blockquote')?.textContent).toContain('引用')
  expect(element.querySelector('pre code')?.textContent).toContain('const count = 1')
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '你好', answer, status: 'completed', error: null, requests: 1,
    input_tokens: 10, output_tokens: 5, thinking: false,
  } }))
  expect(element.textContent).toContain('已完成，记录已保存')
  await act(async () => button('复制回答').click())
  expect(copied).toBe(answer)
  expect(element.textContent).not.toContain('synthetic-secret-never-save')
})

test('user stops a streamed answer and remains blocked until cleanup actually finishes', async () => {
  act(() => listener({ event: 'conversations', items: [{ path: 'records/old.json', title: '旧资料' }], unreadable: 0 }))
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '请继续说明'))
  await act(async () => button('发送消息').click())
  act(() => listener({ event: 'text', text: '已经到达的部分' }))
  expect(button('新建会话').disabled).toBe(false)
  expect(element.querySelector('button[aria-label="导入旧记录"]')).toBeNull()
  expect(button('打开会话：旧资料').disabled).toBe(false)
  act(() => listener({ event: 'error', message: '请等待当前工作收尾后再操作。' }))
  expect(button('新建会话').disabled).toBe(false)
  expect(button('打开设置').disabled).toBe(true)
  await act(async () => button('停止回答').click())
  expect(element.textContent).toContain('正在停止')
  expect(element.textContent).not.toContain('已停止')
  expect(button('停止回答').disabled).toBe(true)
  act(() => listener({ event: 'stopping' }))
  expect(button('新建会话').disabled).toBe(false)
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '请继续说明', answer: '已经到达的部分', status: 'cancelled', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  } }))
  expect(element.textContent).toContain('已停止')
  expect(element.textContent).toContain('未完成')
  expect(element.textContent).toContain('已经到达的部分')
  expect(button('新建会话').disabled).toBe(false)
  expect(button('打开设置').disabled).toBe(false)
  expect(button('打开会话：旧资料').disabled).toBe(false)
})

test('failed work keeps partial answer and explains the failure without claiming it was saved', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '请说明失败'))
  await act(async () => button('发送消息').click())
  act(() => listener({ event: 'text', text: '已经到达的内容' }))
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: false, turn: {
    prompt: '请说明失败', answer: '已经到达的内容', status: 'failed',
    error: '无法建立回答服务连接，请检查连接密码和网络后重试。',
    requests: 0, input_tokens: null, output_tokens: null, thinking: false,
  } }))
  expect(element.textContent).toContain('已经到达的内容')
  expect(element.textContent).toContain('请检查连接密码和网络')
  expect(element.textContent).toContain('未保存')
  expect(element.textContent).not.toContain('已保存')
  expect(button('新建会话').disabled).toBe(false)
})

test('user adjusts the default round limit without restoring a thinking switch or reentering the password', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('工作偏好').click())
  const limit = element.querySelector<HTMLInputElement>('#delegation-max-turns')!
  expect(limit?.value).toBe('250')
  expect(element.querySelector('[aria-label="额外思考"]')).toBeNull()
  act(() => fill(limit, '3'))
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toEqual({ action: 'configure', key: 'test-only', delegation_max_turns: 3 })
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('input[type="password"]')!.required).toBe(false)
  await act(async () => button('工作偏好').click())
  expect(element.querySelector<HTMLInputElement>('#delegation-max-turns')!.value).toBe('3')
  expect(element.querySelector('[aria-label="额外思考"]')).toBeNull()
  await act(async () => button('保存设置').click())
  expect(commands.findLast(command => command.action === 'configure')).toEqual({ action: 'configure', delegation_max_turns: 3 })
})

test('a round limit remains marked on the answer and the user can choose to continue', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '读取并说明'))
  await act(async () => button('发送消息').click())
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '读取并说明', answer: '', status: 'limited',
    error: '已达到本次1轮处理上限；请查看已有结果，再决定是否继续。',
    requests: 1, input_tokens: 10, output_tokens: 5, thinking: false,
  } }))
  expect(element.querySelector('[aria-label="Myrk 的回答"]')?.textContent).toContain('已达上限 · 回答未完成')
  expect(element.textContent).toContain('请查看已有结果，再决定是否继续')
  expect(commands.filter(command => command.action === 'send')).toHaveLength(1)
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '继续说明'))
  await act(async () => button('发送消息').click())
  expect(commands.filter(command => command.action === 'send')).toHaveLength(2)
  expect(element.querySelector('[aria-label="Myrk 的回答"]')?.textContent).toContain('已达上限 · 回答未完成')
})

test('a request failure gives a next step and remains visible when the user asks again', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '请说明失败'))
  await act(async () => button('发送消息').click())
  act(() => listener({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '请说明失败', answer: '已经到达的内容', status: 'failed',
    error: '本次处理失败，已保留收到的内容。程序报告的错误种类：RuntimeError。',
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  } }))
  expect(element.textContent).toContain('请检查连接密码和网络后重试')
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '重新提问'))
  await act(async () => button('发送消息').click())
  expect(element.textContent).toContain('程序报告的错误种类：RuntimeError')
  expect(element.textContent).toContain('已经到达的内容')
  expect(commands.filter(command => command.action === 'send')).toHaveLength(2)
})

test('user searches titles, opens history, creates a clean conversation and explicitly imports a copy', async () => {
  expect(commands).toContainEqual({ action: 'list', query: '' })
  act(() => listener({ event: 'conversations', items: [
    { path: 'records/old.json', title: '采购资料' },
    { path: 'records/other.json', title: '出游计划' },
  ], unreadable: 0 }))
  const search = element.querySelector<HTMLInputElement>('input[aria-label="搜索会话标题"]')!
  act(() => fill(search, '采购'))
  expect(element.textContent).not.toContain('出游计划')
  await act(async () => button('打开会话：采购资料').click())
  expect(commands).toContainEqual({ action: 'open', path: 'records/old.json' })
  act(() => listener({ event: 'conversation', path: 'records/old.json', turns: [{
    prompt: '采购资料', answer: '以前的资料内容', status: 'completed', error: null,
    requests: 1, input_tokens: 10, output_tokens: 5, thinking: true,
  }] }))
  expect(element.querySelector('[aria-label="对话内容"]')?.textContent).toContain('以前的资料内容')
  expect(element.textContent).toContain('移除待发送附件不会清除历史资料')
  await act(async () => button('新建会话').click())
  expect(commands).toContainEqual({ action: 'new' })
  act(() => listener({ event: 'conversation', path: 'records/new.json', turns: [] }))
  expect(element.textContent).toContain('今天想完成什么？')
  expect(element.querySelector('[aria-label="对话内容"]')).toBeNull()
  expect(element.querySelector('button[aria-label="导入旧记录"]')).toBeNull()
  await act(async () => button('打开设置').click())
  await act(async () => button('记录与导入').click())
  await act(async () => button('导入旧记录').click())
  expect(imports).toBe(1)
  expect(element.textContent).toContain('复制导入')
})

test('a failed service connection keeps history visible and disables further operations', async () => {
  act(() => listener({ event: 'conversation', path: 'records/old.json', turns: [{
    prompt: '旧问题', answer: '旧回答', status: 'completed', error: null,
    requests: 1, input_tokens: 10, output_tokens: 5, thinking: false,
  }] }))
  const composer = element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!
  act(() => fill(composer, '未发送的草稿'))
  expect(button('发送消息').disabled).toBe(false)
  act(() => listener({ event: 'error', fatal: true, message: '办事程序已退出，请关闭后重新打开。' }))
  expect(element.textContent).toContain('旧回答')
  expect(button('新建会话').disabled).toBe(true)
  expect(button('打开设置').disabled).toBe(true)
  expect(composer.disabled).toBe(true)
  expect(composer.value).toBe('未发送的草稿')
  expect(button('发送消息').disabled).toBe(true)
})

test.each(['event first', 'rejection first'])('startup preserves the service failure when the snapshot also rejects (%s)', async (order) => {
  const originalCommand = window.myrk.command
  let rejectSnapshot!: (error: Error) => void
  const snapshot = new Promise<void>((_resolve, reject) => { rejectSnapshot = reject })
  window.myrk.command = command => command.action === 'snapshot' ? snapshot : originalCommand(command)
  act(() => root.render(<App key="startup-failure" />))

  const failure = '无法启动办事程序，请检查开发环境和 Python（执行办事逻辑的程序）路径。'
  const notifyFailure = () => listener({ event: 'error', message: failure, fatal: true })
  const status = () => element.querySelector('.caption [role="status"]')?.textContent
  if (order === 'event first') {
    act(notifyFailure)
    expect(status()).toBe(failure)
  }
  await act(async () => rejectSnapshot(new Error('未能完成操作')))
  if (order === 'rejection first') {
    expect(status()).toContain('与办事程序通信失败')
    act(notifyFailure)
  }

  expect(status()).toBe(failure)
  expect(element.querySelector('.main-status')?.textContent).toBe('办事程序不可用')
  expect(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')?.disabled).toBe(true)
  expect(button('发送消息').disabled).toBe(true)

  act(() => listener({ event: 'ready', path: 'records/reconnected.json' }))
  expect(status()).toContain('准备就绪')
  expect(element.querySelector('.main-status')?.textContent).toBe('准备就绪')
  expect(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')?.disabled).toBe(false)
})

test('user selects one authorized text, reads its disclosure and sends it with the question', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  await act(async () => button('选择文本资料').click())
  expect(element.textContent).toContain('E:\\Myrk\\试用资料.txt')
  expect(element.textContent).toContain('发送后将读取这份资料，并将内容提供给回答服务')
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '800元及801元需要审批吗？'))
  await act(async () => button('发送消息').click())
  expect(commands).toContainEqual({ action: 'send', path: 'records/current.json', prompt: '800元及801元需要审批吗？', document: 'E:\\Myrk\\试用资料.txt' })
  expect(button('选择文本资料').disabled).toBe(true)
  expect(element.querySelector('[aria-label="待发送资料"]')).toBeNull()
})

test('user removes the pending attachment and sends plain chat without clearing old conversation material', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => listener({ event: 'conversation', path: 'records/old.json', turns: [{
    prompt: '先前已提供资料', answer: '原有回答', status: 'completed', error: null,
    requests: 2, input_tokens: 10, output_tokens: 5, thinking: false,
  }] }))
  await act(async () => button('选择文本资料').click())
  selectedDocument = null
  await act(async () => button('选择文本资料').click())
  expect(element.querySelector('[aria-label="待发送资料"]')?.textContent).toContain('试用资料.txt')
  await act(async () => button('移除待发送资料').click())
  expect(element.querySelector('[aria-label="待发送资料"]')).toBeNull()
  expect(element.textContent).toContain('原有回答')
  expect(element.textContent).toContain('移除待发送附件不会清除历史资料')
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '继续聊'))
  await act(async () => button('发送消息').click())
  expect(commands).toContainEqual({ action: 'send', path: 'records/old.json', prompt: '继续聊' })
})

test('selection failure explains supported text without discarding the draft', async () => {
  window.myrk.selectDocument = async () => { throw new Error('os-error') }
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '保留这个问题'))
  await act(async () => button('选择文本资料').click())
  expect(element.textContent).toContain('无法选择资料')
  expect(element.textContent).toContain('.txt（普通文本）')
  expect(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')?.value).toBe('保留这个问题')
})

test('folder trust is saved separately from the answer recipient and committed only after acknowledgement', async () => {
  window.myrk.command = async value => { commands.push(value) }
  await act(async () => button('选择任务文件夹').click())
  await act(async () => button('信任文件夹').click())
  expect(commands).toContainEqual({ action: 'set_project_trust', root: 'E:\\task-materials', trusted: true })
  expect(element.querySelector('.task-scope')).toBeNull()
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')).not.toBeNull()
  act(() => listener(decodeEvent(JSON.stringify({ event: 'project_trust', root: 'E:\\task-materials', trusted: true, saved: true }))))
  expect(element.querySelector('.task-scope')?.textContent).toContain('已信任项目')
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')).toBeNull()
  act(() => listener({ event: 'configured', configured: true, provider_config: { name: '另一个回答服务', base_url: 'http://127.0.0.1:9002/v1', model: 'another-model', protocol: 'openai-chat' } }))
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '使用同一项目办新工作'))
  await act(async () => button('发送消息').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')).toBeNull()
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ prompt: '使用同一项目办新工作', scope: { root: 'E:\\task-materials', provider: 'openai-compatible' } })
})

test('a completed file action exposes a local file location without turning model links into executable actions', async () => {
  const revealed: string[] = []
  window.myrk.revealFile = async path => { revealed.push(path) }
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: '生成中文产物' }))
  act(() => listener({ event: 'action', action: { id: 'actual-file', task_id: 'task-file', kind: 'create', target: 'E:\\成果\\核对.md', status: 'completed', started_at: '2026-11-02', finished_at: '2026-11-02', output: '已新建。', error: null, full_output_path: null } }))
  const link = [...element.querySelectorAll<HTMLAnchorElement>('a')].find(link => link.getAttribute('aria-label') === '在文件夹中查看：E:\\成果\\核对.md')!
  expect(link).not.toBeNull()
  await act(async () => link.click())
  expect(revealed).toEqual(['E:\\成果\\核对.md'])
  act(() => listener({ event: 'text', text: '[来自回答的链接](file:///E:/untrusted.exe)' }))
  expect(element.querySelector('.answer-body a')?.getAttribute('href')).not.toBe('file:///E:/untrusted.exe')
  expect(revealed).toEqual(['E:\\成果\\核对.md'])
})

test('reopened project trust selects the same project without a new recipient-dependent confirmation', async () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'snapshot', active_path: 'records/current.json', configured: false, trusted_projects: ['E:\\task-materials'], conversations: [{ path: 'records/current.json', turns: [], queue: [], running: false, stopping: false }] }))))
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('dialog.folder-trust')).toBeNull()
  expect(element.querySelector('.task-scope')?.textContent).toContain('已信任项目')
  expect(commands.some(command => command.action === 'set_project_trust')).toBe(false)
})

test('a late trust acknowledgement cannot replace a newer pending folder after the first dialog was cancelled', async () => {
  window.myrk.command = async command => { commands.push(command) }
  await act(async () => button('选择任务文件夹').click())
  await act(async () => button('信任文件夹').click())
  await act(async () => button('取消文件夹确认').click())
  window.myrk.selectWorkspace = async () => 'E:\\replacement-folder'
  await act(async () => button('选择任务文件夹').click())
  await act(async () => button('信任文件夹').click())
  act(() => listener(decodeEvent(JSON.stringify({ event: 'project_trust', root: 'E:\\task-materials', trusted: true, saved: true }))))
  expect(element.querySelector('.task-scope')).toBeNull()
  expect(element.querySelector('dialog.folder-trust')?.textContent).toContain('replacement-folder')
  act(() => listener(decodeEvent(JSON.stringify({ event: 'project_trust', root: 'E:\\replacement-folder', trusted: true, saved: true }))))
  expect(element.querySelector('.task-scope')?.textContent).toContain('replacement-folder')
  expect(element.querySelector('dialog.folder-trust')).toBeNull()
})

test.each(['auto_review', 'full_access'])('project trust never replaces personal history-recipient approval in %s', async mode => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'snapshot', active_path: 'records/current.json', configured: true, trusted_projects: ['E:\\task-materials'], conversations: [{ path: 'records/current.json', turns: [], queue: [], running: false, stopping: false }] }))))
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('.task-scope')?.textContent).toContain('已信任项目')
  const approval = { mode, access: { sandbox: mode === 'full_access' ? 'full_access' : 'restricted', writable_roots: [], denied_paths: [], read_only: false, legacy_scope: false }, condition: mode === 'full_access' ? 'never' : 'on_request', reviewer: 'user', revision: 'separate-history-authorization' }
  const task = { id: 'new-recipient-task', goal: '同一项目办新工作', scope: { root: 'E:\\task-materials', allowed_actions: [], provider: 'openai-compatible', recipients: ['openai-compatible'] }, request_limit: 10, approval }
  act(() => listener(decodeEvent(JSON.stringify({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))))
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action: { id: 'exact-history-transfer', task_id: task.id, kind: 'history_reuse', target: 'http://127.0.0.1:9002/v1', status: 'awaiting_approval', started_at: '2026-11-02', finished_at: null, output: '', error: null, full_output_path: null, approval_settings: approval, approval_id: 'history-approval', impact: '把旧工作资料提供给这个新的回答服务，须由本人明确同意。' } }))))
  expect(element.textContent).toContain('等待你批准')
  expect(element.textContent).not.toContain('独立助理正在审核')
  expect(element.querySelector('dialog.folder-trust')).toBeNull()
  await act(async () => button('拒绝这次动作').click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: task.id, action_id: 'exact-history-transfer', approval_id: 'history-approval', approved: false })
})

test('user trusts a folder once and sees real action failures', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.hasAttribute('open')).toBe(true)
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.textContent).toContain('默认工作位置')
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.textContent).toContain('任务文件夹不是全部读取边界')
  expect(commands.filter(command => command.action === 'send')).toHaveLength(0)
  await act(async () => button('信任文件夹').click())
  expect(element.querySelector('.task-scope input[type="checkbox"]')).toBeNull()
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '比较两份规定'))
  await act(async () => button('发送消息').click())
  expect(commands).toContainEqual({ action: 'send', path: 'records/current.json', prompt: '比较两份规定', scope: {
    root: 'E:\\task-materials', allowed_actions: ['list', 'read', 'search', 'edit', 'calculate', 'check'], checks: [], provider: 'deepseek', recipients: ['deepseek'],
  } })
  act(() => listener({ event: 'action', action: {
    id: 'action-1', task_id: 'task-1', kind: 'read', target: '../private.txt', status: 'denied',
    started_at: '2026-11-02T00:00:00Z', finished_at: '2026-11-02T00:00:01Z', output: '',
    error: '目标在任务文件夹之外，已拒绝，未读取资料。', full_output_path: null,
  } }))
  expect(element.textContent).toContain('已拒绝')
  expect(element.textContent).toContain('未读取资料')
  expect(element.textContent).not.toContain('动作成功')
})

test('the desktop can switch conversations while work runs and sends supplements or queued new work', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '第一项工作'))
  await act(async () => button('发送消息').click())
  expect(button('新建会话').disabled).toBe(false)
  expect(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!.disabled).toBe(false)
  act(() => listener({ event: 'task', task: { id: 'task-1', goal: '第一项工作', scope: null, request_limit: null } }))
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '补充要求'))
  await act(async () => button('发送补充').click())
  expect(commands.findLast(command => command.action === 'submit_supplement')).toMatchObject({ action: 'submit_supplement', prompt: '补充要求', path: 'records/current.json', task_id: 'task-1', supplement_id: expect.any(String) })
  act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '新的工作'))
  await act(async () => button('排队新工作').click())
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ action: 'send', prompt: '新的工作', new_task: true })
  act(() => listener({ event: 'conversation', path: 'records/other.json', turns: [], queue: [], running: false, stopping: false }))
  act(() => listener({ event: 'text', path: 'records/current.json', text: '第一项后台结果' }))
  expect(element.textContent).not.toContain('第一项后台结果')
})

test('the desktop steers a selected queue item into the current work and waits for confirmation', async () => {
  const path = 'records/current.json'
  const task = { id: 'active', goal: '核对库存', scope: null, request_limit: null }
  const first = { task: { ...task, id: 'first', goal: '核对入库单' }, document: null }
  const second = { task: { ...task, id: 'second', goal: '核对补货单' }, document: null }
  act(() => listener({ event: 'started', path, prompt: task.goal, task }))
  act(() => listener({ event: 'queue', path, queue: [first, second], saved: true }))

  await act(async () => button('引导，第2条').click())
  expect(commands.at(-1)).toEqual({ action: 'steer_queued', path, task_id: task.id, queued_task_id: second.task.id })
  expect(element.querySelector('.queued-tasks')?.textContent).toContain(second.task.goal)

  act(() => listener({ event: 'queue', path, queue: [first] }))
  expect(element.querySelector('.queued-tasks')?.textContent).toContain(first.task.goal)
  expect(element.querySelector('.queued-tasks')?.textContent).not.toContain(second.task.goal)
  expect(button('引导，第1条').disabled).toBe(false)
  act(() => listener({ event: 'stopping', path }))
  expect(button('引导，第1条').disabled).toBe(true)
  act(() => listener({ event: 'finished', path, saved: true, turn: {
    prompt: task.goal, answer: '', status: 'cancelled', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task,
  } }))
  expect(element.querySelector('[aria-label^="引导，"]')).toBeNull()
  await act(async () => button('继续排队').click())
  expect(commands.at(-1)).toEqual({ action: 'start_next', path })
})

test('a saved failure can hand off to queued work without reopening over the next turn', () => {
  const path = 'records/current.json'
  const task = { id: 'inventory', goal: '核对库存', scope: null, request_limit: null }
  const following = { ...task, id: 'restock', goal: '核对补货单' }
  act(() => listener({ event: 'started', path, prompt: task.goal, task }))
  const openedBefore = commands.filter(command => command.action === 'open').length
  act(() => listener({ event: 'finished', path, saved: true, recovery: {
    task_id: task.id, required: true, can_resume: true, actions: [], message: '已核查原任务。',
  }, turn: {
    prompt: task.goal, answer: '库存核对未完成', status: 'failed', service_failure: true,
    error: '原服务错误', requests: 1, input_tokens: null, output_tokens: null, thinking: false, task,
  } }))
  expect(commands.filter(command => command.action === 'open')).toHaveLength(openedBefore)

  act(() => listener({ event: 'started', path, prompt: following.goal, task: following }))
  expect(element.textContent).toContain('库存核对未完成')
  expect(element.textContent).toContain(following.goal)
})

test('retrying a supplement reuses its ID and only its saved receipt clears the draft', async () => {
  const path = 'records/current.json'
  const task = { id: 'running-task', goal: '第一项工作', scope: null, request_limit: null }
  act(() => listener({ event: 'started', path, prompt: task.goal, task }))
  const input = element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!
  act(() => fill(input, '新要求'))
  await act(async () => button('发送补充').click())
  const submission = commands.findLast(command => command.action === 'submit_supplement')!
  act(() => listener({ event: 'error', path, task_id: task.id, supplement_id: submission.supplement_id, message: '记录未能保存' }))
  expect(input.value).toBe('新要求')
  expect(element.textContent).toContain('记录未能保存')
  await act(async () => button('发送补充').click())
  expect(commands.filter(command => command.action === 'submit_supplement')).toEqual([submission, submission])

  const receipt = { event: 'supplement' as const, path, task_id: task.id, state: 'received' as const, saved: true,
    supplement: { id: submission.supplement_id, text: submission.prompt, received_at: '2026-11-18T00:00:00Z', applied_request: null } }
  act(() => listener({ ...receipt, task_id: 'other-task' }))
  act(() => listener({ ...receipt, path: 'records/other.json' }))
  expect(input.value).toBe('新要求')
  act(() => listener(receipt))
  expect(input.value).toBe('')

  act(() => fill(input, '新要求'))
  await act(async () => button('发送补充').click())
  const newSubmission = commands.findLast(command => command.action === 'submit_supplement')!
  expect(newSubmission.supplement_id).not.toBe(submission.supplement_id)
  act(() => listener(receipt))
  expect(input.value).toBe('新要求')
  expect(element.querySelectorAll('.current-supplements li')).toHaveLength(1)
  act(() => listener({ event: 'finished', path, saved: true, turn: {
    prompt: task.goal, answer: '完成', status: 'completed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task,
  } }))
  act(() => listener({ event: 'error', path, task_id: task.id, supplement_id: newSubmission.supplement_id, message: '原工作已经结束' }))
  expect(input.value).toBe('新要求')
  expect(button('发送消息').disabled).toBe(false)

  const nextTask = { ...task, id: 'queued-task', goal: '排队后的工作' }
  act(() => listener({ event: 'started', path, prompt: nextTask.goal, task: nextTask }))
  expect(input.value).toBe('新要求')
  await act(async () => button('发送补充').click())
  const nextSubmission = commands.findLast(command => command.action === 'submit_supplement')!
  expect(nextSubmission).toMatchObject({ task_id: nextTask.id, prompt: '新要求' })
  expect(nextSubmission.supplement_id).not.toBe(newSubmission.supplement_id)
  act(() => listener({ ...receipt, supplement: { ...receipt.supplement, id: newSubmission.supplement_id } }))
  expect(input.value).toBe('新要求')
})

test('a stopped task can revoke one waiting supplement while displaying selected and revoked records', async () => {
  const path = 'records/current.json'
  const first = { id: 'remove-me', text: '撤回这一条', received_at: '2026-11-18T00:00:00Z', applied_request: null }
  const retained = { ...first, id: 'keep-me', text: '保留这一条' }
  const task = { id: 'stopped-task', goal: '暂停的工作', scope: null, request_limit: null, supplements: [first, retained] }
  act(() => listener({ event: 'conversation', path, running: false, turns: [{
    prompt: task.goal, answer: '', status: 'cancelled', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task,
  }] }))
  await act(async () => button('撤回补充，第1条').click())
  expect(commands.findLast(command => command.action === 'revoke_supplement')).toEqual({ action: 'revoke_supplement', path, task_id: task.id, supplement_id: first.id })
  expect(button('撤回补充，第2条').disabled).toBe(false)

  const revoked = { ...first, revoked_at: '2026-11-18T00:00:01Z' }
  act(() => listener({ event: 'supplement', path, task_id: task.id, state: 'revoked', saved: true, supplement: revoked }))
  const rows = element.querySelectorAll('.current-supplements li')
  expect(rows[0].textContent).toContain('已撤回')
  expect(rows[0].querySelector('button')).toBeNull()
  expect(rows[1].textContent).toContain('保留这一条')
  expect(button('撤回补充，第2条').disabled).toBe(false)

  act(() => listener({ event: 'task', path, task: { ...task, supplements: [revoked, { ...retained, selected: true }] } }))
  expect(rows[1].textContent).toContain('已选用，已进入请求准备')
  expect(rows[1].querySelector('button')).toBeNull()
})

test('a memory proposal remains approvable after its former whole-task deadline', async () => {
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{
    prompt: '主要工作', answer: '主要成果已交付。', status: 'completed', error: null, requests: 4,
    input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'task-memory', goal: '主要工作', scope: null, request_limit: 10, requests: 4, active_seconds: 0.21, active_seconds_limit: 0.2 },
    actions: [{ id: 'pending-memory', task_id: 'task-memory', kind: 'memory', target: 'personal', status: 'awaiting_approval',
      started_at: '2026-11-02T00:00:00Z', finished_at: null, output: '', error: null, full_output_path: null,
      approval_id: 'approval-memory', approval_basis: 'pending', impact: '纠正所选当前事实。', memory_deferred: true }],
  }] }))
  const approve = Array.from(element.querySelectorAll('button')).find(button => button.textContent === '批准这次动作')
  expect(approve).toBeDefined()
  await act(async () => approve!.click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: 'task-memory', action_id: 'pending-memory', approval_id: 'approval-memory', approved: true })
})

test('interrupted work shows evidence and requires verification before the user resumes', async () => {
  const recovery = { task_id: 'interrupted-task', required: true, can_resume: false, message: '意外中断后未自动续办', actions: [{ action_id: 'interrupted-edit', effect: 'unknown' as const, observation: '文件需要核查', checked_at: '2026-11-02' }] }
  act(() => listener({ event: 'conversation', path: 'records/interrupted.json', running: false, turns: [{ prompt: '原工作', answer: '部分内容', status: 'running', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false, task: { id: recovery.task_id, goal: '原工作', scope: null, request_limit: 10, requests: 2, active_seconds: 3, active_seconds_limit: 600 } }], recovery, save_failures: ['早前保存失败'], last_saved_at: '2026-11-02' }))
  expect(element.textContent).toContain('意外中断后未自动续办')
  expect(element.textContent).toContain('影响未知')
  expect(element.textContent).toContain('部分内容')
  expect(element.textContent).toContain('早前保存失败')
  expect(button('继续原工作').disabled).toBe(true)
  await act(async () => button('核查真实进展').click())
  expect(commands).toContainEqual({ action: 'verify_recovery', path: 'records/interrupted.json', task_id: recovery.task_id })
  act(() => listener({ event: 'recovery', path: 'records/interrupted.json', recovery: { ...recovery, can_resume: true, actions: [{ ...recovery.actions[0], effect: 'completed', observation: '真实文件已核对，未重复改写' }] }, saved: true }))
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  await act(async () => button('继续原工作').click())
  expect(commands).toContainEqual({ action: 'resume_task', path: 'records/interrupted.json', task_id: recovery.task_id })
})

test('a blocked attachment retry keeps interrupted work current and leaves the queue paused', async () => {
  const path = 'records/interrupted.json'
  const task = { id: 'inventory', goal: '核对库存', scope: null, request_limit: null, requests: 2 }
  const queued = { ...task, id: 'restock', goal: '独立补货工作', requests: 0 }
  const interrupted = { prompt: task.goal, status: 'running', answer: '保留的答复', error: null,
    task, requests: 2, input_tokens: null, output_tokens: null, thinking: false }
  const blocked = { ...interrupted, status: 'failed', answer: '', error: '无法读取所选资料', requests: 0 }
  const recovery = { task_id: task.id, required: true, can_resume: true, message: '意外中断后未自动续办', actions: [] }
  act(() => listener({ event: 'configured', configured: true }))
  function deliver(event: object) { act(() => listener(decodeEvent(JSON.stringify({ path, ...event })))) }
  deliver({ event: 'conversation', turns: [interrupted], queue: [{ task: queued, document: null }], recovery })
  deliver({ event: 'started', prompt: task.goal, task })
  deliver({ event: 'finished', turn: blocked, saved: true, recovery })
  expect(element.textContent).toContain(queued.goal)
  expect(element.textContent).not.toContain('继续排队')
  await act(async () => button('继续原工作').click())
  expect(commands.at(-1)).toEqual({ action: 'resume_task', path, task_id: task.id })
})

test('a verified service failure still offers independent queued work after a blocked attachment retry', async () => {
  const path = 'records/service-failure.json'
  const task = { id: 'inventory', goal: '核对库存', scope: null, request_limit: 3, requests: 2 }
  const following = { ...task, id: 'restock', goal: '独立补货工作', request_limit: null, requests: 0 }
  const failed = { prompt: task.goal, status: 'failed', service_failure: true, answer: '保留的答复', error: 'HTTP 400',
    task, requests: 2, input_tokens: null, output_tokens: null, thinking: false }
  const blocked = { ...failed, service_failure: false, answer: '', error: '无法读取所选资料', requests: 0 }
  const recovery = { task_id: task.id, required: true, can_resume: true, message: '已核查原任务', actions: [] }
  act(() => listener({ event: 'configured', configured: true }))
  function deliver(event: object) { act(() => listener(decodeEvent(JSON.stringify({ path, ...event })))) }
  deliver({ event: 'conversation', turns: [failed], queue: [{ task: following, document: null }], recovery })
  deliver({ event: 'started', prompt: task.goal, task })
  deliver({ event: 'finished', turn: blocked, saved: true, recovery })
  expect(element.textContent).toContain(following.goal)
  await act(async () => button('继续排队').click())
  expect(commands.at(-1)).toEqual({ action: 'start_next', path })
})

test('earlier failed work can be selected after the independent queue finishes and the chat reopens', async () => {
  const path = 'records/resume-earlier.json'
  const task = { id: 'stock', goal: '核对库存', scope: null, request_limit: 10, requests: 2 }
  const following = { ...task, id: 'restock', goal: '独立补货工作', request_limit: null }
  const failed = { prompt: task.goal, status: 'failed', service_failure: true, answer: '库存部分保留', error: 'HTTP 400', task, requests: 2, input_tokens: null, output_tokens: null, thinking: false }
  const completed = { prompt: following.goal, status: 'completed', answer: '补货完成', error: null, task: following, requests: 1, input_tokens: null, output_tokens: null, thinking: false }
  const recovery = { task_id: task.id, required: true, can_resume: true, message: '库存工作等待手动续办', actions: [] }
  act(() => listener({ event: 'configured', configured: true }))
  function deliver(event: object) { act(() => listener(decodeEvent(JSON.stringify({ path, ...event })))) }
  deliver({ event: 'conversation', turns: [failed], recovery })
  deliver({ event: 'started', prompt: following.goal, task: following })
  expect(element.querySelector('[aria-label="较早工作中断核查"]')).toBeNull()
  deliver({ event: 'finished', turn: completed, saved: true, recovery: null, earlier_recoveries: [recovery] })
  expect(element.textContent).toContain('补货完成')
  expect(element.querySelector('[aria-label="较早工作中断核查"]')?.textContent).toContain(task.goal)
  deliver({ event: 'conversation', turns: [failed, completed], recovery: null, earlier_recoveries: [recovery] })
  await act(async () => button('核查真实进展').click())
  expect(commands).toContainEqual({ action: 'verify_recovery', path, task_id: task.id })
  deliver({ event: 'recovery', recovery: { ...recovery, message: '已核对库存动作' }, earlier: true, saved: true })
  expect(element.querySelector('[aria-label="较早工作中断核查"]')?.textContent).toContain('已核对库存动作')
  await act(async () => button('继续原工作').click())
  expect(commands).toContainEqual({ action: 'resume_task', path, task_id: task.id })
  await act(async () => button('增加旧记录10次请求并续办原工作').click())
  expect(commands).toContainEqual({ action: 'continue_task', path, task_id: task.id, request_limit: 20 })
  deliver({ event: 'started', prompt: '续办库存', task })
  deliver({ event: 'finished', turn: { ...completed, task, prompt: '续办库存', answer: '库存完成' }, saved: true, recovery: null, earlier_recoveries: [] })
  expect(element.querySelector('[aria-label="较早工作中断核查"]')).toBeNull()
  expect(element.textContent).toContain('库存部分保留')
  expect(element.textContent).toContain('补货完成')
})

test('continued work adds new actions only to its newest turn and updates an existing proposal in its original turn', () => {
  const task = { id: 'shared-task', goal: '同一项工作', scope: null, request_limit: 12 }
  const oldAction = { id: 'old-proposal', task_id: task.id, kind: 'memory', target: '原轮待审事实', status: 'awaiting_approval' as const,
    started_at: '2026-11-02T00:00:00Z', finished_at: null, output: '', error: null, full_output_path: null, memory_deferred: true, approval_id: 'old-approval' }
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [
    { prompt: '原轮', answer: '已有成果', status: 'limited', error: null, requests: 10, input_tokens: null, output_tokens: null, thinking: false, task, actions: [oldAction] },
    { prompt: '增加额度后继续', answer: '', status: 'running', error: null, requests: 0, input_tokens: null, output_tokens: null, thinking: false, task, actions: [] },
  ], running: true }))
  act(() => listener({ event: 'action', action: { ...oldAction, id: 'new-read', kind: 'read', target: '只属于续办轮.txt', status: 'completed' } }))
  const answers = element.querySelectorAll('[aria-label="Myrk 的回答"]')
  expect(answers[0].textContent).not.toContain('只属于续办轮.txt')
  expect(answers[1].textContent).toContain('只属于续办轮.txt')
  act(() => listener({ event: 'action', action: { ...oldAction, status: 'completed', output: '原提议已处理' } }))
  expect(answers[0].textContent).toContain('原提议已处理')
  expect(answers[1].textContent).not.toContain('原提议已处理')
})

test.each(['取消文件夹确认', '关闭文件夹确认', 'Escape'])('folder confirmation leaves an existing attachment and draft unchanged when dismissed by %s', async exit => {
  await act(async () => button('选择文本资料').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '保留这个草稿'))
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.hasAttribute('open')).toBe(true)
  if (exit === 'Escape') {
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  } else {
    await act(async () => button(exit).click())
  }
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')).toBeNull()
  expect(element.querySelector('[aria-label="待发送资料"]')?.textContent).toContain('试用资料.txt')
  expect(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!.value).toBe('保留这个草稿')
  expect(element.querySelector('.task-scope')).toBeNull()
  expect(commands.some(command => command.action === 'send')).toBe(false)
})

test.each(['取消文件夹确认', '关闭文件夹确认', 'Escape'])('cancelling a replacement folder preserves the trusted scope through %s', async exit => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  await act(async () => button('选择任务文件夹').click())
  await act(async () => button('信任文件夹').click())
  window.myrk.selectWorkspace = async () => 'E:\\replacement-folder'
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.textContent).toContain('replacement-folder')
  if (exit === 'Escape') {
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  } else {
    await act(async () => button(exit).click())
  }
  expect(element.querySelector('.task-scope')?.textContent).toContain('task-materials')
  await act(async () => button('查看文件夹授权').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.textContent).toContain('E:\\task-materials')
  await act(async () => button('取消文件夹确认').click())
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '读取原目录'))
  await act(async () => button('发送消息').click())
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ scope: {
    root: 'E:\\task-materials', allowed_actions: ['list', 'read', 'search', 'edit', 'calculate', 'check'],
  } })
})

test('settings contain import and memory controls even before a password is configured', async () => {
  expect(element.querySelector('button[aria-label="导入旧记录"]')).toBeNull()
  expect(element.querySelector('button[aria-label="查看长期记忆"]')).toBeNull()
  await act(async () => button('打开设置').click())
  await act(async () => button('查看长期记忆').click())
  expect(commands).toContainEqual({ action: 'memory_list' })
  act(() => listener({ event: 'memory', facts: [], approve_writes: false, warnings: [], location: 'records/memories' }))
  expect(element.querySelector('[aria-label="长期记忆管理"]')?.textContent).toContain('尚无')
  await act(async () => button('记录与导入').click())
  await act(async () => button('导入旧记录').click())
  expect(imports).toBe(1)
  expect(commands.some(command => command.action === 'configure')).toBe(false)
})

test('closing settings discards uncommitted work preferences and preserves continuation allowance', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('[type="password"]')!, 'test-only'))
  await act(async () => button('保存设置').click())
  const configuredCount = commands.filter(command => command.action === 'configure').length
  await act(async () => button('打开设置').click())
  await act(async () => button('工作偏好').click())
  act(() => fill(element.querySelector<HTMLInputElement>('#delegation-max-turns')!, '31'))
  await act(async () => button('关闭设置').click())
  expect(commands.filter(command => command.action === 'configure')).toHaveLength(configuredCount)
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{
    prompt: '原工作', answer: '部分成果', status: 'limited', error: null, requests: 3,
    input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'continue-existing', goal: '原工作', scope: null, request_limit: 3, requests: 3, active_seconds: 8, active_seconds_limit: 10 },
  }] }))
  await act(async () => button('增加额度并继续').click())
  expect(commands).toContainEqual({ action: 'continue_task', path: 'records/current.json', task_id: 'continue-existing', request_limit: 13 })
  await act(async () => button('打开设置').click())
  await act(async () => button('工作偏好').click())
  expect(element.querySelector<HTMLInputElement>('#delegation-max-turns')!.value).toBe('250')
  expect(element.querySelector('#active-limit')).toBeNull()
})

test('a failed settings save retains the dialog and provider draft and shows its reason', async () => {
  window.myrk.command = async command => {
    commands.push(command)
    if (command.action === 'configure') listener({ event: 'error', message: '检查用保存失败，原设置保留。' })
  }
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('[type="password"]')!, 'test-only'))
  act(() => fill(element.querySelector<HTMLInputElement>('#service-model')!, 'draft-model'))
  await act(async () => button('保存设置').click())
  expect(element.querySelector('.settings')?.textContent).toContain('检查用保存失败，原设置保留。')
  expect(element.querySelector<HTMLInputElement>('#service-model')!.value).toBe('draft-model')
  expect(element.querySelector('.settings')?.textContent).not.toContain('连接成功')
})

test('an unconfigured replacement provider requires its own password', async () => {
  await act(async () => button('打开设置').click())
  act(() => fill(element.querySelector<HTMLInputElement>('[type="password"]')!, 'first-service-only'))
  await act(async () => button('保存设置').click())
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('[type="password"]')!.required).toBe(false)
  await act(async () => button('自定义兼容服务').click())
  act(() => fill(element.querySelector<HTMLInputElement>('#service-address')!, 'http://127.0.0.1:9001/v1'))
  expect(element.querySelector<HTMLInputElement>('[type="password"]')!.required).toBe(true)
  expect(element.querySelector<HTMLInputElement>('[type="password"]')!.placeholder).not.toContain('已设置')
  expect(element.querySelector<HTMLInputElement>('[type="password"]')!.value).toBe('')
})

test('import errors remain visible in settings and a successful import opens the copied conversation', async () => {
  window.myrk.importConversation = async () => { throw new Error('synthetic-import-failure') }
  await act(async () => button('打开设置').click())
  await act(async () => button('记录与导入').click())
  await act(async () => button('导入旧记录').click())
  expect(element.querySelector('.settings')?.textContent).toContain('未能导入记录')
  expect(element.querySelector('[aria-label="对话内容"]')).toBeNull()
  window.myrk.importConversation = async () => listener({ event: 'conversation', path: 'records/imported-copy.json', turns: [{
    prompt: '导入的问题', answer: '导入副本中的回答', status: 'completed', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  }] })
  await act(async () => button('导入旧记录').click())
  expect(element.querySelector('.settings')).toBeNull()
  expect(element.querySelector('[aria-label="对话内容"]')?.textContent).toContain('导入副本中的回答')
  expect(commands.some(command => command.action === 'configure')).toBe(false)
})

test('a real settings acknowledgement remains authoritative after an unrelated error without resetting project trust', async () => {
  const original = { name: '兼容回答服务', base_url: 'http://127.0.0.1:9001/v1', model: 'answer-model', protocol: 'openai-chat' as const }
  act(() => listener({ event: 'snapshot', active_path: 'records/current.json', configured: true, provider_config: original,
    request_limit: null, delegation_max_turns: 250, active_seconds_limit: 600, thinking: false,
    conversations: [{ path: 'records/current.json', turns: [], queue: [], running: false, stopping: false }],
  }))
  await act(async () => button('选择任务文件夹').click())
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')?.textContent).toContain('项目信任会独立保存')
  await act(async () => button('信任文件夹').click())
  window.myrk.command = async command => { commands.push(command) }
  const target = { ...original, base_url: 'http://127.0.0.1:9002/v1' }
  await act(async () => button('打开设置').click())
  await act(async () => button('自定义兼容服务').click())
  act(() => {
    fill(element.querySelector<HTMLInputElement>('#service-name')!, target.name)
    fill(element.querySelector<HTMLInputElement>('#service-address')!, target.base_url)
    fill(element.querySelector<HTMLInputElement>('#service-model')!, target.model)
    fill(element.querySelector<HTMLInputElement>('[type="password"]')!, 'replacement-service-only')
  })
  await act(async () => button('工作偏好').click())
  act(() => fill(element.querySelector<HTMLInputElement>('#delegation-max-turns')!, '17'))
  await act(async () => button('保存设置').click())
  act(() => listener({ event: 'error', message: '另一份记录暂时无法读取。' }))
  act(() => listener({ event: 'configured', provider_config: target, request_limit: null, delegation_max_turns: 17, active_seconds_limit: 120, thinking: true }))
  expect(element.querySelector('.settings')).toBeNull()
  act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!, '按原范围继续'))
  const sentBefore = commands.filter(command => command.action === 'send').length
  await act(async () => button('发送消息').click())
  expect(commands.filter(command => command.action === 'send')).toHaveLength(sentBefore + 1)
  expect(element.querySelector('dialog[aria-label="信任此文件夹吗？"]')).toBeNull()
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [], running: false }))
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('#service-address')!.value).toBe(target.base_url)
  await act(async () => button('工作偏好').click())
  expect(element.querySelector<HTMLInputElement>('#delegation-max-turns')!.value).toBe('17')
  expect(element.querySelector('#active-limit')).toBeNull()
  expect(element.querySelector('[aria-label="额外思考"]')).toBeNull()
  await act(async () => button('关闭设置').click())
  expect(commands.findLast(command => command.action === 'send')).toMatchObject({ scope: { provider: 'openai-compatible', recipients: ['openai-compatible'] } })
})

test('a reopened screen displays actual provider and work preferences from its snapshot', async () => {
  act(() => root.unmount())
  root = createRoot(element)
  act(() => root.render(<App />))
  const current = { name: '已保存的兼容服务', base_url: 'http://127.0.0.1:9010/v1', model: 'actual-answer-model', protocol: 'openai-chat' as const }
  act(() => listener({ event: 'snapshot', active_path: 'records/current.json', configured: true, provider_config: current,
    request_limit: null, delegation_max_turns: 23, active_seconds_limit: 345, thinking: true,
    conversations: [{ path: 'records/current.json', turns: [], queue: [], running: false, stopping: false }],
  }))
  await act(async () => button('打开设置').click())
  expect(element.querySelector<HTMLInputElement>('#service-name')!.value).toBe(current.name)
  expect(element.querySelector<HTMLInputElement>('#service-address')!.value).toBe(current.base_url)
  expect(element.querySelector<HTMLInputElement>('#service-model')!.value).toBe(current.model)
  expect(element.querySelector<HTMLInputElement>('[type="password"]')!.required).toBe(false)
  await act(async () => button('工作偏好').click())
  expect(element.querySelector<HTMLInputElement>('#delegation-max-turns')!.value).toBe('23')
  expect(element.querySelector('#active-limit')).toBeNull()
  expect(element.querySelector('[aria-label="额外思考"]')).toBeNull()
  expect(commands.some(command => command.action === 'configure')).toBe(false)
})

test('legacy request receipts show cache usage as unknown without replacing known input usage', () => {
  act(() => listener(decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '读取旧记录', answer: '旧成果仍可查看', status: 'completed', error: null, requests: 1, input_tokens: 20, output_tokens: 5, thinking: false,
    request_attempts: [{ number: 1, actor: 'main', model: 'saved-model', provider: 'https://api.deepseek.com', delegation_id: null, status: 'completed', http_status: null, error: null, decision: '服务回应已收到。', input_tokens: 20, output_tokens: 5 }],
  } }))))
  expect(element.textContent).toContain('输入20、输出5')
  expect(element.textContent).toContain('缓存观测：未知（旧记录未保存）')
})

test('request cache observations preserve measured zero and unknown fields through the desktop protocol', () => {
  const observation = {
    provider: 'deepseek', endpoint: 'https://api.deepseek.com', protocol: 'deepseek', model: 'deepseek-flash', layout_version: null,
    input_tokens: 100, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: null,
    input_source: 'usage.prompt_tokens', output_source: 'usage.completion_tokens', cache_read_source: 'usage.prompt_tokens_details.cached_tokens', cache_write_source: null,
    usage_present: true, usage_status: 'partial', raw_usage: { prompt_tokens: 100, 'prompt_tokens_details.cached_tokens': 0 },
    field_presence: { 'usage.prompt_tokens': true, 'usage.prompt_tokens_details.cached_tokens': true, 'usage.cache_write_tokens': false },
    sdk_usage: { input_tokens: 100, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0 }, conflicts: [], elapsed_seconds: 1.25, first_output_seconds: 0.2,
    wire: { body_sha256: 'body-digest', body_bytes: 800, system_sha256: 'system-digest', tools_sha256: 'tools-digest', messages_sha256: 'messages-digest', message_count: 2, tool_count: 3, message_hashes: ['first-message-digest', 'second-message-digest'], change_reasons: ['first_request'] },
  }
  const decoded = decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '查看此次用量', answer: '完成', status: 'completed', error: null, requests: 1, input_tokens: 100, output_tokens: 5, thinking: false,
    request_attempts: [{ number: 1, own_number: 1, actor: 'main', model: 'deepseek-flash', provider: 'https://api.deepseek.com', delegation_id: null, status: 'completed', http_status: null, error: null, decision: '服务回应已收到。', input_tokens: 100, output_tokens: 5, observation }],
  } }))
  expect(decoded).toMatchObject({ turn: { request_attempts: [{ observation }] } })
  act(() => listener(decoded))
  const details = element.querySelector('[aria-label="此次请求缓存观测"]')!
  expect(details.textContent).toContain('输入总量100；缓存读取0；缓存写入未知')
  expect(details.textContent).toContain('缓存读取已包含在输入总量中，不另行相加')
  expect(details.textContent).toContain('协议：DeepSeek Chat')
  expect(details.textContent).toContain('缓存读取来源：usage.prompt_tokens_details.cached_tokens')
  expect(details.textContent).toContain('请求耗时：1.250秒；首段输出：0.200秒')
  expect(details.textContent).toContain('usage.cache_write_tokens：缺失')
  expect(details.textContent).toContain('body-digest')
  expect(details.textContent).toContain('消息2条；工具3个；正文800字节')
})

test('user sees the actual extension request before approving and after reopening its action record', async () => {
  const task = { id: 'extension-task', goal: '计算统计表', scope: null, request_limit: 10 }
  const action = { id: 'extension-action', task_id: task.id, kind: 'calculate', target: '统计.csv', status: 'awaiting_approval' as const,
    started_at: '2026-11-09', finished_at: null, output: '', error: null, full_output_path: null, approval_id: 'extension-approval',
    tool_name: 'calculate_table', tool_arguments: { path: '统计.csv', column: '金额', filters: { active: true }, groups: ['部门'] }, tool_source: 'extension', tool_schema_version: 'schema-7' }
  act(() => listener({ event: 'started', path: 'records/current.json', prompt: task.goal, task }))
  act(() => listener(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action }))))
  expect(element.textContent).toContain('实际工具：calculate_table')
  expect(element.textContent).toContain('调用来源：扩展工具入口')
  expect(element.textContent).toContain('工具定义版本（用于核对参数格式）：schema-7')
  expect(JSON.parse(element.querySelector('[aria-label="实际调用参数"]')!.textContent!)).toEqual({ path: '统计.csv', column: '金额', filters: { active: true }, groups: ['部门'] })
  await act(async () => button('批准这次动作').click())
  expect(commands).toContainEqual({ action: 'approve_action', path: 'records/current.json', task_id: task.id, action_id: action.id, approval_id: action.approval_id, approved: true })
  act(() => listener(decodeEvent(JSON.stringify({ event: 'conversation', path: 'records/current.json', turns: [{
    prompt: task.goal, answer: '合计完成', status: 'completed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task,
    actions: [{ ...action, status: 'completed', finished_at: '2026-11-09T01:00:01Z', output: '合计：125.5' }],
  }] }))))
  expect(element.textContent).toContain('实际工具：calculate_table')
  expect(element.textContent).toContain('调用来源：扩展工具入口')
  expect(element.textContent).toContain('工具定义版本（用于核对参数格式）：schema-7')
  expect(JSON.parse(element.querySelector('[aria-label="实际调用参数"]')!.textContent!)).toEqual({ path: '统计.csv', column: '金额', filters: { active: true }, groups: ['部门'] })
  expect(element.textContent).toContain('合计：125.5')
})
