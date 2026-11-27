import { join, resolve } from 'node:path'
import { readFile } from 'node:fs/promises'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, test } from 'vitest'
import { PythonService } from '../main/python-service'
import type { ServiceEvent } from '../shared/protocol'
import { App } from './App'
import { testDirectory } from '../../tests/run-context.mjs'

test('a real compacted session reopens in the desktop and reads the original tool evidence across pages', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const folder = await testDirectory('desktop-compaction-')
  const records = join(folder, 'records')
  const path = join(records, 'compacted.json')
  const events: ServiceEvent[] = []
  const listeners = new Set<(event: ServiceEvent) => void>()
  let service: PythonService
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: command => command.action === 'get_connection_storage' ? Promise.resolve() : service.send(command),
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    selectWorkspace: async () => null, selectDocument: async () => null, importConversation: async () => {},
    copy: async () => {}, revealFile: async () => { throw new Error('Source reading must not reveal an arbitrary file') }, quit: async () => {},
  }
  const element = document.createElement('div')
  document.body.append(element)
  const root = createRoot(element)
  service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: records, module: 'tests.desktop_compaction_fixture' }, event => {
    events.push(event)
    act(() => { for (const listener of listeners) listener(event) })
  })
  act(() => root.render(<App />))
  const button = (label: string) => Array.from(element.querySelectorAll('button')).find(button => button.textContent === label)!
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    await act(async () => service.send({ action: 'open', path }))
    await expect.poll(() => element.textContent).toContain('已整理较早记录')
    expect(element.textContent).toContain('原工作尚待核对，拒绝和原始证据保留。')
    expect(element.textContent).toContain('用途：整理较早记录')
    const snapshot = events.findLast(event => event.event === 'conversation')!
    if (snapshot.event !== 'conversation') throw new Error('Missing conversation')
    expect(snapshot.compactions).toHaveLength(1)
    expect(snapshot.compactions![0].status).toBe('completed')
    expect(snapshot.turns[0]).not.toHaveProperty('compactions')
    const saved = await readFile(path, 'utf8')
    expect(JSON.parse(saved).turns[0].compactions[0]).toHaveProperty('view')
    const select = element.querySelector<HTMLSelectElement>('[aria-label^="整理来源："]')!
    const option = Array.from(select.options).find(option => option.textContent === '消息3／部件1')!
    await act(async () => { select.value = option.value; select.dispatchEvent(new Event('change', { bubbles: true })) })
    await act(async () => button('回查整理原文').click())
    await expect.poll(() => element.querySelector('[aria-label="整理来源原文"]')?.textContent).toContain('original-tool-evidence')
    expect(element.querySelector('[aria-label="整理来源原文"]')!.textContent).not.toContain('middle-source-refusal-937')
    await act(async () => button('下一段原文').click())
    await expect.poll(() => element.querySelector('[aria-label="整理来源原文"]')?.textContent).toContain('middle-source-refusal-937')
    expect(element.querySelector('[aria-label="整理来源原文"] a')).toBeNull()
    await service.send({ action: 'get_compaction_evidence', path, summary_id: 'not-registered', message_index: 2, part_index: 0, start: 0 })
    await expect.poll(() => events.some(event => event.event === 'compaction_evidence' && event.error !== null)).toBe(true)
    expect(await readFile(path, 'utf8')).toBe(saved)
    await act(async () => service.send({ action: 'snapshot' }))
    await expect.poll(() => events.some(event => event.event === 'snapshot' && event.active_path === path)).toBe(true)
    const current = events.findLast(event => event.event === 'snapshot' && event.active_path === path)!
    if (current.event !== 'snapshot') throw new Error('Missing snapshot')
    expect(current.conversations.find(item => item.path === path)?.compactions?.[0].status).toBe('completed')
    expect(events.filter(event => event.event === 'error')).toEqual([])
  } finally {
    await service.shutdown()
    act(() => root.unmount())
    element.remove()
  }
}, 30000)
