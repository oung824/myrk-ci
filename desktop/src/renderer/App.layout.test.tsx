import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, test } from 'vitest'
import type { Action, Command, CompactionStatus, Delegation, ServiceEvent, TaskScope } from '../shared/protocol'
import { App } from './App'
import type { DeliveredArtifact } from '../shared/protocol'
import { decodeEvent } from '../shared/decode'

let root: Root
let element: HTMLDivElement
let listener: (event: ServiceEvent) => void
let commands: Command[]

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  commands = []
  window.myrk = {
    openArtifactPreview: async () => { throw new Error('No artifact selected') }, closeArtifactPreview: async () => {}, revealArtifact: async () => {},
    command: async command => { commands.push(command) },
    subscribe: receive => { listener = receive; return () => {} },
    copy: async () => {},
    importConversation: async () => {},
    selectDocument: async () => null,
    selectWorkspace: async () => null,
    revealFile: async () => {},
    quit: async () => {},
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

function button(label: string, scope: ParentNode = element): HTMLButtonElement {
  const result = Array.from(scope.querySelectorAll('button')).find(
    entry => entry.getAttribute('aria-label') === label || entry.textContent === label,
  )
  if (!result) throw new Error(`找不到按钮：${label}`)
  return result
}

test('a selected task folder stays compact while its scope and check controls remain available on demand', async () => {
  window.myrk.selectWorkspace = async () => 'E:\\task-materials'
  act(() => listener(decodeEvent(JSON.stringify({ event: 'snapshot', active_path: 'records/current.json', configured: false, trusted_projects: ['E:\\task-materials'], conversations: [{ path: 'records/current.json', turns: [], queue: [], running: false, stopping: false }] }))))
  await act(async () => button('选择任务文件夹').click())
  const folder = element.querySelector<HTMLDetailsElement>('details[aria-label="任务文件夹详情"]')
  expect(folder).not.toBeNull()
  expect(folder!.open).toBe(false)
  expect(folder!.querySelector('summary')?.textContent).toContain('task-materials')
  await act(async () => folder!.querySelector('summary')!.click())
  expect(folder!.open).toBe(true)
  expect(button('查看文件夹授权', folder!).disabled).toBe(false)
  expect(folder!.querySelector('[aria-label="指定检查程序与参数"]')).not.toBeNull()
})

test('the conversation files list uses saved explicit deliveries and clears when opening another conversation', async () => {
  const task = { id: 'delivery-task', goal: '整理报告', scope: null, request_limit: null }
  const artifact: DeliveredArtifact = { id: 'report-delivery', task_id: task.id, source_action_id: 'create-report', file_change_id: 'report-change', delegation_id: null, source_record_path: 'records/current.json', name: '交付报告.md', path: 'E:\\work\\交付报告.md', change_kind: 'created', delivered_sha256: 'a'.repeat(64), size_bytes: 24, delivered_at: '2026-11-14T00:00:00Z' }
  const turn = { prompt: task.goal, answer: '材料已读取。', status: 'running' as const, error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false, task, actions: [{ id: 'read-input', task_id: task.id, kind: 'read', target: '输入资料.md', status: 'completed' as const, started_at: '2026-11-14', finished_at: null, output: '原始资料', error: null, full_output_path: '工具日志.txt', backup_path: '备份.txt' }] }
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [turn] }))
  await act(async () => button('查看会话成果').click())
  const panel = () => element.querySelector('[aria-label="右侧文件查看区"]')!
  expect(panel().textContent).toContain('尚未登记已交付文件')
  act(() => listener({ event: 'artifacts', path: 'records/current.json', task_id: task.id, artifacts: [artifact], turn: { ...turn, artifacts: [artifact] } }))
  expect(panel().textContent).toContain(artifact.name)
  expect(panel().textContent).toContain('新建')
  expect(panel().textContent).not.toContain('输入资料.md')
  expect(panel().textContent).not.toContain('备份.txt')
  expect(panel().textContent).not.toContain('工具日志.txt')
  act(() => listener({ event: 'conversation', path: 'records/reopened.json', running: false, turns: [{ ...turn, artifacts: [artifact] }] }))
  await act(async () => button('查看会话成果').click())
  expect(panel().textContent).toContain(artifact.name)
  act(() => listener({ event: 'conversation', path: 'records/new.json', running: false, turns: [] }))
  await act(async () => button('查看会话成果').click())
  expect(panel().textContent).not.toContain(artifact.name)
  expect(panel().textContent).toContain('尚未登记已交付文件')
})

