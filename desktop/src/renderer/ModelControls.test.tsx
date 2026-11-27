import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, test } from 'vitest'
import { ModelControls } from './ModelControls'

test('the composer shows the model and offers only its discrete reasoning choices', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const changed: string[][] = []
  const selection = { provider_config: { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' as const }, reasoning: 'high' }
  const model = { id: 'deepseek-flash', name: 'DeepSeek Flash', capability: 'chat' as const, reasoning_options: [{ id: 'low', label: '低' }, { id: 'high', label: '高' }, { id: 'max', label: '最高' }], default_reasoning: 'high', reasoning_note: null, tools_supported: true, unavailable_reason: null }
  try {
    act(() => root.render(<ModelControls selection={selection} models={[model]} disabled={false} busy={false} legacy={false} error="" onChange={(id, effort) => changed.push([id, effort])} onRefresh={() => {}} />))
    expect(element.querySelector('[aria-label="选择模型"]')?.textContent).toContain('DeepSeek Flash')
    const opener = element.querySelector<HTMLButtonElement>('[aria-label="选择推理强度"]')!
    opener.focus()
    await act(async () => opener.click())
    const slider = document.querySelector<HTMLInputElement>('[aria-label="推理强度"]')!
    expect(slider.max).toBe('2')
    expect(slider.step).toBe('1')
    expect(slider.getAttribute('aria-valuetext')).toBe('高')
    expect(document.activeElement).toBe(slider)
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(slider, '2'); slider.dispatchEvent(new Event('input', { bubbles: true })) })
    expect(changed).toEqual([['deepseek-flash', 'max']])
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="恢复初始推理强度"]')!.click())
    expect(changed.at(-1)).toEqual(['deepseek-flash', 'high'])
    await act(async () => slider.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.querySelector('[aria-label="推理强度"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="选择模型"]')!.click())
    await act(async () => document.querySelector<HTMLButtonElement>('[role="option"]')!.click())
    expect(changed.at(-1)).toEqual(['deepseek-flash', 'high'])
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test('Gemini dynamic reasoning is selected and restored as an actual slider choice', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const changed: string[][] = []
  const selection = { provider_config: { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-pro', protocol: 'google' as const }, reasoning: 'dynamic' }
  const model = { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', capability: 'chat' as const, reasoning_options: [{ id: 'dynamic', label: '动态' }, { id: 'low', label: '低' }, { id: 'high', label: '高' }], default_reasoning: 'dynamic', reasoning_note: null, tools_supported: true, unavailable_reason: null }
  try {
    act(() => root.render(<ModelControls selection={selection} models={[model]} disabled={false} busy={false} legacy={false} error="" onChange={(id, effort) => changed.push([id, effort])} onRefresh={() => {}} />))
    expect(element.querySelector('[aria-label="选择推理强度"]')?.textContent).toContain('动态')
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="选择推理强度"]')!.click())
    const panel = document.querySelector('[aria-label="调整推理强度"]')!
    const slider = panel.querySelector<HTMLInputElement>('[aria-label="推理强度"]')!
    expect(slider.value).toBe('0')
    expect(slider.getAttribute('aria-valuetext')).toBe('动态')
    expect(panel.textContent).not.toContain('默认')
    expect(panel.textContent).not.toContain('关闭')
    await act(async () => panel.querySelector<HTMLButtonElement>('[aria-label="恢复初始推理强度"]')!.click())
    expect(changed).toEqual([['gemini-2.5-pro', 'dynamic']])
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test('an unknown connection explains its missing reasoning choices without a slider', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const selection = { provider_config: { name: 'Local', base_url: 'http://127.0.0.1:9001/v1', model: 'local-chat', protocol: 'openai-chat' as const }, reasoning: 'default' }
  const model = { id: 'local-chat', name: 'Local chat', capability: 'unknown' as const, reasoning_options: [], default_reasoning: 'default', reasoning_note: '此连接尚无可确认的推理档位。', tools_supported: null, unavailable_reason: null }
  try {
    act(() => root.render(<ModelControls selection={selection} models={[model]} disabled={false} busy={false} legacy={false} error="" onChange={() => { throw new Error('No selection should be sent') }} onRefresh={() => {}} />))
    expect(element.querySelector('[aria-label="选择推理强度"]')?.textContent).toContain('不可调')
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="选择推理强度"]')!.click())
    const panel = document.querySelector('[aria-label="调整推理强度"]')!
    expect(panel.textContent).toContain('此连接尚无可确认的推理档位。')
    expect(panel.querySelector('input[type="range"]')).toBeNull()
    expect(panel.querySelector('[aria-label="恢复初始推理强度"]')).toBeNull()
    expect(document.activeElement).toBe(panel)
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test('an unconfirmed provider default requires choosing a known level before showing a selected slider', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const changed: string[][] = []
  const selection = { provider_config: { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: 'gemini-3.5-flash', protocol: 'google' as const }, reasoning: 'default' }
  const model = { id: 'gemini-3.5-flash', name: 'Gemini 3.5 Flash', capability: 'chat' as const, reasoning_options: [{ id: 'low', label: '低' }, { id: 'high', label: '高' }], default_reasoning: 'default', reasoning_note: '此型号的服务默认档位尚未确认，可手动选择。', tools_supported: true, unavailable_reason: null }
  const props = { models: [model], disabled: false, busy: false, legacy: false, error: '', onChange: (id: string, effort: string) => changed.push([id, effort]), onRefresh: () => {} }
  try {
    act(() => root.render(<ModelControls {...props} selection={selection} />))
    expect(element.querySelector('[aria-label="选择推理强度"]')?.textContent).toContain('待选择')
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="选择推理强度"]')!.click())
    const panel = document.querySelector('[aria-label="调整推理强度"]')!
    expect(panel.querySelector('input[type="range"]')).toBeNull()
    expect(panel.textContent).toContain('此型号的服务默认档位尚未确认')
    await act(async () => panel.querySelector<HTMLButtonElement>('.reasoning-initial-choice')!.click())
    expect(changed).toEqual([['gemini-3.5-flash', 'low']])
    act(() => root.render(<ModelControls {...props} selection={{ ...selection, reasoning: 'low' }} />))
    expect(panel.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe('0')
    expect(panel.querySelector<HTMLInputElement>('input[type="range"]')?.getAttribute('aria-valuetext')).toBe('低')
    await act(async () => panel.querySelector<HTMLButtonElement>('[aria-label="恢复初始推理强度"]')!.click())
    expect(changed.at(-1)).toEqual(['gemini-3.5-flash', 'default'])
    act(() => root.render(<ModelControls {...props} selection={selection} />))
    expect(element.querySelector('[aria-label="选择推理强度"]')?.textContent).toContain('待选择')
    expect(panel.querySelector('input[type="range"]')).toBeNull()
  } finally {
    act(() => root.unmount())
    element.remove()
  }
})

test.each([{ top: 180, maxHeight: 160, bottom: 428 }, { top: 60, maxHeight: 492, bottom: null }])('a long model menu stays within the available viewport at trigger top $top', async ({ top, maxHeight, bottom }) => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const viewport = Object.getOwnPropertyDescriptor(window, 'innerHeight')!
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 600 })
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  const selection = { provider_config: { name: 'Local', base_url: 'http://127.0.0.1:9001/v1', model: 'chat-0', protocol: 'openai-chat' as const }, reasoning: 'default' }
  const models = Array.from({ length: 30 }, (_, index) => ({ id: `chat-${index}`, name: `Chat model ${index}`, capability: 'chat' as const, reasoning_options: [], default_reasoning: 'default', reasoning_note: '此连接未提供推理档位。', tools_supported: true, unavailable_reason: null }))
  try {
    act(() => root.render(<ModelControls selection={selection} models={models} disabled={false} busy={false} legacy={false} error="" onChange={() => {}} onRefresh={() => {}} />))
    element.querySelector<HTMLDivElement>('.model-controls')!.getBoundingClientRect = () => new DOMRect(300, top, 200, 28)
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="选择模型"]')!.click())
    const panel = document.querySelector<HTMLDivElement>('.model-list-panel')!
    expect(panel.style.maxHeight).toBe(`${maxHeight}px`)
    expect(panel.querySelectorAll('[role="option"]')).toHaveLength(30)
    if (bottom !== null) expect(panel.style.bottom).toBe(`${bottom}px`)
    else expect(panel.style.top).toBe('96px')
  } finally {
    act(() => root.unmount())
    element.remove()
    Object.defineProperty(window, 'innerHeight', viewport)
  }
})
