import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, test } from 'vitest'
import { SettingsDialog } from './SettingsDialog'
import type { ProviderConfig } from '../shared/protocol'
import { decodeCommand } from '../shared/decode'

test('model capacity and automatic compaction preferences survive the settings command', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const submissions: unknown[] = []
  try {
    act(() => root.render(<SettingsDialog
      value={{ providerConfig: { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-pro', protocol: 'google' }, delegationMaxTurns: 250, approvalMode: 'request_approval' }}
      configured={true} busy={false} error="" memoryState={null} modelCatalog={null}
      connectionStorage={null} onForget={() => {}} onFetchCatalog={async () => {}}
      onSave={value => submissions.push(decodeCommand({ action: 'configure', provider_config: value.providerConfig }))}
      onApprovalSave={() => {}} onClose={() => {}} onMemoryList={() => {}} onMemorySettings={() => {}} onMemoryChange={() => {}} onImport={() => {}}
    />))
    const window = element.querySelector<HTMLInputElement>('#context-window')
    expect(window, 'Gemini has the same model capacity controls as other services').not.toBeNull()
    const fill = (id: string, value: string) => {
      const input = element.querySelector<HTMLInputElement>(id)!
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    act(() => { fill('#context-window', '128000'); fill('#input-token-limit', '96000'); fill('#output-token-reserve', '4096') })
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="工作偏好"]')!.click())
    await act(async () => element.querySelector<HTMLInputElement>('[aria-label="自动整理较早记录"]')!.click())
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="保存设置"]')!.click())
    expect(submissions).toMatchObject([{ provider_config: {
      context_window_override: 128000, input_token_limit_override: 96000,
      output_token_reserve: 4096, auto_compaction: false,
    } }])
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test('saved connections can switch models with a blank password and be forgotten without revealing a secret', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const provider = { name: 'Service A', base_url: 'https://service-a.example/v1/', model: 'model-a', protocol: 'openai-chat' as const }
  const saved = { ...provider, base_url: 'https://service-a.example/v1', model: 'previous-model' }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const submissions: { model: string; key: string }[] = []
  const forgotten: ProviderConfig[] = []
  try {
    act(() => root.render(<SettingsDialog
      value={{ providerConfig: provider, delegationMaxTurns: 250, approvalMode: 'request_approval' }}
      configured={false} busy={false} error="" memoryState={null} modelCatalog={null}
      connectionStorage={{ available: [saved], saved: [saved], error: null }} onForget={value => { forgotten.push(value) }}
      onFetchCatalog={async () => {}} onSave={(value, key) => { submissions.push({ model: value.providerConfig.model, key }) }} onApprovalSave={() => {}} onClose={() => {}}
      onMemoryList={() => {}} onMemorySettings={() => {}} onMemoryChange={() => {}} onImport={() => {}}
    />))
    const password = element.querySelector<HTMLInputElement>('#connection-password')!
    expect(password.value).toBe('')
    expect(password.required).toBe(false)
    expect(element.textContent).toContain('已保存到本机')
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="保存设置"]')!.click())
    expect(submissions).toEqual([{ model: 'model-a', key: '' }])
    await act(async () => Array.from(element.querySelectorAll('button')).find(button => button.textContent === '忘记此连接密码')!.click())
    expect(forgotten).toEqual([provider])
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test('work settings leave reasoning control in the composer instead of keeping an extra thinking switch', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  try {
    act(() => root.render(<SettingsDialog
      value={{ providerConfig: { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-pro', protocol: 'google' }, delegationMaxTurns: 250, approvalMode: 'request_approval' }}
      configured={true} busy={false} error="" memoryState={null} modelCatalog={null}
      connectionStorage={null} onForget={() => {}}
      onFetchCatalog={async () => {}} onSave={() => {}} onApprovalSave={() => {}} onClose={() => {}}
      onMemoryList={() => {}} onMemorySettings={() => {}} onMemoryChange={() => {}} onImport={() => {}}
    />))
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="工作偏好"]')!.click())
    expect(element.querySelector('[aria-label="额外思考"]')).toBeNull()
    expect(element.textContent).not.toContain('额外思考')
    expect(element.querySelector('#delegation-max-turns')).not.toBeNull()
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})