test('streaming preserves a reader’s position until they choose to follow the latest answer', async () => {
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [{
    prompt: '阅读长回答', answer: '前面的回答。', status: 'running', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  Object.defineProperties(conversation, {
    scrollHeight: { configurable: true, value: 1800 },
    clientHeight: { configurable: true, value: 500 },
  })
  conversation.scrollTop = 260
  act(() => conversation.dispatchEvent(new Event('scroll', { bubbles: true })))
  act(() => listener({ event: 'text', text: '新到达的回答。' }))

  expect(conversation.scrollTop).toBe(260)
  expect(conversation.textContent).toContain('新到达的回答。')
  await act(async () => button('回到最新内容').click())
  expect(conversation.scrollTop).toBe(1800)
  Object.defineProperty(conversation, 'scrollHeight', { configurable: true, value: 2200 })
  act(() => listener({ event: 'text', text: '继续到达的回答。' }))
  expect(conversation.scrollTop).toBe(2200)
})

test('an answer stays outside collapsed technical records and all execution facts remain available when expanded', async () => {
  act(() => listener({ event: 'conversation', path: 'records/current.json', turns: [{
    prompt: '交付正文', answer: '请先阅读这份简洁回答。', status: 'completed', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
    execution_facts: '审核未完成记录：0\n真实动作摘要：已写入报告。',
  }] }))
  const answer = element.querySelector('.answer-body')!
  const facts = element.querySelector('[aria-label="程序记录的事实"]')!
  const records = element.querySelector<HTMLDetailsElement>('details.turn-records')!
  expect(records.open).toBe(false)
  expect(answer.closest('details')).toBeNull()
  expect(answer.textContent).toContain('请先阅读这份简洁回答。')
  expect(facts.closest('details')).toBe(records)
  await act(async () => records.querySelector('summary')!.click())
  expect(records.open).toBe(true)
  expect(facts.textContent).toContain('审核未完成记录：0')
  expect(facts.textContent).toContain('真实动作摘要：已写入报告。')
})

test('opening the conversation’s files preserves the live conversation and returns focus when closed', async () => {
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [{
    prompt: '继续整理材料', answer: '已有回答。', status: 'running', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  Object.defineProperties(conversation, {
    scrollHeight: { configurable: true, value: 1800 },
    clientHeight: { configurable: true, value: 500 },
  })
  conversation.scrollTop = 280
  act(() => conversation.dispatchEvent(new Event('scroll', { bubbles: true })))
  const input = element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, '继续补充要求')
  act(() => input.dispatchEvent(new Event('input', { bubbles: true })))
  const files = button('查看会话成果')
  await act(async () => files.click())

  const panel = element.querySelector('[aria-label="右侧文件查看区"]')!
  expect(panel).not.toBeNull()
  expect(panel.textContent).toContain('本会话尚未登记已交付文件')
  expect(element.querySelector('[aria-label="对话内容"]')).toBe(conversation)
  expect(input.value).toBe('继续补充要求')
  expect(button('停止回答').disabled).toBe(false)
  act(() => listener({ event: 'text', text: '新的真实回答。' }))
  await act(async () => button('返回聊天', panel).click())
  expect(element.querySelector('[aria-label="右侧文件查看区"]')).toBeNull()
  expect(document.activeElement).toBe(files)
  expect(input.value).toBe('继续补充要求')
  expect(conversation.textContent).toContain('新的真实回答。')
  expect(conversation.scrollTop).toBe(280)
})

test('a reader near the bottom follows streamed content while arrow keys keep the same panel', async () => {
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [{
    prompt: '跟随回答', answer: '开头。', status: 'running', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  }] }))
  const conversation = element.querySelector<HTMLElement>('[aria-label="对话内容"]')!
  Object.defineProperties(conversation, {
    scrollHeight: { configurable: true, value: 1800 },
    clientHeight: { configurable: true, value: 500 },
  })
  conversation.scrollTop = 1240
  act(() => conversation.dispatchEvent(new Event('scroll', { bubbles: true })))
  act(() => listener({ event: 'text', text: '继续回答。' }))
  expect(conversation.scrollTop).toBe(1800)

  const input = element.querySelector<HTMLTextAreaElement>('[aria-label="输入消息"]')!
  input.focus()
  act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })))
  expect(document.activeElement).toBe(input)
  expect(element.querySelector('[aria-label="右侧文件查看区"]')).toBeNull()
  await act(async () => button('查看会话成果').click())
  const panel = element.querySelector('[aria-label="右侧文件查看区"]')!
  act(() => panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(element.querySelector('[aria-label="右侧文件查看区"]')).toBeNull()
  expect(document.activeElement).toBe(button('查看会话成果'))
})

