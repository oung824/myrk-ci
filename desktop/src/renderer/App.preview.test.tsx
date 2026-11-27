import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, test } from 'vitest'
import type { DeliveredArtifact, ServiceEvent, Turn } from '../shared/protocol'
import type { ArtifactPreviewResult } from '../shared/artifact-preview'
import { App } from './App'

let root: Root
let element: HTMLDivElement
let listener: (event: ServiceEvent) => void
let current: ArtifactPreviewResult
let released: string[]
const artifact: DeliveredArtifact = { id: 'report-artifact', task_id: 'report-task', source_action_id: 'write-report', file_change_id: 'report-change', delegation_id: null, source_record_path: 'records/current.json', name: 'report.md', path: 'E:\\reports\\report.md', change_kind: 'modified', delivered_sha256: 'delivered', size_bytes: 16, delivered_at: '2026-11-14T01:00:00Z' }
const turn: Turn = { prompt: '交付报告', answer: '请查看成果。![模型图片](file:///E:/private.png)', status: 'completed', error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task: { id: 'report-task', goal: '交付报告', scope: null, request_limit: null }, artifacts: [artifact] }

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  released = []
  current = { status: 'ready', conversationId: 'records/current.json', artifactId: artifact.id, snapshotId: 'snapshot-one', name: artifact.name, sha256: 'delivered', deliveryState: 'unchanged', content: { kind: 'text', text: '# 完整当前正文\n\n末尾的句子。', format: 'markdown', language: 'md', history: { status: 'available', actionId: 'write-report', beforeText: '原来正文', afterText: '# 完整当前正文\n\n末尾的句子。' } } }
  window.myrk = {
    command: async () => {}, subscribe: receive => { listener = receive; return () => {} }, copy: async () => {}, revealFile: async () => {}, importConversation: async () => {}, selectDocument: async () => null, selectWorkspace: async () => null, quit: async () => {},
    openArtifactPreview: async request => { expect(request).toMatchObject({ conversationId: 'records/current.json', artifactId: artifact.id }); return current },
    closeArtifactPreview: async request => { released.push('snapshotId' in request ? request.snapshotId : request.requestId) }, revealArtifact: async () => {},
  }
  element = document.createElement('div'); document.body.append(element); root = createRoot(element)
  act(() => root.render(<App />))
  act(() => listener({ event: 'ready', path: 'records/current.json' }))
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [turn] }))
})
afterEach(() => { act(() => root.unmount()); element.remove() })
function button(label: string): HTMLButtonElement {
  const found = [...element.querySelectorAll('button')].find(item => item.getAttribute('aria-label') === label || item.textContent === label)
  if (!found) throw new Error(`找不到按钮：${label}`)
  return found
}

test('a reader opens complete artifact text and a real action comparison, then explicitly refreshes', async () => {
  await act(async () => button('查看会话成果').click())
  await act(async () => button('预览 report.md').click())
  expect(element.querySelector('[aria-label="切换会话成果"]')).not.toBeNull()
  expect(element.querySelector('[aria-label="本会话已交付文件"]')).toBeNull()
  expect(element.querySelector('[aria-label="成果全文"]')!.textContent).toContain('末尾的句子。')
  expect(element.querySelectorAll('img')).toHaveLength(0)
  const information = element.querySelector<HTMLDetailsElement>('.artifact-metadata')!
  expect(information.open).toBe(false)
  await act(async () => information.querySelector('summary')!.click())
  expect(information.open).toBe(true)
  expect(information.textContent).toContain('这里是打开时的固定内容。刷新时重新读取。')
  expect(information.textContent).toContain('对应真实动作：write-report')
  await act(async () => information.querySelector('summary')!.click())
  await act(async () => button('修改对照').click())
  expect(element.querySelector('[aria-label="修改前"]')!.textContent).toBe('原来正文')
  expect(element.querySelector('[aria-label="修改后"]')!.textContent).toContain('完整当前正文')
  current = { ...current, status: 'ready', snapshotId: 'snapshot-two', sha256: 'later', deliveryState: 'changed', content: { kind: 'text', text: '刷新后全文', format: 'plain', language: null, history: { status: 'unavailable', message: '当前文件后来又有变化。' } } }
  act(() => listener({ event: 'artifacts', path: 'records/current.json', task_id: 'report-task', artifacts: [artifact], turn }))
  expect(element.textContent).not.toContain('刷新后全文')
  await act(async () => button('刷新预览').click())
  expect(element.textContent).toContain('文件在交付后已有变化')
  expect(element.querySelector('[aria-label="成果全文"]')!.textContent).toBe('刷新后全文')
  expect(released).toHaveLength(1)
  await act(async () => button('返回聊天').click())
  expect(released).toHaveLength(2)
})

