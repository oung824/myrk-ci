import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'
import Markdown from 'react-markdown'
import type { ApprovalMode, ApprovalSettings, ChatModel, Command, CompactionEvidence, CompactionStatus, ConnectionStorageState, ConversationSummary, MemoryState, ModelCatalogResult, ModelSelection, ProviderChange, ProviderConfig, QueuedTask, RecoveryReport, Supplement, TaskScope, Turn } from '../shared/protocol'
import { SettingsDialog, type SettingsValues } from './SettingsDialog'
import { CompactionRecords } from './CompactionRecords'
import { FolderTrustDialog } from './FolderTrustDialog'
import { ConversationApprovalDialog } from './ConversationApprovalDialog'
import { defaultProvider } from './provider-defaults'
import { connectionIdentity } from '../shared/connection'
import { RequestObservationDetails } from './RequestObservationDetails'
import { ActionDetails } from './ActionDetails'
import { Icon } from './Icon'
import { approvalLabels, requestActorLabels } from './ui-labels'
import { ModelControls } from './ModelControls'
import { ConversationFilesPanel } from './ConversationFilesPanel'
import { ArtifactWorkspace } from './ArtifactWorkspace'
import { DeliveredImage } from './ImagePreview'
import './styles.css'

const folderActions: TaskScope['allowed_actions'] = ['list', 'read', 'search', 'edit', 'calculate', 'check']

