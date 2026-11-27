import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, test } from 'vitest'
import { PythonService } from '../main/python-service'
import type { ServiceEvent } from '../shared/protocol'
import { App } from './App'
import { testDirectory } from '../../tests/run-context.mjs'

test('the actual screen saves, views, corrects and deletes a fact and shows deletion limits', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('memory-ui-')
  const events: ServiceEvent[] = []
  const listeners = new Set<(event: ServiceEvent) => void>()
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => service.send(command), subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => null, selectDocument: async () => null, importConversation: async () => {}, copy: async () => {}, revealFile: async () => {}, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
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
  const finished = () => events.filter(event => event.event === 'finished').length
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await act(async () => button('打开设置').click())
    act(() => fill(element.querySelector<HTMLInputElement>('input[type="password"]')!, 'memory-controlled'))
    await act(async () => element.querySelector<HTMLFormElement>('.settings form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    await expect.poll(() => events.some(event => event.event === 'configured')).toBe(true)
    act(() => fill(element.querySelector<HTMLTextAreaElement>('textarea[aria-label="输入消息"]')!, '我喜欢简短的中文回复。'))
    await act(async () => button('发送消息').click())
    await expect.poll(finished).toBe(1)
    await act(async () => button('打开设置').click())
    await act(async () => button('查看长期记忆').click())
    await expect.poll(() => element.querySelector('[aria-label="长期记忆管理"]')?.textContent).toContain('用户喜欢简短的中文回复。')
    const factsPath = join(folder, 'records', 'memories', 'facts.json')
    const id = JSON.parse(await readFile(factsPath, 'utf8')).facts[0].id as string
    await act(async () => button(`纠正笔记：${id}`).click())
    act(() => fill(element.querySelector<HTMLTextAreaElement>('[aria-label="纠正后的事实"]')!, '用户喜欢详细的中文回复。'))
    await act(async () => button('保存事实纠正').click())
    await expect.poll(finished).toBe(2)
    expect(JSON.parse(await readFile(factsPath, 'utf8')).facts[0].text).toBe('用户喜欢详细的中文回复。')
    await act(async () => button('打开设置').click())
    await act(async () => button('查看长期记忆').click())
    await expect.poll(() => element.querySelector('[aria-label="长期记忆管理"]')?.textContent).toContain('用户喜欢详细的中文回复。')
    expect(element.querySelector('[aria-label="长期记忆管理"]')?.textContent).toContain('待审提议可能保存原文')
    await act(async () => button(`删除笔记：${id}`).click())
    await expect.poll(() => element.textContent).toContain('旧聊天、摘要、备份、导出和其他待审提议保留')
    await expect.poll(() => Array.from(element.querySelectorAll('button')).some(button => button.textContent === '批准这次动作')).toBe(true)
    await act(async () => Array.from(element.querySelectorAll('button')).find(button => button.textContent === '批准这次动作')!.click())
    await expect.poll(finished).toBe(3)
    expect(JSON.parse(await readFile(factsPath, 'utf8')).facts).toEqual([])
    expect(await readFile(events.find(event => event.event === 'finished')!.path!, 'utf8')).toContain('用户喜欢简短的中文回复。')
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 20000)