test('an image overlay pauses a following reader during streaming and restores the trigger and original position', async () => {
  const image = { ...artifact, name: 'diagram.png' }
  current = { status: 'ready', conversationId: 'records/current.json', artifactId: artifact.id, snapshotId: 'image-snapshot', name: image.name, sha256: 'image', deliveryState: 'unchanged', content: { kind: 'image', mimeType: 'image/png', base64: 'iVBORw0KGgo=', width: 640, height: 480 } }
  await act(async () => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [{ ...turn, status: 'running', artifacts: [image] }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  Object.defineProperties(conversation, { scrollHeight: { configurable: true, value: 1800 }, clientHeight: { configurable: true, value: 500 } })
  conversation.scrollTop = 1300
  const trigger = button('放大图片 diagram.png')
  await act(async () => trigger.click())
  expect(element.querySelector('dialog[open]')).not.toBeNull()
  await act(async () => button('放大图片').click())
  expect(element.querySelector('[aria-label="图片缩放"]')!.textContent).toBe('125%')
  act(() => conversation.dispatchEvent(new Event('scroll', { bubbles: true })))
  Object.defineProperty(conversation, 'scrollHeight', { configurable: true, value: 1850 })
  act(() => listener({ event: 'text', text: '浮层期间的新回答。' }))
  expect(conversation.scrollTop).toBe(1300)
  await act(async () => button('关闭大图').click())
  expect(element.querySelector('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
  expect(conversation.scrollTop).toBe(1300)
  expect(button('回到最新内容')).toBeDefined()
  act(() => conversation.dispatchEvent(new Event('scroll', { bubbles: true })))
  Object.defineProperty(conversation, 'scrollHeight', { configurable: true, value: 1900 })
  act(() => listener({ event: 'text', text: '关闭浮层后继续到达的回答。' }))
  expect(conversation.scrollTop).toBe(1300)
  await act(async () => button('回到最新内容').click())
  expect(conversation.scrollTop).toBe(1900)
})

test('reopening a conversation labels a changed delivered image beside its current fixed snapshot', async () => {
  const image = { ...artifact, name: 'diagram.png' }
  current = { status: 'ready', conversationId: 'records/current.json', artifactId: image.id, snapshotId: 'later-image', name: image.name, sha256: 'later', deliveryState: 'changed', content: { kind: 'image', mimeType: 'image/png', base64: 'bGF0ZXI=', width: 640, height: 480 } }
  await act(async () => listener({ event: 'conversation', path: 'records/current.json', turns: [{ ...turn, artifacts: [image] }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  expect(conversation.textContent).toContain('图片在交付后已有变化；当前显示的是后来版本的固定快照。')
  expect(conversation.querySelector('img')!.getAttribute('src')).toBe('data:image/png;base64,bGF0ZXI=')
  current = { ...current, content: { kind: 'image', mimeType: 'image/png', base64: 'YW5vdGhlcg==', width: 640, height: 480 } }
  await act(async () => listener({ event: 'artifacts', path: 'records/current.json', task_id: 'report-task', artifacts: [image], turn: { ...turn, artifacts: [image] } }))
  await act(async () => button('放大图片 diagram.png').click())
  expect(element.querySelector('dialog img')!.getAttribute('src')).toBe('data:image/png;base64,bGF0ZXI=')
  await act(async () => button('关闭大图').click())
})

test.each([
  ['missing', '文件不在原位置。', '图片目前已丢失。'],
  ['unreadable', '当前账户没有读取权限。', '图片目前无法读取。'],
] as const)('reopening a conversation keeps the delivered image entry and explains %s in the chat', async (status, message, explanation) => {
  const image = { ...artifact, name: 'diagram.png' }
  current = { status, conversationId: 'records/current.json', artifactId: image.id, name: image.name, message }
  await act(async () => listener({ event: 'conversation', path: 'records/current.json', turns: [{ ...turn, artifacts: [image] }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  expect(conversation.textContent).toContain(`diagram.png：${explanation}`)
  expect(conversation.textContent).toContain(message)
  expect(conversation.textContent).toContain('成果入口仍保留，可从“查看会话成果”查看文件信息。')
  expect(conversation.querySelectorAll('img')).toHaveLength(0)
  await act(async () => button('查看会话成果').click())
  expect(button('预览 diagram.png')).toBeDefined()
  await act(async () => listener({ event: 'conversation', path: 'records/current.json', turns: [] }))
  expect(released).toHaveLength(1)
})