function supplementStatus(item: Supplement): { label: string; canRevoke: boolean } {
  if (item.revoked_at != null) return { label: '已撤回', canRevoke: false }
  if (item.applied_request !== null) return { label: `第${item.applied_request}次请求准备时已采用`, canRevoke: false }
  if (item.selected) return { label: '已选用，已进入请求准备', canRevoke: false }
  return { label: '已收到，等待下一次准备动作', canRevoke: true }
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [filesOpen, setFilesOpen] = useState(false)
  const filesButton = useRef<HTMLButtonElement>(null)
  const restoreFilesFocus = useRef(false)
  const [followingLatest, setFollowingLatest] = useState(true)
  const followLatest = useRef(true)
  const [workExpanded, setWorkExpanded] = useState(true)
  const [serviceState, setServiceState] = useState<'connecting' | 'ready' | 'failed'>('connecting')
  const serviceFailure = useRef<string | null>(null)
  const ready = serviceState === 'ready'
  const [selectedConfigured, setConfigured] = useState(false)
  const [connectionStorage, setConnectionStorage] = useState<ConnectionStorageState | null>(null)
  const connectionStorageRef = useRef<ConnectionStorageState | null>(null)
  const forgettingConnection = useRef(false)
  const [providerConfig, setProviderConfig] = useState<ProviderConfig>(defaultProvider)
  const [modelSelection, setModelSelection] = useState<ModelSelection | null>(null)
  const confirmedModelSelection = useRef<ModelSelection | null>(null)
  const [chatModels, setChatModels] = useState<ChatModel[]>([])
  const [modelError, setModelError] = useState('')
  const [savingModel, setSavingModel] = useState(false)
  const savingModelRef = useRef(false)
  const modelRequest = useRef<string | null>(null)
  const pendingSettings = useRef<SettingsValues | null>(null)
  const pendingApproval = useRef(false)
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>('request_approval')
  const [conversationApproval, setConversationApproval] = useState<ApprovalSettings | null>(null)
  const [conversationApprovalOpen, setConversationApprovalOpen] = useState(false)
  const [savingConversationApproval, setSavingConversationApproval] = useState(false)
  const [savingTurnReviewer, setSavingTurnReviewer] = useState(false)
  const [providerChange, setProviderChange] = useState<ProviderChange | null>(null)
  const [settings, setSettings] = useState(false)
  const settingsOpen = useRef(false)
  const [settingsError, setSettingsError] = useState('')
  const [modelCatalog, setModelCatalog] = useState<ModelCatalogResult | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [memoryState, setMemoryState] = useState<MemoryState | null>(null)
  const [delegationMaxTurns, setDelegationMaxTurns] = useState(250)
  const [queue, setQueue] = useState<QueuedTask[]>([])
  const activePathRef = useRef('')
  const [draft, setDraft] = useState('')
  const pendingSupplement = useRef<Extract<Command, { action: 'submit_supplement' }> | null>(null)
  const [newTurnApproval, setNewTurnApproval] = useState<ApprovalMode | ''>('')
  const [document, setDocument] = useState<string | null>(null)
  const [workspace, setWorkspace] = useState<string | null>(null)
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null)
  const [checkArguments, setCheckArguments] = useState('')
  const [trustedProjects, setTrustedProjects] = useState<string[]>([])
  const [savingProjectTrust, setSavingProjectTrust] = useState(false)
  const [projectTrustError, setProjectTrustError] = useState('')
  const pendingProjectTrust = useRef<{ root: string; owner: string; resetChecks: boolean } | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [compactions, setCompactions] = useState<CompactionStatus[]>([])
  const [compactionEvidence, setCompactionEvidence] = useState<CompactionEvidence | null>(null)
  const [running, setRunning] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [status, setStatus] = useState('正在连接办事程序…')
  const [notice, setNotice] = useState('')
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [query, setQuery] = useState('')
  const [activePath, setActivePath] = useState('')
  const [unreadable, setUnreadable] = useState(0)
  const [recovery, setRecovery] = useState<RecoveryReport | null>(null)
  const [earlierRecoveries, setEarlierRecoveries] = useState<RecoveryReport[]>([])
  const [saveFailures, setSaveFailures] = useState<string[]>([])
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null)
  const activeTask = useRef(false)
  const messages = useRef<HTMLElement>(null)
  const imageReading = useRef<{ top: number; height: number; following: boolean } | null>(null)
  const restoredImageTop = useRef<number | null>(null)

  function imageReadingChanged(open: boolean): void {
    const conversation = messages.current
    if (!conversation) return
    if (open) {
      restoredImageTop.current = null
      imageReading.current = { top: conversation.scrollTop, height: conversation.scrollHeight, following: followLatest.current }
      followLatest.current = false
    } else if (imageReading.current) {
      conversation.scrollTop = imageReading.current.top
      followLatest.current = imageReading.current.following && imageReading.current.height === conversation.scrollHeight
      restoredImageTop.current = followLatest.current ? null : conversation.scrollTop
      setFollowingLatest(followLatest.current)
      imageReading.current = null
    }
  }

  function connectionAvailable(provider: ProviderConfig): boolean {
    return connectionStorage ? connectionStorage.available.some(candidate => connectionIdentity(candidate) === connectionIdentity(provider)) : selectedConfigured
  }
  function taskConnectionAvailable(taskId: string): boolean {
    return connectionAvailable(turns.findLast(turn => turn.task?.id === taskId)?.task?.provider_config ?? providerConfig)
  }
  const configured = connectionAvailable(modelSelection?.provider_config ?? providerConfig)

  useEffect(() => window.myrk.subscribe((event) => {
    if (event.path && event.path !== activePathRef.current && !['ready', 'conversation'].includes(event.event)) {
      if (event.event === 'finished' || event.event === 'started') command({ action: 'list', query: '' })
      return
    }
    switch (event.event) {
      case 'model_controls':
        if (event.request_id && event.request_id !== modelRequest.current) break
        modelRequest.current = null
        savingModelRef.current = false
        setSavingModel(false)
        confirmedModelSelection.current = event.selection
        setModelSelection(event.selection)
        setChatModels(event.models)
        setModelError(event.error ?? '')
        break
      case 'model_catalog':
        setModelCatalog(event)
        break
      case 'connection_storage':
        connectionStorageRef.current = event
        setConnectionStorage(event)
        if (forgettingConnection.current) {
          forgettingConnection.current = false
          setSavingSettings(false)
          setStatus(event.error ?? '已忘记此连接密码；聊天和型号选择保留。')
        }
        break
      case 'memory':
        setMemoryState(event)
        break
      case 'ready':
        serviceFailure.current = null
        setServiceState('ready')
        activePathRef.current = event.path
        setActivePath(event.path)
        command({ action: 'list', query: '' })
        command({ action: 'get_connection_storage' })
        fetchModelControls(event.path)
        setStatus('准备就绪。请在设置中填写连接密码。')
        break
      case 'configured': {
        const acknowledgedProvider = event.provider_config ?? pendingSettings.current?.providerConfig
        const storage = connectionStorageRef.current
        const passwordSaved = acknowledgedProvider && storage?.saved.some(candidate => connectionIdentity(candidate) === connectionIdentity(acknowledgedProvider))
        if (event.trusted_projects !== undefined) setTrustedProjects(event.trusted_projects)
        if (event.approval_mode !== undefined) setApprovalMode(event.approval_mode)
        if (event.provider_config || pendingSettings.current) {
          setProviderConfig(event.provider_config ?? pendingSettings.current!.providerConfig)
        }
        if (event.delegation_max_turns !== undefined || pendingSettings.current) setDelegationMaxTurns(event.delegation_max_turns ?? pendingSettings.current!.delegationMaxTurns)
        if (!pendingApproval.current) {
          const provider = event.provider_config ?? pendingSettings.current?.providerConfig
          if (provider) { confirmedModelSelection.current = null; setModelSelection(null); setChatModels([]) }
          fetchModelControls(activePathRef.current)
        }
        pendingSettings.current = null
        if (event.configured !== undefined) setConfigured(event.configured)
        else if (!pendingApproval.current) setConfigured(true)
        setSavingSettings(false)
        setSettingsError('')
        setSettings(false)
        settingsOpen.current = false
        setStatus(pendingApproval.current ? '默认批准方式已保存；只用于之后新建的会话，已有聊天的选择保持原样。' : storage?.error ?? (event.configured === false ? '准备就绪。请在设置中填写连接密码。' : passwordSaved ? '连接密码已保存到本机；尚未检查真实连接。' : '连接资料已设置；尚未检查真实连接。'))
        pendingApproval.current = false
        break
      }
      case 'snapshot': {
        if (event.trusted_projects !== undefined) setTrustedProjects(event.trusted_projects)
        serviceFailure.current = null
        setServiceState('ready')
        setConfigured(event.configured)
        if (event.approval_mode !== undefined) setApprovalMode(event.approval_mode)
        if (event.delegation_max_turns !== undefined) setDelegationMaxTurns(event.delegation_max_turns)
        if (event.provider_config) {
          setProviderConfig(event.provider_config)
        }
        const current = event.conversations.find(item => item.path === event.active_path)
        if (current) {
          if (activePathRef.current !== current.path) {
            imageReading.current = null
            if (pendingSupplement.current) {
              pendingSupplement.current = null
              setDraft('')
            }
          }
          activePathRef.current = current.path
          setActivePath(current.path)
          setConversationApproval(current.approval_settings ?? null)
          setTurns(current.turns)
          setCompactions(current.compactions ?? [])
          setCompactionEvidence(null)
          setQueue(current.queue)
          activeTask.current = current.running
          setRunning(current.running)
          setStopping(current.stopping)
          setRecovery(current.recovery ?? null)
          setEarlierRecoveries(current.earlier_recoveries ?? [])
          setSaveFailures(current.save_failures ?? [])
          setLastSavedAt(current.last_saved_at ?? null)
          confirmedModelSelection.current = current.model_selection ?? null
          setModelSelection(current.model_selection ?? null)
          setChatModels([])
          setModelError('')
          fetchModelControls(current.path)
          setStatus(current.stopping ? '正在停止；等待本次请求收尾…' : current.running ? '正在等待回答服务' : current.turns.at(-1)?.service_failure ? '服务失败后保留原工作，等待你核查并选择续办。' : '已恢复当前进展，未自动重新发起工作。')
        }
        command({ action: 'list', query: '' })
        break
      }
      case 'project_trust': {
        const selection = pendingProjectTrust.current
        const selectedProject = selection?.root.toLowerCase() === event.root.toLowerCase()
        if (selectedProject) {
          setSavingProjectTrust(false)
          pendingProjectTrust.current = null
        }
        if (!event.saved) {
          if (selectedProject) setProjectTrustError('项目信任未能保存；原文件夹选择保持原样，请重试。')
          break
        }
        setTrustedProjects(previous => event.trusted ? [...new Set([...previous, event.root])] : previous.filter(root => root !== event.root))
        if (selectedProject && selection && selection.owner === activePathRef.current && event.trusted) {
          setWorkspace(event.root)
          setDocument(null)
          if (selection.resetChecks) setCheckArguments('')
          setPendingWorkspace(null)
          setProjectTrustError('')
          setStatus('已保存项目信任；它独立于回答服务和批准方式，发送后才按需使用资料。')
        }
        break
      }
      case 'turn_approval':
        setSavingTurnReviewer(false)
        if (event.saved) {
          setTurns(previous => previous.map((turn, index) => index === previous.length - 1 && turn.status === 'running' && turn.task?.id === event.task_id ? { ...turn, approval_settings: event.approval_settings } : turn))
          setNotice('本轮审核者已保存；只影响后续新准备的步骤，已有待批动作及现有分工保持原选择。')
        } else setStatus('本轮审核者未保存；原选择保持原样。')
        break
      case 'conversation_approval':
        setSavingConversationApproval(false)
        if (event.saved) {
          setConversationApproval(event.approval_settings)
          setConversationApprovalOpen(false)
          setNotice('会话批准方式已保存；用于之后的新执行轮（从开始到结束的一次办事过程）。')
        } else setStatus('会话批准方式未保存；原选择保持原样。')
        break
      case 'conversation':
        imageReading.current = null
        restoredImageTop.current = null
        followLatest.current = true
        setFollowingLatest(true)
        setCompactions(event.compactions ?? [])
        setCompactionEvidence(null)
        confirmedModelSelection.current = event.model_selection ?? null
        setModelSelection(event.model_selection ?? null)
        setChatModels([])
        setModelError('')
        savingModelRef.current = false
        setSavingModel(false)
        setNewTurnApproval('')
        setConversationApproval(event.approval_settings ?? null)
        setConversationApprovalOpen(false)
        setSavingConversationApproval(false)
        setSavingTurnReviewer(false)
        setTurns(event.turns)
        setQueue(event.queue ?? [])
        activeTask.current = event.running ?? false
        setRunning(event.running ?? false)
        setStopping(event.stopping ?? false)
        setRecovery(event.recovery ?? null)
        setEarlierRecoveries(event.earlier_recoveries ?? [])
        setSaveFailures(event.save_failures ?? [])
        setLastSavedAt(event.last_saved_at ?? null)
        if (pendingSupplement.current?.path !== event.path) {
          pendingSupplement.current = null
          setDraft('')
          setNotice('')
        }
        activePathRef.current = event.path
        setActivePath(event.path)
        setDocument(null)
        setWorkspace(null)
        setPendingWorkspace(null)
        setCheckArguments('')
        pendingProjectTrust.current = null
        setSavingProjectTrust(false)
        setProjectTrustError('')
        setSettings(false)
        settingsOpen.current = false
        setProviderChange(null)
        setStatus(event.stopping ? '正在停止；等待本次请求收尾…' : event.running ? '正在等待回答服务' : event.turns.at(-1)?.service_failure ? '服务失败后保留原工作，等待你核查并选择续办。' : event.turns.length ? '已打开旧会话，可以继续提问。' : '新会话已准备。')
        command({ action: 'list', query: '' })
        fetchModelControls(event.path)
        break
      case 'conversations':
        setConversations(event.items)
        setUnreadable(event.unreadable)
        break
      case 'started':
        setNewTurnApproval('')
        setProviderChange(null)
        setRecovery(null)
        activeTask.current = true
        setRunning(true)
        setStopping(false)
        if (!pendingSupplement.current) {
          setDraft('')
          setNotice('')
        }
        setDocument(null)
        setStatus('正在连接并等待回答…')
        setTurns((previous) => [...previous, {
          prompt: event.prompt, answer: '', status: 'running', error: null,
          requests: 0, input_tokens: null, output_tokens: null, thinking: false, task: event.task, actions: [], approval_settings: event.approval_settings ?? event.task?.approval,
        }])
        command({ action: 'list', query: '' })
        break
      case 'task':
        setTurns(previous => previous.map((turn, index) => turn.task?.id === event.task.id || !turn.task && index === previous.length - 1 ? { ...turn, task: event.task } : turn))
        break
      case 'queue':
        setNewTurnApproval('')
        setQueue(event.queue)
        if (event.saved !== undefined) {
          if (!pendingSupplement.current) setDraft('')
          setNotice(event.saved ? '新工作已排队，轮到后才开始。' : '新工作已进入队列，但进展未能保存。')
        }
        break
      case 'supplement': {
        const pending = pendingSupplement.current
        if (event.saved && pending && pending.path === event.path && pending.task_id === event.task_id && pending.supplement_id === event.supplement.id && pending.prompt === event.supplement.text) {
          pendingSupplement.current = null
          setDraft('')
        }
        if (!event.saved) {
          setNotice('补充的变更尚未保存。')
          break
        }
        setNotice(`补充${supplementStatus(event.supplement).label}。`)
        setTurns(previous => {
          const owner = previous.findLastIndex(turn => turn.task?.id === event.task_id)
          return previous.map((turn, index) => {
            if (index !== owner || !turn.task) return turn
            const supplements = [...(turn.task.supplements ?? [])]
            const itemIndex = supplements.findIndex(item => item.id === event.supplement.id)
            if (itemIndex < 0) supplements.push(event.supplement)
            else supplements[itemIndex] = event.supplement
            return { ...turn, task: { ...turn.task, supplements } }
          })
        })
        break
      }
      case 'text':
        setTurns((previous) => previous.map((turn, index) => index === previous.length - 1
          ? { ...turn, answer: turn.answer + event.text } : turn))
        break
      case 'status':
        setStatus((previous) => previous.startsWith('正在停止') ? previous : event.text)
        break
      case 'compactions':
        setCompactions(event.compactions)
        break
      case 'compaction_evidence':
        setCompactionEvidence(event)
        break
      case 'action':
        setTurns(previous => {
          const existing = previous.findIndex(turn => turn.actions?.some(action => action.id === event.action.id))
          const newest = previous.findLastIndex(turn => turn.task?.id === event.action.task_id)
          const owner = existing >= 0 ? existing : newest >= 0 ? newest : previous.at(-1)?.task ? -1 : previous.length - 1
          return previous.map((turn, index) => index === owner
            ? { ...turn, actions: [...(turn.actions ?? []).filter(action => action.id !== event.action.id), event.action] } : turn)
        })
        break
      case 'artifacts':
        setTurns(previous => {
          const owner = previous.findLastIndex(turn => turn.task?.id === event.task_id)
          return previous.map((turn, index) => index === owner ? { ...turn, artifacts: event.artifacts, actions: event.turn.actions ?? turn.actions } : turn)
        })
        break
      case 'plan':
        setTurns(previous => previous.map((turn, index) => index === previous.length - 1 ? { ...turn, plan: event.plan } : turn))
        break
      case 'delegation':
        setTurns(previous => previous.map((turn, index) => index === previous.length - 1 ? { ...turn, delegations: [...(turn.delegations ?? []).filter(item => item.id !== event.delegation.id), event.delegation] } : turn))
        break
      case 'skills':
        setTurns(previous => previous.map((turn, index) => index === previous.length - 1 ? { ...turn, skill_catalog: event.skills } : turn))
        break
      case 'delivery':
        setTurns(previous => {
          const owner = previous.findLastIndex(turn => turn.task?.id === event.task_id && turn.delivery_status === 'awaiting_acceptance')
          return previous.map((turn, index) => index === owner ? event.turn : turn)
        })
        setNotice(event.saved ? '已记录你接受成果。' : '接受选择尚未保存，请检查记录位置。')
        break
      case 'stopping':
        setStopping(true)
        setStatus('正在停止；等待本次请求收尾…')
        break
      case 'finished':
        activeTask.current = false
        activePathRef.current = event.path
        setActivePath(event.path)
        command({ action: 'list', query: '' })
        setTurns((previous) => [...previous.slice(0, -1), event.turn])
        setRunning(false)
        setStopping(false)
        setStatus(event.saved
          ? event.turn.status === 'completed' ? event.turn.actions?.some(action => action.status !== 'completed')
            ? '本次处理已结束，仍有未完成或需要重新核对的动作；请查看动作记录。记录已保存。' : event.turn.delivery_status === 'awaiting_acceptance' ? '本次处理结束，记录已保存；成果仍待你验收。' : '已完成，记录已保存。'
            : event.turn.status === 'cancelled' ? '已停止，已收到的内容已保存；本次回答未完成。'
              : `${event.turn.error ?? '本轮未完成。'} 已有内容已保存。`
          : `${event.turn.error ?? ''} 本次结果未保存；已有内容保留在当前窗口。`)
        if (event.recovery !== undefined) setRecovery(event.recovery)
        else if (event.turn.service_failure && event.saved) command({ action: 'open', path: event.path })
        if (event.earlier_recoveries !== undefined) setEarlierRecoveries(event.earlier_recoveries)
        break
      case 'recovery':
        if (event.earlier) setEarlierRecoveries(previous => previous.map(report => report.task_id === event.recovery.task_id ? event.recovery : report))
        else setRecovery(event.recovery)
        setNotice(event.saved ? '真实核查结论已另行保存，原始动作记录保留。' : '核查结论未保存，不能据此续办。')
        break
      case 'provider_preview':
        setProviderChange(event.change)
        setStatus('尚未更换服务；请审阅具体接收地址、必要资料及影响。')
        break
      case 'provider_decided':
        setProviderChange(null)
        setNotice(event.approved ? '已记录本次接收方批准；等待原工作续办。' : '已拒绝更换服务，没有向新接收方发送资料。')
        break
      case 'error':
        if (event.supplement_id !== undefined && !event.fatal) {
          setNotice(event.message)
          break
        }
        if (savingModelRef.current) {
          savingModelRef.current = false
          setSavingModel(false)
          modelRequest.current = null
          setModelSelection(confirmedModelSelection.current)
          setModelError(event.message)
        }
        if (pendingProjectTrust.current) {
          pendingProjectTrust.current = null
          setSavingProjectTrust(false)
          setProjectTrustError(event.message)
        }
        setSavingTurnReviewer(false)
        setSavingConversationApproval(false)
        if (settingsOpen.current) setSettingsError(event.message)
        if (pendingSettings.current || pendingApproval.current || forgettingConnection.current) {
          pendingSettings.current = null
          pendingApproval.current = false
          forgettingConnection.current = false
          setSavingSettings(false)
        }
        if (event.fatal || !activeTask.current) {
          activeTask.current = false
          setRunning(false)
          setStopping(false)
        }
        if (event.fatal) {
          serviceFailure.current = event.message
          setServiceState('failed')
        }
        setStatus(event.message)
        break
      case 'shutdown_blocked':
        setStopping(false)
        setStatus(event.message)
        break
      case 'closed':
        activeTask.current = false
        setServiceState('failed')
        setRunning(false)
        setStopping(false)
        setStatus(serviceFailure.current ?? '办事程序已结束，请关闭窗口后重新启动。')
        break
    }
  }), [])

  useEffect(() => { command({ action: 'snapshot' }) }, [])

  useEffect(() => { setFilesOpen(false) }, [activePath])

  useLayoutEffect(() => {
    if (!filesOpen && restoreFilesFocus.current) {
      restoreFilesFocus.current = false
      filesButton.current?.focus()
    }
  }, [filesOpen])

  function closeFiles(): void {
    restoreFilesFocus.current = true
    setFilesOpen(false)
  }

  useEffect(() => {
    if (messages.current && followLatest.current) messages.current.scrollTop = messages.current.scrollHeight
  }, [turns])

  function command(action: Exclude<Command, { action: 'shutdown' | 'import' }>): void {
    void window.myrk.command(action).catch(() => {
      activeTask.current = false
      setServiceState('failed')
      setRunning(false)
      setStatus(serviceFailure.current ?? '与办事程序通信失败，请关闭窗口后重新启动。')
    })
  }

  function fetchModelControls(path: string, refresh = false): void {
    const request_id = crypto.randomUUID()
    modelRequest.current = request_id
    void window.myrk.command({ action: 'get_model_controls', path, refresh, request_id }).catch(() => {
      if (modelRequest.current !== request_id || path !== activePathRef.current) return
      modelRequest.current = null
      setModelError('未能读取可选型号；当前选择保留，可刷新重试。')
    })
  }

  function selectModel(model: string, reasoning: string): void {
    const selection = confirmedModelSelection.current
    if (!selection || running) return
    const path = activePathRef.current
    const request_id = crypto.randomUUID()
    modelRequest.current = request_id
    savingModelRef.current = true
    setSavingModel(true)
    setModelError('')
    setModelSelection({ provider_config: { ...selection.provider_config, model }, reasoning })
    void window.myrk.command({ action: 'select_model', path, model, reasoning, request_id }).catch(() => {
      if (modelRequest.current !== request_id || path !== activePathRef.current) return
      modelRequest.current = null
      savingModelRef.current = false
      setSavingModel(false)
      setModelSelection(confirmedModelSelection.current)
      setModelError('选择未能保存，已保留原型号与强度；请重试。')
    })
  }

  const latestTurn = turns.at(-1)
  const runningTask = running && latestTurn?.status === 'running' ? latestTurn.task : undefined
  const supplementTask = latestTurn?.status === 'completed' ? undefined : latestTurn?.task

  function send(event?: FormEvent, newTask = false): void {
    event?.preventDefault()
    const prompt = draft.trim()
    if (!ready || stopping || savingModelRef.current || !prompt) return
    if (!configured && (!running || newTask)) {
      setSettingsError('')
      setSettings(true)
      settingsOpen.current = true
      setStatus('请先填写本次使用的连接密码。')
      return
    }
    if (pendingWorkspace) {
      setStatus('请先确认或取消这次选择的文件夹。')
      return
    }
    if (running && !newTask) {
      const path = activePathRef.current
      if (!runningTask || !path) return
      const previous = pendingSupplement.current
      const submission = previous?.path === path && previous.task_id === runningTask.id && previous.prompt === prompt ? previous : {
        action: 'submit_supplement' as const, path, task_id: runningTask.id, supplement_id: crypto.randomUUID(), prompt,
      }
      pendingSupplement.current = submission
      void window.myrk.command(submission).catch(() => {
        if (pendingSupplement.current === submission && activePathRef.current === path) setNotice('暂未确认收到补充，可再次发送。')
      })
      return
    }
    let checks: TaskScope['checks'] = []
    if (workspace && checkArguments.trim()) {
      try {
        const argv: unknown = JSON.parse(checkArguments)
        if (!Array.isArray(argv) || !argv.length || !argv.every(value => typeof value === 'string' && value.length > 0)) throw new Error('invalid')
        checks = [{ name: '指定检查', argv }]
      } catch {
        setStatus('指定检查请填写文字列表：第一项是程序完整位置，后面各项是参数（交给程序的内容）。')
        return
      }
    }
    pendingSupplement.current = null
    if (!running) setRunning(true)
    const provider = modelSelection?.provider_config ?? providerConfig
    const recipient = provider.protocol === 'google' ? 'google' : provider.protocol === 'deepseek' ? 'deepseek' : 'openai-compatible'
    command({ path: activePathRef.current, ...(modelSelection ? { model_selection: modelSelection } : {}), ...(newTask ? { new_task: true } : {}), action: 'send', prompt, ...(newTurnApproval ? { approval_mode: newTurnApproval } : {}), ...(document ? { document } : {}), ...(workspace ? {
      scope: { root: workspace, allowed_actions: folderActions, checks, provider: recipient, recipients: [recipient] },
    } : {}) })
  }

  function steerQueued(task_id: string, queued_task_id: string): void {
    const path = activePath
    if (path !== activePathRef.current) return
    void window.myrk.command({ action: 'steer_queued', path, task_id, queued_task_id }).catch(() => {
      if (activePathRef.current === path) setNotice('暂未确认引导，排队列表将显示实际结果。')
    })
  }

  function revokeSupplement(task_id: string, supplement_id: string): void {
    const path = activePathRef.current
    void window.myrk.command({ action: 'revoke_supplement', path, task_id, supplement_id }).catch(() => {
      if (activePathRef.current === path) setNotice('暂未确认撤回补充，可重试。')
    })
  }

  const settingsProvider = modelSelection?.provider_config ?? providerConfig
  const displayedSelection = runningTask?.provider_config ? { provider_config: runningTask.provider_config, reasoning: runningTask.reasoning ?? 'default' } : modelSelection ?? { provider_config: providerConfig, reasoning: 'default' }
  const composer = <div className="composer-area">
    <form className="composer" onSubmit={send}>
      {workspace && <fieldset className="task-scope" disabled={running}>
        <details className="scope-settings" aria-label="任务文件夹详情">
        <summary className="scope-compact"><Icon name="folder" size={15}/><span><strong>{workspace.split(/[\\/]/).filter(Boolean).at(-1) || workspace}</strong><small>已信任 · 默认工作位置</small></span><Icon name="chevronDown" size={14}/></summary>
        <div className="scope-summary"><span className="folder-symbol" aria-hidden="true">▱</span><div><strong>{workspace.split(/[\\/]/).filter(Boolean).at(-1) || workspace}</strong><span>已信任项目 · 默认工作位置</span></div><button type="button" aria-label="查看文件夹授权" onClick={() => { setProjectTrustError(''); setPendingWorkspace(workspace) }}>查看范围</button><button type="button" aria-label="移除任务文件夹" onClick={() => { setWorkspace(null); setCheckArguments('') }}>×</button></div>
        <details className="scope-check"><summary>指定检查</summary><label>具体程序与参数（启动哪个程序及交给它的内容）<textarea aria-label="指定检查程序与参数" value={checkArguments} onChange={event => setCheckArguments(event.target.value)} /></label><p>填写文字列表，例如 ["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoProfile", "-Command", "Get-Location"]。列表第一项是程序，其余是参数（交给程序的内容），按当前实际访问范围运行，默认最多60秒。</p></details>
        </details>
      </fieldset>}
      {document && <div className="attachment" aria-label="待发送资料">
        <div><strong>{document.split(/[\\/]/).at(-1)}</strong><span className="attachment-path">{document}</span></div>
        <button type="button" aria-label="移除待发送资料" disabled={running} onClick={() => setDocument(null)}>×</button>
        <p>发送后将读取这份资料，并将内容提供给回答服务；本次只读取这一份文件。</p>
      </div>}
      <textarea
        aria-label="输入消息" placeholder="输入消息…" value={draft} disabled={!ready || stopping}
        rows={turns.length ? 2 : 3}
        onChange={(event) => {
          pendingSupplement.current = null
          setDraft(event.target.value)
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            send()
          }
        }}
      />
      <div className="composer-actions">
        <button type="button" className="attach" aria-label="选择文本资料" disabled={!ready || running} onClick={() => {
          const owner = activePathRef.current
          void window.myrk.selectDocument().then(path => {
            if (path !== null && owner === activePathRef.current) { setDocument(path); setWorkspace(null); setCheckArguments('') }
          }, () => setStatus('无法选择资料；请选择可读取的 .txt（普通文本）或 .md（带简易排版标记的文本）文件。'))
        }}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9"/></svg></button>
        <button type="button" aria-label="选择任务文件夹" disabled={!ready || running} onClick={() => {
          const owner = activePathRef.current
          void window.myrk.selectWorkspace().then(path => {
            if (path !== null && owner === activePathRef.current) {
              if (trustedProjects.some(root => root.replace(/\\/g, '/').replace(/\/$/, '').toLowerCase() === path.replace(/\\/g, '/').replace(/\/$/, '').toLowerCase())) {
                if (workspace !== path) setCheckArguments('')
                setWorkspace(path)
                setDocument(null)
              } else {
                setProjectTrustError('')
                setPendingWorkspace(path)
              }
            }
          }, () => setStatus('无法选择任务文件夹，请确认目录存在且可读取。'))
        }}><Icon name="folder" size={15}/><span>任务文件夹</span></button>
        <ModelControls key={activePath} selection={displayedSelection} models={chatModels} disabled={!ready || running || !configured || !modelSelection} busy={savingModel} legacy={Boolean(runningTask && (runningTask.reasoning == null || runningTask.reasoning === 'default'))} error={modelError} onChange={selectModel} onRefresh={() => fetchModelControls(activePathRef.current, true)} />
        {running && <button type="submit" aria-label="发送补充" disabled={!ready || !runningTask || !draft.trim() || stopping}>发送补充</button>}
        {running && <button type="button" aria-label="排队新工作" disabled={!draft.trim() || stopping} onClick={() => send(undefined, true)}>排队新工作</button>}
        {running ? <button type="button" className="send" aria-label="停止回答" disabled={stopping} onClick={() => {
          setStopping(true)
          setStatus('正在停止；等待本次请求收尾…')
          command({ action: 'stop', path: activePathRef.current })
        }}><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor" stroke="none" /></svg></button>
        : <button type="submit" className="send" aria-label="发送消息" disabled={!ready || savingModel || !draft.trim()}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg>
        </button>}
      </div>
      <label className="turn-approval-choice"><Icon name="shield" size={13}/>新一轮批准方式<select aria-label="新一轮批准方式" title="随新工作发送，并保存供以后使用" value={newTurnApproval} disabled={!ready || stopping} onChange={event => setNewTurnApproval(event.target.value as ApprovalMode | '')}>
        <option value="">沿用会话选择</option><option value="request_approval">请求批准</option><option value="auto_review">帮我批准</option><option value="full_access">完全访问权限</option>
      </select></label>
      {running && newTurnApproval && <p className="field-help">此选择只随“排队新工作”发送；普通补充不会改变本轮批准方式。</p>}
    </form>
    <div className="caption"><span role="status" aria-live="polite">{status}</span><span className="keyboard-hint">Shift（换挡键）+ 回车换行</span></div>
    <p className="attachment-history">本会话以前提供的资料仍可能再次发送；移除待发送附件不会清除历史资料。</p>
  </div>

  const currentTurnApproval = latestTurn?.approval_settings ?? latestTurn?.task?.approval
  const recoveryTurn = recovery ? turns.findLast(turn => turn.task?.id === recovery.task_id && (turn.status === 'running' || turn.service_failure)) : undefined
  const retryableDenials = new Set(latestTurn?.status === 'cancelled' ? [] : turns.filter(turn => turn.task?.id === latestTurn?.task?.id).flatMap(turn => turn.actions ?? []).filter(action => action.task_id === latestTurn?.task?.id && action.status === 'denied' && action.review?.status === 'denied').slice(-10).filter(action => !action.review!.override_used).map(action => action.id))

  const actionEntries = [...new Map(turns.flatMap(turn => (turn.actions ?? []).map(action => [`${action.task_id}:${action.id}`, { action, turn }] as const))).values()]
  const pendingActions = actionEntries.filter(({ action }) => action.status === 'awaiting_approval' || ((action.task_id === latestTurn?.task?.id || action.memory_deferred) && (action.status === 'started' || action.status === 'failed' || action.status === 'conflict' || action.effect === 'unknown' || action.denial_source === 'review_incomplete' || retryableDenials.has(action.id))))
  const awaitingDeliveries = [...new Map(turns.filter(turn => turn.delivery_status === 'awaiting_acceptance' && turn.task).map(turn => [turn.task!.id, turn] as const)).values()]
  const pendingWork = <>
    {(pendingActions.length > 0 || providerChange || recovery || earlierRecoveries.length > 0 || queue.length > 0 || latestTurn?.service_failure || awaitingDeliveries.length > 0) && <section role="region" className="pending-work" aria-label="待处理事项">
      <h2 className="pending-work-heading"><Icon name="shield" size={16}/>待处理事项{pendingActions.length > 0 && <span>{pendingActions.length} 项动作</span>}</h2>
      {pendingActions.map(({ action, turn }) => <ActionDetails key={`${action.task_id}:${action.id}`} action={action} sharedRequests={turn.task?.request_policy === 'shared'} onNotice={setNotice}
        onApprove={(running || action.memory_deferred) && action.status === 'awaiting_approval' && action.approval_id && (action.approval_settings?.reviewer !== 'assistant' || action.review?.human_fallback) ? approved => command({ action: 'approve_action', path: activePathRef.current, task_id: action.task_id, action_id: action.id, approval_id: action.approval_id!, approved }) : undefined}
        onRetry={!running && retryableDenials.has(action.id) ? () => command({ action: 'retry_denied_action', path: activePathRef.current, task_id: action.task_id, action_id: action.id }) : undefined}
        retryDisabled={!taskConnectionAvailable(action.task_id)}/>) }
      {providerChange && !running && <section aria-label="更换服务审阅" className="task-record">
        <h2>审阅本次资料接收方</h2>
        <p>原接收方：{providerChange.previous.name}（理解和回答服务）；地址：{providerChange.previous.base_url}；型号：{providerChange.previous.model}。</p>
        <p>新接收方：{providerChange.target.name}（理解和回答服务）；地址：{providerChange.target.base_url}；型号：{providerChange.target.model}。</p>
        <p>以下列出将使用的必要历史、原允许范围、已做与未做事项。批准后会把这些内容及原范围内按需读取的资料交给上述地址；保留原工作编号、已用次数和主动时间，不重复已经确认完成的动作。</p>
        <pre>{providerChange.materials}</pre><p>{providerChange.reason}</p>
        <button disabled={!connectionAvailable(providerChange.target) || !providerChange.compatible} onClick={() => command({ action: 'decide_provider', path: activePathRef.current, change_id: providerChange.id, approved: true })}>批准更换服务并续办</button>
        <button onClick={() => command({ action: 'decide_provider', path: activePathRef.current, change_id: providerChange.id, approved: false })}>拒绝更换服务</button>
      </section>}
      {!running && [...(recovery ? [recovery] : []), ...earlierRecoveries].map(report => {
        const earlier = report.task_id !== recovery?.task_id
        const task = turns.findLast(turn => turn.task?.id === report.task_id)?.task
        const requestLimit = task?.request_limit
        const blocked = !taskConnectionAvailable(report.task_id) || !report.can_resume || earlier && !!recovery
        return <section key={report.task_id} aria-label={earlier ? '较早工作中断核查' : '中断核查'}>
          {earlier && <p>{task?.goal}</p>}
          <p>{report.message}</p>
          {earlier && recovery && <p>请先处理最近一项中断工作，再续办这一项。</p>}
          {report.actions.map(action => <p key={action.action_id}>动作{action.action_id}：{({ not_started: '未开始', completed: '已完成', unknown: '影响未知' })[action.effect]}。{action.observation}</p>)}
          <button onClick={() => command({ action: 'verify_recovery', path: activePathRef.current, task_id: report.task_id })}>核查真实进展</button>
          <button disabled={blocked} onClick={() => command({ action: 'resume_task', path: activePathRef.current, task_id: report.task_id })}>继续原工作</button>
          {requestLimit != null && <button disabled={blocked} onClick={() => command({ action: 'continue_task', path: activePathRef.current, task_id: report.task_id, request_limit: requestLimit + 10 })}>增加旧记录10次请求并续办原工作</button>}
        </section>
      })}
      {queue.length > 0 && <section aria-label="排队工作" className="queued-tasks">
        <p>排队工作：{queue.length}项</p>
        {queue.map((item, index) => <div key={item.task.id} className="queued-task">
          <p>{index + 1}. {item.task.goal}</p>
          {runningTask && <button type="button" aria-label={`引导，第${index + 1}条`} title="将这条消息交给当前工作"
            disabled={!ready || !activePath || stopping} onClick={() => steerQueued(runningTask.id, item.task.id)}>引导</button>}
        </div>)}
        {!running && (recovery ? recovery.can_resume && recoveryTurn?.status === 'failed' && recoveryTurn.service_failure : !latestTurn?.service_failure) &&
          <button type="button" disabled={!ready} onClick={() => command({ action: 'start_next', path: activePathRef.current })}>继续排队</button>}
      </section>}

      {latestTurn?.service_failure && latestTurn.task && !running && <section aria-label="服务失败后选择"><p>服务失败后已暂停；已做、未做、部分回答及真实错误仍在记录中。由你决定何时续办，或先审阅新的资料接收方。</p><button className="button-primary" disabled={!taskConnectionAvailable(latestTurn.task.id) || recovery?.can_resume === false} onClick={() => command({ action: 'resume_task', path: activePathRef.current, task_id: latestTurn.task!.id })}>继续原工作</button><button disabled={!connectionAvailable(providerConfig) || recovery?.can_resume === false} onClick={() => command({ action: 'preview_provider', path: activePathRef.current, task_id: latestTurn.task!.id, provider_config: providerConfig })}>审阅更换服务</button></section>}
      {!running && awaitingDeliveries.map(turn => <section key={turn.task!.id} className="delivery-pending" aria-label="成果待验收"><p>{turn.task!.goal} · 成果已交付，等你检查接受。</p><button className="button-primary" onClick={() => command({ action: 'accept_delivery', path: activePathRef.current, task_id: turn.task!.id })}>我已接受成果</button></section>)}
    </section>}
  </>
  const currentSupplements = !!supplementTask?.supplements?.length && <section className="current-supplements" aria-label="本项工作的补充">
    <strong>补充</strong>
    <ul>{supplementTask.supplements.map((item, index) => {
      const { label, canRevoke } = supplementStatus(item)
      return <li key={item.id}>
        <div><span>{item.text}</span><small>{label}</small></div>
        {canRevoke && <button type="button" disabled={!ready || stopping} aria-label={`撤回补充，第${index + 1}条`}
          onClick={() => revokeSupplement(supplementTask.id, item.id)}
        >撤回补充</button>}
      </li>
    })}</ul>
  </section>
  const currentWork = turns.length > 0 && <section role="region" className="current-work" aria-label="当前工作"><button type="button" className="current-work-heading" aria-label={workExpanded ? '收起当前工作' : '展开当前工作'} aria-expanded={workExpanded} onClick={() => setWorkExpanded(value => !value)}><Icon name="activity" size={16}/><strong>当前工作</strong><span>{stopping ? '等待本次请求收尾' : running ? '处理中' : '本轮处理已结束'}</span><Icon name="chevronDown" size={15}/></button>
      {workExpanded && <div className="current-work-body">
        {currentSupplements}
        {!!latestTurn?.plan?.length && <ol>{latestTurn.plan.map((step, index) => <li key={index}>{step}</li>)}</ol>}
        {!!latestTurn?.delegations?.length && <div>{latestTurn.delegations.map(work => <p key={work.id}>{work.goal} · {work.status === 'completed' ? ({ pending: '专用核验尚未登记', passed: '已核对', gaps: '核验发现缺口' })[work.verification_status] : ({ running: '办理中', failed: '失败', cancelled: '已停止', limited: '已达上限' })[work.status]}</p>)}</div>}
        {!latestTurn?.plan?.length && !latestTurn?.delegations?.length && <p>{running ? '正在处理你的要求，详细动作可在本轮记录中查看。' : '本轮记录与原始证据保留在对话中。'}</p>}
        <button type="button" className="work-record-link" onClick={() => { const records = messages.current?.querySelectorAll<HTMLDetailsElement>('.turn-records'); const record = records?.[records.length - 1]; if (record) { record.open = true; record.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) } }}>查看本轮记录<Icon name="chevronRight" size={14}/></button>
      </div>}
    </section>

  return <div className={`app${sidebarOpen ? ' sidebar-open' : ''}`} onKeyDown={event => { if (event.key === 'Escape') setSidebarOpen(false) }}>
    {sidebarOpen && <button type="button" className="sidebar-scrim" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)}/>}
    <aside className="sidebar" aria-label="会话导航">
      <div className="brand"><span className="brand-mark">M</span><strong>Myrk</strong><span className="brand-subtitle">桌面助理</span><button type="button" className="sidebar-close" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)}><Icon name="close"/></button></div>
      <nav>
        <button className="new-button" aria-label="新建会话" disabled={!ready} onClick={() => { setSidebarOpen(false); command({ action: 'new' }) }}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg><span>新建会话</span>
        </button>
      </nav>
      <div className="history">
        <label className="search-field"><Icon name="search" size={16}/><input aria-label="搜索会话标题" placeholder="搜索会话标题…" value={query} disabled={!ready} onChange={event => setQuery(event.target.value)} /></label><p className="history-heading">最近会话</p>
        <nav className="history-list" aria-label="本机会话">{conversations
          .filter(item => item.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
          .map(item => <button key={item.path} aria-label={`打开会话：${item.title}`} aria-current={activePath === item.path ? 'page' : undefined}
            disabled={!ready} onClick={() => { setSidebarOpen(false); command({ action: 'open', path: item.path }) }}><Icon name="chat" size={15}/><span>{item.title}</span></button>)}</nav>
        {unreadable > 0 && <p className="history-help">有{unreadable}份记录无法读取或格式不受支持，未列入会话。</p>}
      </div>
      <footer className="sidebar-footer">
        <button aria-label="打开设置" disabled={!ready || running} onClick={() => { setSettingsError(''); setSettings(true); settingsOpen.current = true }}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 3-1 3-3 1-3 3 2 2-1 4 3 2 3-1 3 3 3-2v-3l3-2-1-4-3-1-1-3z"/><circle cx="11" cy="12" r="3"/></svg><span>设置</span>
        </button><span className="local">本机</span>
      </footer>
    </aside>
    <main className="main">
      {saveFailures.length > 0 && <section className="connection-alert" aria-label="记录保存失败">{saveFailures.map((failure, index) => <p role="alert" key={index}>{failure}</p>)}<p>最后可靠保存位置：{lastSavedAt ? activePath : '尚无可靠保存'}；时间：{lastSavedAt ?? '未知'}。</p></section>}
      <header className="topbar"><button type="button" className="sidebar-toggle" aria-label="打开会话列表" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}><Icon name="menu"/></button><div className="title"><strong>{turns[0]?.prompt.replace(/\s+/g, ' ').trim().slice(0, 36) || '新会话'}</strong></div><span className="main-status" data-state={(serviceState === 'failed' || pendingActions.length) ? 'attention' : running ? 'running' : 'idle'}>{serviceState === 'failed' ? '办事程序不可用' : serviceState === 'connecting' ? '正在连接' : stopping ? '正在停止' : running ? '处理中' : pendingActions.length ? '待你处理' : awaitingDeliveries.length > 0 ? '待验收' : turns.length ? '处理已结束' : '准备就绪'}</span><button type="button" className="files-toggle" aria-label="查看会话成果" aria-expanded={filesOpen} ref={filesButton} onClick={() => setFilesOpen(value => !value)}><Icon name="file" size={16}/><span>成果</span></button></header>
      <div className={`conversation-workspace${filesOpen ? ' files-open' : ''}`}>
      <div className="conversation-pane">
      <details className="approval-controls conversation-info" aria-label="批准方式设置"><summary><Icon name="shield" size={15}/>会话信息与批准方式<Icon name="chevronDown" size={14}/></summary>
        <p>本机默认：{approvalLabels[approvalMode]}；会话{conversationApproval ? '已保存' : '选择'}：{conversationApproval ? approvalLabels[conversationApproval.mode] : '原记录未记载（明确保存后才采用新方式）'}。</p>
        {conversationApproval?.access.legacy_scope && <p>原记录授权仍保留；明确保存才采用新方式。</p>}
        <button type="button" aria-label="修改会话批准方式" disabled={!ready || stopping || savingConversationApproval} onClick={() => setConversationApprovalOpen(true)}>修改会话批准方式</button>
        {running && latestTurn?.task && currentTurnApproval && <div>
          <p>本轮实际访问范围：{currentTurnApproval.access.sandbox === 'full_access' ? '完全访问（移除一般本地文件与联网限制）' : currentTurnApproval.access.legacy_scope ? '原记录授权范围（沿用旧工作已确认的限制）' : '普通系统限制（由电脑系统限制文件写入与联网）'}。</p>
          {currentTurnApproval.condition === 'never' ? <p>本轮免普通逐项批准；若要更换实际访问范围，请为新一轮保存会话批准方式。</p> : <label>本轮后续新步骤审核者（谁来决定后续需要批准的动作）<select aria-label="本轮后续新步骤审核者" value={currentTurnApproval.reviewer} disabled={!ready || stopping || savingTurnReviewer} onChange={event => {
            setSavingTurnReviewer(true)
            command({ action: 'set_turn_reviewer', path: activePathRef.current, task_id: latestTurn.task!.id, reviewer: event.target.value as 'user' | 'assistant' })
          }}><option value="user">你本人（由你决定）</option><option value="assistant">独立助理（另一位负责审核的助理）</option></select></label>}
          <p>这里只改变后续新准备的步骤；已经准备的动作和现有分工保留原设置。</p>
        </div>}
      </details>
      {turns.length ? <>
        <section className="conversation" aria-label="对话内容" ref={messages} onScroll={event => {
          const content = event.currentTarget
          if (imageReading.current || content.scrollTop === restoredImageTop.current) return
          restoredImageTop.current = null
          const following = content.scrollHeight - content.scrollTop - content.clientHeight <= 96
          followLatest.current = following
          setFollowingLatest(following)
        }}>
          <CompactionRecords key={activePath} records={compactions} activeTaskId={running ? latestTurn?.task?.id ?? null : null} evidence={compactionEvidence} onRead={(summary_id, source, start) => {
            setCompactionEvidence(null)
            command({ action: 'get_compaction_evidence', path: activePath, summary_id, message_index: source.message_index, part_index: source.part_index, start })
          }} />
          <div className="thread">{turns.map((turn, index) => {
            const account = turn.request_owner_id == null ? turn.task : turn.request_account
            const executionScope = turn.request_owner_id == null ? turn.execution_scope ?? turn.task?.scope : turn.execution_scope
            return <div key={index}>
            <div className="user-message"><div className="bubble">{turn.prompt}</div></div>
            <article className="answer" aria-label="Myrk 的回答">
              <header className="answer-header"><span className="brand-mark">M</span><strong>Myrk</strong><span>桌面助理</span></header>
              {index === turns.length - 1 && currentWork}
              <div className="answer-body"><Markdown components={{ img: ({ alt }) => <span>{alt || '图片'}</span> }}>{turn.answer}</Markdown>{!turn.answer && turn.status === 'running' && <span className="waiting">{running && index === turns.length - 1 ? '正在等待回答…' : '该次处理曾中断；原始进度保留。'}</span>}</div>
              {turn.artifacts?.filter(artifact => /\.(png|jpe?g|gif|webp)$/i.test(artifact.name)).map(artifact => <DeliveredImage key={artifact.id} artifact={artifact} conversationId={activePath} onReadingChange={imageReadingChanged}/>)}
              {turn.context_notes?.map((note, index) => <p key={index} className="field-help">{note}</p>)}
              <details className="turn-records"><summary><Icon name="activity" size={15}/>查看本轮记录<span>动作、用量与原始证据</span><Icon name="chevronDown" size={14}/></summary>
              {turn.task && executionScope && <details className="task-record"><summary>任务范围与资料接收方</summary>
                <p>任务编号：{turn.task.id}</p><p>目标：{turn.task.goal}</p><p>{turn.task.approval && !turn.task.approval.access.legacy_scope ? '默认工作位置：' : '原授权文件范围：'}{executionScope.root}</p>
                <p>允许动作：{executionScope.allowed_actions.map(action => ({ create: '新建', write: '整份写入', command: '运行程序', list: '列举', read: '读取', search: '搜索', edit: '精确修改', calculate: '文字表格计算', check: '指定检查' })[action]).join('、')}</p>
                <p>接收方：{turn.task.provider_config?.name ?? defaultProvider.name}（理解和回答服务）；地址：{turn.task.provider_config?.base_url ?? defaultProvider.base_url}；型号：{turn.task.provider_config?.model ?? defaultProvider.model}；{turn.task.request_policy === 'shared' ? `旧记录共用请求上限${turn.task.request_limit}次` : turn.request_owner_id != null ? `子助理办事轮数上限${account?.max_turns ?? '未知'}轮` : '主助理不设办事轮数上限'}。</p>
              </details>}
              {(turn.approval_settings ?? turn.task?.approval) && <p className="turn-approval-record">本轮保存设置：准备时方式为{approvalLabels[(turn.approval_settings ?? turn.task!.approval)!.mode]}；审核者（需要批准时由谁决定）：{({ user: '你本人', assistant: '独立助理（另一位负责判断的助理）', none: '免普通逐项批准' })[(turn.approval_settings ?? turn.task!.approval)!.reviewer]}。每项动作仍采用自己准备时保存的设置。</p>}
              {turn.task && <div className="task-progress"><p>{turn.task.request_policy === 'shared' ? `旧记录共用次数：${turn.task.requests ?? turn.requests}/${turn.task.request_limit}次` : turn.request_owner_id != null ? account ? `子助理已办${account.turns_used ?? 0}/${account.max_turns ?? '未知'}轮；实际请求尝试：${account.requests ?? turn.requests}次` : '子助理自己的次数记录未知' : `主助理不设办事轮数上限；已办${account?.turns_used ?? 0}轮；实际请求尝试：${account?.requests ?? turn.requests}次`}；主动运行：{(turn.task.active_seconds ?? 0).toFixed(1)}秒。</p><p>整项累计请求次数：{turn.task.total_requests ?? turn.task.requests ?? turn.requests}次，仅作统计。</p><p>服务实际用量：{turn.input_tokens === null || turn.output_tokens === null ? '未知' : `输入${turn.input_tokens}、输出${turn.output_tokens}个文字计量单位`}。</p>{(account?.review_requests ?? 0) > 0 && <p>其中独立审核：{account?.review_requests}次，{(account?.review_seconds ?? 0).toFixed(1)}秒，已计入上述请求次数。审核实际用量：{(account?.review_unmeasured_requests ?? 0) > 0 ? `未知（有${account?.review_unmeasured_requests}次未报告用量）` : account?.review_input_tokens == null || account?.review_output_tokens == null ? '未知' : `输入${account?.review_input_tokens}、输出${account?.review_output_tokens}个文字计量单位（服务统计文字量时使用的单位）`}。</p>}{turn.task.supplements?.map(item => <p key={item.id}>补充：{item.text} · {supplementStatus(item).label}</p>)}</div>}
              {!!turn.request_attempts?.length && <details className="task-record"><summary>每次实际请求与失败决定</summary>{turn.request_attempts.map(attempt => <div key={attempt.number}>
                <p>用途：{attempt.purpose === 'compaction' ? '整理较早记录' : '原工作及动作判断'}。</p>
                <p>第{attempt.number}次请求；接收地址：{attempt.provider}；回答型号：{attempt.model}；{attempt.status === 'completed' ? '已收到回应' : attempt.status === 'failed' ? '失败' : '等待回应'}。</p>
                <p>请求归属：{requestActorLabels[attempt.actor ?? 'main']}；{turn.task?.request_policy === 'independent' ? `计入这名助理的实际请求次数${attempt.own_number == null ? '' : `，本助理第${attempt.own_number}次`}` : '已计入旧记录共用次数'}。</p>
                {attempt.delegation_id && <p>所属分工：{attempt.delegation_id}。</p>}
                {attempt.http_status !== null && <p>HTTP（网络回应状态）：{attempt.http_status}。</p>}
                {attempt.error && <p>{attempt.error}</p>}<p>{attempt.decision}</p>
                <p>此次服务实际用量：{attempt.input_tokens === null || attempt.output_tokens === null ? '未知' : `输入${attempt.input_tokens}、输出${attempt.output_tokens}个文字计量单位`}。</p>
                <RequestObservationDetails observation={attempt.observation} />
              </div>)}</details>}
              {!!turn.skill_catalog?.length && <details className="task-record"><summary>技能目录（可复用的办事步骤，选中后才读正文）</summary>{turn.skill_catalog.map(skill => <p key={skill.name}>{skill.name}（步骤名称）：{skill.description}；位置：{skill.path}</p>)}</details>}
              {!!turn.plan?.length && <details className="task-record"><summary>当前办事步骤</summary><ol>{turn.plan.map((step, index) => <li key={index}>{step}</li>)}</ol></details>}
              {!!turn.delegations?.length && <details className="task-record"><summary>按需分工与主助理核验</summary>{turn.delegations.map(work => <div key={work.id}>
                <p>分工编号：{work.id}；目标：{work.goal}</p>
                <p>必要资料：{work.context}；允许文件范围：{work.scope.root}</p>
                <p>证据要求：{work.evidence_requirements}；应交文件：{work.expected_files.join('、')}</p>
                <p>{work.status === 'running' ? '分工办理中' : '分工处理结束'}；{work.verification_status === 'passed' ? '主助理已核对所列真实原文' : work.verification_status === 'gaps' ? '主助理核验发现缺口' : '专用核验尚未登记'}；{turn.task?.request_policy === 'independent' ? `本助理已办${work.turns_used ?? 0}/${work.max_turns ?? turn.task.delegation_max_turns ?? 250}轮；实际请求尝试：${work.requests}次，独立计次` : `本助理请求尝试：${work.requests}次，已包含在旧记录共用次数中`}。</p>
                {work.report && <pre>{work.report}</pre>}{work.error && <p role="alert">{work.error}</p>}
                <p>保留本任务分工的原始记录，供回查与继续：{work.record_path}。记录是任务证据；用户文件和共享资料不会随分工结束删除。</p>
              </div>)}</details>}
              {(turn.actions?.length ?? 0) > 0 && <details className="action-records"><summary>真实动作记录</summary>
                {turn.actions!.map(action => <ActionDetails key={action.id} action={action} sharedRequests={turn.task?.request_policy === 'shared'} onNotice={setNotice}/>)}</details>}
              {turn.execution_facts && <section className="task-record" aria-label="程序记录的事实"><h3>程序记录的事实</h3>{turn.execution_facts.split('\n').map((line, index) => <p key={index}>{line}</p>)}</section>}
              </details>
              {turn.delivery_status && turn.delivery_status !== 'not_delivered' && <p>本次处理结束；{turn.checks_passed === true ? '实际检查通过' : turn.checks_passed === false ? '实际检查未通过' : '未运行指定检查'}；{turn.delivery_status === 'accepted' ? '用户已接受' : '待验收（成果已交付，等你检查接受）'}。</p>}
              {(turn.answer || turn.status !== 'running') && <div className="answer-actions">
                {turn.answer && <button aria-label="复制回答" onClick={() => {
                  void window.myrk.copy(turn.answer).then(() => setNotice('已复制回答。'), () => setNotice('复制失败，请直接选择文字复制。'))
                }}><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="12" rx="2"/><path d="M15 8V4H4v12h4"/></svg></button>}
                {turn.status === 'cancelled' && <span>已停止 · 回答未完成</span>}
                {turn.status === 'failed' && <span>本轮失败 · 回答未完成</span>}
                {turn.status === 'limited' && <span>已达上限 · 回答未完成</span>}
                {turn.status === 'limited' && turn.task?.request_limit != null && index === turns.length - 1 && !running && <button aria-label="增加额度并继续" onClick={() => command({ action: 'continue_task', path: activePathRef.current, task_id: turn.task!.id, request_limit: turn.task!.request_limit! + 10 })}>增加旧记录10次请求并继续</button>}
              </div>}
              {turn.error && <p className="field-help">{turn.error}{turn.status === 'failed' && !turn.service_failure && !turn.error.includes('保存') && ' 请检查连接密码和网络后重试，或调整问题后再发送。'}</p>}
            </article>
          </div>})}</div>
        </section>
        <footer className="composer-region">{!followingLatest && <button type="button" className="follow-latest" onClick={() => {
          restoredImageTop.current = null
          followLatest.current = true
          setFollowingLatest(true)
          if (messages.current) messages.current.scrollTop = messages.current.scrollHeight
        }}>回到最新内容<Icon name="chevronDown" size={14}/></button>}{pendingWork}{composer}</footer>
      </> : <section className="welcome"><span className="welcome-mark">M</span><p className="welcome-eyebrow">你的桌面助理</p><h1>今天想完成什么？</h1>{composer}<p className="welcome-note">从一个问题开始。</p></section>}
      </div>
      {filesOpen && <ConversationFilesPanel key={activePath} onClose={closeFiles}>{turns.some(turn => turn.artifacts?.length) ? <ArtifactWorkspace conversationId={activePath} artifacts={turns.flatMap(turn => turn.artifacts ?? [])} onImageReadingChange={imageReadingChanged}/> : undefined}</ConversationFilesPanel>}
      </div>
      {notice && <div className="notice" role="status">{notice}<button type="button" aria-label="关闭提示" onClick={() => setNotice('')}><Icon name="close" size={14}/></button></div>}
    </main>
    {pendingWorkspace && <FolderTrustDialog path={pendingWorkspace} mode={newTurnApproval || conversationApproval?.mode || approvalMode} busy={savingProjectTrust} error={projectTrustError} onClose={() => {
      pendingProjectTrust.current = null
      setSavingProjectTrust(false)
      setProjectTrustError('')
      setPendingWorkspace(null)
    }} onTrust={() => {
      pendingProjectTrust.current = { root: pendingWorkspace, owner: activePathRef.current, resetChecks: workspace !== pendingWorkspace }
      setSavingProjectTrust(true)
      setProjectTrustError('')
      command({ action: 'set_project_trust', root: pendingWorkspace, trusted: true })
    }} />}
    {conversationApprovalOpen && <ConversationApprovalDialog mode={conversationApproval?.mode ?? 'request_approval'} busy={savingConversationApproval}
      onClose={() => setConversationApprovalOpen(false)} onSave={approval_mode => {
        setSavingConversationApproval(true)
        command({ action: 'set_conversation_approval', path: activePathRef.current, approval_mode })
      }} />}
    {settings && <SettingsDialog value={{ providerConfig: settingsProvider, delegationMaxTurns, approvalMode }} configured={configured} busy={savingSettings || !ready || running} error={settingsError} memoryState={memoryState} modelCatalog={modelCatalog} connectionStorage={connectionStorage}
      onForget={provider_config => {
        forgettingConnection.current = true
        setSavingSettings(true)
        setSettingsError('')
        void window.myrk.command({ action: 'forget_connection', provider_config }).catch(() => {
          forgettingConnection.current = false
          setSavingSettings(false)
          setSettingsError('未能忘记此连接密码，请重试。')
        })
      }}
      onFetchCatalog={request => window.myrk.command(request)}
      onClose={() => { setSettings(false); settingsOpen.current = false; setSettingsError('') }}
      onApprovalSave={approval_mode => {
        pendingApproval.current = true
        setSavingSettings(true)
        setSettingsError('')
        void window.myrk.command({ action: 'configure', approval_mode }).catch(() => {
          pendingApproval.current = false
          setSavingSettings(false)
          setSettingsError('默认批准方式未能保存，请重试。')
        })
      }}
      onSave={(value, key) => {
        pendingSettings.current = value
        setSavingSettings(true)
        setSettingsError('')
        void window.myrk.command({ action: 'configure', ...(key ? { key } : {}), delegation_max_turns: value.delegationMaxTurns,
          ...(JSON.stringify(value.providerConfig) === JSON.stringify(defaultProvider) && JSON.stringify(settingsProvider) === JSON.stringify(defaultProvider) && JSON.stringify(providerConfig) === JSON.stringify(defaultProvider) ? {} : { provider_config: value.providerConfig }) }).catch(() => {
          pendingSettings.current = null
          setSavingSettings(false)
          setSettingsError('未能保存设置，请检查连接资料或等待其他工作收尾后重试。')
        })
      }}
      onMemoryList={() => command({ action: 'memory_list' })}
      onMemorySettings={approve_writes => command({ action: 'memory_settings', approve_writes })}
      onMemoryChange={(changes, project) => {
        setSettings(false)
        settingsOpen.current = false
        command({ action: 'memory_change', path: activePathRef.current, ...(project ? { project } : {}), changes })
      }}
      onImport={() => {
        setSettingsError('')
        void window.myrk.importConversation().catch(() => setSettingsError('未能导入记录。若仍有工作进行，请等待全部工作收尾后重试；也请检查记录位置。'))
      }} />}
  </div>
}