test('opening another conversation closes the previous conversation’s files', async () => {
  await act(async () => button('查看会话成果').click())
  act(() => listener({ event: 'conversation', path: 'records/another.json', running: false, turns: [{
    prompt: '另一会话', answer: '这是另一会话的回答。', status: 'completed', error: null,
    requests: 1, input_tokens: null, output_tokens: null, thinking: false,
  }] }))
  expect(element.querySelector('[aria-label="右侧文件查看区"]')).toBeNull()
  expect(element.querySelector('[aria-label="对话内容"]')!.textContent).toContain('这是另一会话的回答。')
})

test('collapsing current work keeps an older turn’s pending memory approval available', async () => {
  const oldTask = { id: 'older-memory-task', goal: '整理原工作并提议长期记忆', scope: null, request_limit: null }
  const latestTask = { id: 'latest-completed-task', goal: '完成后来的工作', scope: null, request_limit: null }
  const proposal: Action = {
    id: 'older-memory-proposal', task_id: oldTask.id, kind: 'memory', target: '旧执行轮的待审记忆',
    status: 'awaiting_approval', started_at: '2026-11-10T01:00:00Z', finished_at: null,
    output: '', error: null, full_output_path: null, memory_deferred: true,
    approval_id: 'older-memory-approval', approval_basis: 'pending', impact: '保存用户希望答复简洁的偏好。',
  }
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: false, turns: [
    {
      prompt: oldTask.goal, answer: '原工作已完成，记忆提议仍待你决定。', status: 'completed',
      error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false,
      task: oldTask, actions: [proposal],
    },
    {
      prompt: latestTask.goal, answer: '后来的工作也已完成。', status: 'completed',
      error: null, requests: 1, input_tokens: null, output_tokens: null, thinking: false,
      task: latestTask, actions: [],
    },
  ] }))

  expect(element.querySelector('[role="region"][aria-label="当前工作"]')).not.toBeNull()
  await act(async () => button('收起当前工作').click())
  expect(button('展开当前工作')).toBeDefined()

  const pending = element.querySelector('[role="region"][aria-label="待处理事项"]')
  expect(pending).not.toBeNull()
  expect(pending!.closest('[hidden]')).toBeNull()
  expect(pending!.closest('details:not([open])')).toBeNull()
  expect(pending!.textContent).toContain('旧执行轮的待审记忆')
  const approve = button('批准这次动作', pending!)
  expect(approve.disabled).toBe(false)
  await act(async () => approve.click())

  expect(commands.filter(command => command.action === 'approve_action')).toEqual([
    {
      action: 'approve_action', path: 'records/current.json', task_id: 'older-memory-task',
      action_id: 'older-memory-proposal', approval_id: 'older-memory-approval', approved: true,
    },
  ])
})

test('compaction records start collapsed while their latest failure and original source remain reachable', async () => {
  const failed: CompactionStatus = {
    id: 'failed-summary', version: 1, status: 'failed', trigger: 'capacity_compression',
    task_id: 'compaction-task', model: 'offline-summary-model', layout_version: 2, replaced_id: null,
    source: [{ message_index: 4, part_index: 2, sha256: 'registered-source-fingerprint' }],
    retained: [], covered: [], summary: '', before_tokens: 900, after_tokens: null,
    input_budget_tokens: 1000, measurement_source: 'local_text_estimate', target_reached: null,
    request_numbers: [2], error: '候选摘要未有效减少资料，原文保留。',
  }
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: false, turns: [{
    prompt: '继续原工作', answer: '原始回答仍保留。', status: 'completed', error: null,
    requests: 2, input_tokens: null, output_tokens: null, thinking: false,
  }], compactions: [failed] }))

  const records = element.querySelector<HTMLDetailsElement>('details[aria-label="较早记录整理情况"]')
  expect(records).not.toBeNull()
  expect(records!.open).toBe(false)
  const summary = records!.querySelector('summary')!
  expect(summary.textContent).toContain('整理失败，原始记录保留')

  await act(async () => summary.click())
  expect(records!.open).toBe(true)
  expect(records!.textContent).toContain('候选摘要未有效减少资料，原文保留。')
  await act(async () => button('回查整理原文', records!).click())

  expect(commands.filter(command => command.action === 'get_compaction_evidence')).toEqual([
    {
      action: 'get_compaction_evidence', path: 'records/current.json', summary_id: 'failed-summary',
      message_index: 4, part_index: 2, start: 0,
    },
  ])
})

