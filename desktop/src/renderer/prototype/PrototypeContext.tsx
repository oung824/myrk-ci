import { createContext, useContext, useState, type ReactNode } from 'react'

export type Scenario = 'welcome' | 'running' | 'approval' | 'delivered' | 'failed'
export const scenarioLabels: Record<Scenario, string> = {
  welcome: '新会话', running: '处理中', approval: '等待批准', delivered: '已交付', failed: '中断恢复',
}
export const conversations = [
  { id: 'release', title: '整理项目发布资料', time: '刚刚', hint: '等待你的决定' },
  { id: 'reading', title: '阅读资料，整理重点', time: '今天', hint: '已完成' },
  { id: 'settings', title: '检查本地开发环境', time: '昨天', hint: '已完成' },
]

function usePrototypeState() {
  const initial = new URLSearchParams(location.search).get('scenario') as Scenario | null
  const [scenario, updateScenario] = useState<Scenario>(initial && initial in scenarioLabels ? initial : 'approval')
  const [draft, setDraft] = useState('')
  const [model, setModel] = useState('DeepSeek Chat')
  const [reasoning, setReasoning] = useState('标准')
  const [approvalMode, setApprovalMode] = useState('请求批准')
  const [queueCount, setQueueCount] = useState(0)
  const [notice, setNotice] = useState('')
  const [activeConversation, setActiveConversation] = useState('release')
  const [conversationQuery, setConversationQuery] = useState('')
  const [workspace, setWorkspace] = useState<string | null>('E:\\工作资料\\Myrk')
  const [attachment, setAttachment] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const [submittedPrompt, setSubmittedPrompt] = useState('帮我整理这个项目的最新变化，写一份发布说明，并检查文中的链接。')
  const title = conversations.find(item => item.id === activeConversation)?.title ?? '新会话'
  const running = scenario === 'running' || scenario === 'approval'

  function setScenario(value: Scenario) {
    updateScenario(value)
    setAccepted(false)
    setNotice('')
    const url = new URL(location.href)
    url.searchParams.set('scenario', value)
    history.replaceState(null, '', url)
  }
  function showNotice(value: string) { setNotice(value) }
  function selectConversation(id: string) {
    setActiveConversation(id)
    setScenario(id === 'release' ? 'approval' : 'delivered')
    setDraft('')
  }
  function newChat() { setActiveConversation('new'); setScenario('welcome'); setDraft('') }
  function send() {
    if (!draft.trim()) return
    setSubmittedPrompt(draft.trim())
    setActiveConversation('release')
    setScenario('running')
    setDraft('')
    showNotice('已进入处理中的示例状态。')
  }
  function stop() { setScenario('failed'); showNotice('示例工作已停止，原进展仍保留。') }
  function supplement() { if (draft.trim()) { setDraft(''); showNotice('补充已加入当前工作（示例）。') } }
  function enqueue() { if (draft.trim()) { setQueueCount(value => value + 1); setDraft(''); showNotice('新工作已排队（示例）。') } }
  function approve() { setScenario('running'); showNotice('本次动作已批准；这里只更新演示状态。') }
  function reject() { setScenario('failed'); showNotice('本次动作已拒绝，未执行任何命令。') }
  function accept() { setAccepted(true); showNotice('已记录为接受成果（示例）。') }
  function retry() { setScenario('running'); showNotice('已继续原工作的示例状态。') }
  function toggleAttachment() { setAttachment(value => value ? null : '项目说明.md'); showNotice('附件仅用于排版演示，未读取本机文件。') }
  function toggleWorkspace() { setWorkspace(value => value ? null : 'E:\\工作资料\\Myrk'); showNotice('任务文件夹只在原型内切换，未访问本机目录。') }

  return { scenario, setScenario, title, draft, setDraft, model, setModel, reasoning, setReasoning,
    approvalMode, setApprovalMode, queueCount, notice, activeConversation, conversationQuery, setConversationQuery,
    workspace, attachment, running, settingsOpen, setSettingsOpen, accepted, submittedPrompt,
    selectConversation, newChat, send, stop, supplement, enqueue, approve, reject, accept, retry,
    toggleAttachment, toggleWorkspace, showNotice, openSettings: () => setSettingsOpen(true) }
}
type PrototypeState = ReturnType<typeof usePrototypeState>
const PrototypeContext = createContext<PrototypeState | null>(null)
export function PrototypeProvider({ children }: { children: ReactNode }) {
  return <PrototypeContext.Provider value={usePrototypeState()}>{children}</PrototypeContext.Provider>
}
export function usePrototype() { return useContext(PrototypeContext)! }