test.each([
  { status: 'failed', verification_status: 'pending', expected: '失败' },
  { status: 'cancelled', verification_status: 'pending', expected: '已停止' },
  { status: 'limited', verification_status: 'pending', expected: '已达上限' },
  { status: 'completed', verification_status: 'gaps', expected: '核验发现缺口' },
  { status: 'completed', verification_status: 'passed', expected: '已核对' },
  { status: 'completed', verification_status: 'pending', expected: '专用核验尚未登记' },
  { status: 'running', verification_status: 'pending', expected: '办理中' },
] satisfies { status: Delegation['status']; verification_status: Delegation['verification_status']; expected: string }[])(
  'current work reports delegation status $status with verification $verification_status accurately',
  ({ status, verification_status, expected }) => {
    const scope: TaskScope = { root: 'E:\\task-materials', allowed_actions: ['read'], provider: 'deepseek', recipients: ['deepseek'] }
    const delegation: Delegation = {
      id: 'report-check', goal: '核对报告文件', context: '核对分工收到的资料。', scope,
      evidence_requirements: '保留真实文件证据。', expected_files: ['report.txt'], record_path: 'records/report-check.json',
      status, verification_status, verified_files: [], report: '', error: null,
      requests: 1, input_tokens: null, output_tokens: null,
    }
    act(() => listener({ event: 'conversation', path: 'records/current.json', running: true, turns: [{
      prompt: '整理报告', answer: '正在汇总分工结果。', status: 'running', error: null,
      requests: 1, input_tokens: null, output_tokens: null, thinking: false,
      task: { id: 'report-task', goal: '整理报告', scope, request_limit: null }, delegations: [delegation],
    }] }))

    const currentWork = element.querySelector('[role="region"][aria-label="当前工作"]')
    expect(currentWork).not.toBeNull()
    expect(currentWork!.textContent).toContain(`核对报告文件 · ${expected}`)
  },
)

test('pending verification registration preserves ordinary read and successful check evidence', async () => {
  const scope: TaskScope = { root: 'E:\\task-materials', allowed_actions: ['read', 'check'], provider: 'deepseek', recipients: ['deepseek'] }
  const delegation: Delegation = {
    id: 'ordinary-checked-report', goal: '核对报告文件', context: '主助理读回后运行指定检查。', scope,
    evidence_requirements: '核对真实内容。', expected_files: ['report.txt'], record_path: 'records/report.json',
    status: 'completed', verification_status: 'pending', verified_files: [], report: '报告已交回。', error: null,
    requests: 1, input_tokens: null, output_tokens: null,
  }
  const read: Action = {
    id: 'main-read', task_id: 'ordinary-check-task', kind: 'read', target: 'report.txt', status: 'completed',
    started_at: '2026-11-11T01:00:00Z', finished_at: '2026-11-11T01:00:01Z',
    output: '[report.txt:1] 已核对的真实报告内容', error: null, full_output_path: null,
  }
  const check: Action = {
    id: 'main-check', task_id: 'ordinary-check-task', kind: 'check', target: '报告全文检查', status: 'completed',
    started_at: '2026-11-11T01:00:02Z', finished_at: '2026-11-11T01:00:03Z',
    output: '指定检查通过', stdout: 'report content matches independently expected text', stderr: '',
    cwd: scope.root, exit_code: 0, error: null, full_output_path: null,
  }
  act(() => listener({ event: 'conversation', path: 'records/current.json', running: false, turns: [{
    prompt: '主助理核对报告后交付', answer: '普通读取与指定检查已经完成。', status: 'completed', error: null,
    requests: 4, input_tokens: null, output_tokens: null, thinking: false, checks_passed: true,
    task: { id: 'ordinary-check-task', goal: '主助理核对报告后交付', scope, request_limit: null },
    delegations: [delegation], actions: [read, check],
  }] }))

  const currentWork = element.querySelector('[role="region"][aria-label="当前工作"]')!
  expect(currentWork.textContent).toContain('核对报告文件 · 专用核验尚未登记')
  const records = element.querySelector<HTMLDetailsElement>('details.turn-records')!
  await act(async () => records.querySelector('summary')!.click())
  expect(records.open).toBe(true)
  const delegationRecord = Array.from(records.querySelectorAll<HTMLDetailsElement>('details')).find(
    detail => detail.querySelector('summary')?.textContent === '按需分工与主助理核验',
  )!
  await act(async () => delegationRecord.querySelector('summary')!.click())
  expect(delegationRecord.open).toBe(true)
  expect(delegationRecord.textContent).toContain('专用核验尚未登记')
  expect(delegationRecord.textContent).not.toContain('仍待主助理核验')
  const actionRecords = records.querySelector<HTMLDetailsElement>('details.action-records')!
  await act(async () => actionRecords.querySelector('summary')!.click())
  expect(actionRecords.open).toBe(true)
  expect(actionRecords.textContent).toContain('[report.txt:1] 已核对的真实报告内容')
  expect(actionRecords.textContent).toContain('退出情况：0')
  expect(actionRecords.textContent).toContain('report content matches independently expected text')
})
