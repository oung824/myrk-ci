import { useEffect, useRef, useState, type ReactNode } from 'react'
import { LayoutVariants } from './LayoutVariants'
import { PreviewWorkspace, previewFiles } from './PreviewWorkspace'
import './prototype.css'

type Variant = 'A' | 'B' | 'C'
type Phase = 'complete' | 'running' | 'approval' | 'error'
type Scenario = 'documents' | 'code' | 'new'
type Modal = 'image' | 'search' | 'settings' | 'folder' | null
const variants: Variant[] = ['A', 'B', 'C']
const variantNames = { A: '对话优先', B: '阅读优先', C: '任务导航优先' }
const phaseNames = { complete: '已完成', running: '正在处理', approval: '等待你确认', error: '需要处理' }

function Glyph({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
    chat: <path d="M5 4h14v12H9l-4 4V4Z" />,
    folder: <path d="M3 6h7l2 2h9v12H3V6Z" />,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></>,
    file: <><path d="M6 3h8l4 4v14H6V3Z" /><path d="M14 3v5h4M9 12h6M9 16h6" /></>,
    arrow: <path d="m7 11 5-5 5 5M12 6v13" />,
    down: <path d="m8 10 4 4 4-4" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    settings: <><circle cx="12" cy="12" r="3" /><path d="m10 3-.5 3-3 1-2-1-2 3 2 2v3l-2 2 2 3 3-1 2 1 .5 3h4l.5-3 3-1 2 1 2-3-2-2v-3l2-2-2-3-3 1-2-1-.5-3h-4Z" /></>,
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8" cy="9" r="1.5" /><path d="m3 17 5-5 4 4 3-3 6 6" /></>,
    shield: <><path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6l8-3Z" /><path d="m8 12 3 3 5-6" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.file}</svg>
}

function Mark({ small = false }: { small?: boolean }) {
  return <span className={small ? 'myrk-mark small' : 'myrk-mark'} aria-hidden="true">M</span>
}

function SampleImage() {
  return <svg className="sample-image" viewBox="0 0 800 420" role="img" aria-label="方案配图：山间湖泊与自然色彩"><defs><linearGradient id="sky" x2="0" y2="1"><stop stopColor="#d6e2db" /><stop offset="1" stopColor="#eff0df" /></linearGradient><linearGradient id="lake" x2="0" y2="1"><stop stopColor="#a7b8ad" /><stop offset="1" stopColor="#d8ddd0" /></linearGradient></defs><rect width="800" height="420" fill="url(#sky)" /><circle cx="594" cy="101" r="36" fill="#f7f3de" /><path d="M0 243 164 70l158 178L461 92l177 155 162-109v282H0Z" fill="#98aaa0" /><path d="m164 70 59 68-58-22-32 21Zm297 22 57 50-54-17-32 17Z" fill="#e8ece1" /><path d="m0 275 118-95 127 89 129-81 167 87 140-95 119 62v178H0Z" fill="#647e72" /><path d="M0 286q168-30 342 0t458 0v134H0Z" fill="url(#lake)" /><g stroke="#e5e8db" strokeWidth="2" opacity=".7"><path d="M94 322h181m224 12h147M262 363h233m-78 28h232" /></g><path d="m0 388 132-38 85 70H0Zm800-48-108 28-75 52h183Z" fill="#405f50" /><text x="35" y="48" fill="#355144" fontFamily="Georgia, serif" fontSize="21" letterSpacing="3">A LITTLE CLOSER TO NATURE</text></svg>
}

export function PrototypeApp() {
  const initial = new URLSearchParams(location.search).get('variant')
  const [variant, setVariant] = useState<Variant>(variants.includes(initial as Variant) ? initial as Variant : 'A')
  const [scenario, setScenario] = useState<Scenario>('documents')
  const [phase, setPhase] = useState<Phase>('complete')
  const [denied, setDenied] = useState(false)
  const [activeFile, setActiveFile] = useState<string | null>(null)
  const [navOpen, setNavOpen] = useState(false)
  const [navHidden, setNavHidden] = useState(false)
  const [modal, setModal] = useState<Modal>(null)
  const [query, setQuery] = useState('')
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<string[]>([])
  const [queued, setQueued] = useState<string[]>([])
  const [composeMode, setComposeMode] = useState('补充要求')
  const [menu, setMenu] = useState<string | null>(null)
  const [model, setModel] = useState('DeepSeek V3.2')
  const [approval, setApproval] = useState('操作前询问')
  const [folder, setFolder] = useState('品牌提案')
  const [attached, setAttached] = useState(false)
  const [zoom, setZoom] = useState(100)
  const [notice, setNotice] = useState('')
  const modalRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const isCode = scenario === 'code'
  const title = scenario === 'new' ? '新对话' : isCode ? '让搜索结果更准确' : '准备一份秋季品牌提案'
  const visibleFiles = scenario === 'new' ? [] : previewFiles.slice(0, 6).filter(file => isCode ? ['search', 'notes'].includes(file.id) : file.id !== 'search')

  function changeVariant(next: Variant) {
    setVariant(next)
    const url = new URL(location.href)
    url.searchParams.set('variant', next)
    history.replaceState(null, '', url)
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') { setModal(null); setMenu(null); setNavOpen(false); return }
      const target = event.target as HTMLElement
      if (modal || target.closest('input, textarea, select, [contenteditable="true"]')) return
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        changeVariant(variants[(variants.indexOf(variant) + (event.key === 'ArrowRight' ? 1 : 2)) % 3])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [variant, modal])

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 3500)
    return () => clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    if (!modal) return
    const previous = document.activeElement as HTMLElement
    const dialog = modalRef.current!
    dialog.querySelector<HTMLElement>('button, input')?.focus()
    function trap(event: KeyboardEvent) {
      if (event.key !== 'Tab') return
      const controls = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, textarea, [tabindex="0"]')]
      const first = controls[0], last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    dialog.addEventListener('keydown', trap)
    return () => { dialog.removeEventListener('keydown', trap); previous?.focus() }
  }, [modal])

  function chooseScenario(next: Scenario) {
    setScenario(next); setPhase('complete'); setDenied(false); setComposeMode('补充要求'); setActiveFile(null); setMessages([]); setQueued([])
    setDraft(''); setNavOpen(false); setModal(null); setMenu(null)
    setFolder(next === 'code' ? 'Myrk' : '品牌提案')
  }

  function openFile(id: string) { setActiveFile(id); setMenu(null) }

  function send() {
    if (!draft.trim()) return
    if (phase === 'running' && composeMode === '稍后处理') {
      setQueued([...queued, draft.trim()]); setNotice('已加入待办示例')
    } else {
      setMessages([...messages, draft.trim()]); setScenario(scenario === 'new' ? 'documents' : scenario)
      setPhase('running')
    }
    setDraft(''); setAttached(false)
  }

  const nav = <aside className="app-nav" aria-label="会话导航">
    <div className="app-brand"><Mark small /><strong>Myrk</strong><button className="icon-button nav-collapse" aria-label="收起导航" onClick={() => { setNavOpen(false); if (variant === 'A' && innerWidth > 700) setNavHidden(true) }}><Glyph name="panel" /></button></div>
    <div className="nav-links">
      <button onClick={() => chooseScenario('new')}><Glyph name="plus" /><span>新对话</span><kbd>＋</kbd></button>
      <button onClick={() => { setQuery(''); setModal('search') }}><Glyph name="search" /><span>搜索对话</span></button>
      <button onClick={() => setModal('folder')}><Glyph name="folder" /><span>项目</span><span className="subtle">2</span></button>
    </div>
    <div className="session-list"><div className="nav-section-label">最近的对话</div>
      {[{ key: 'documents', label: '准备一份秋季品牌提案', time: '今天' }, { key: 'code', label: '让搜索结果更准确', time: '今天' }].map(item => <button key={item.key} className={scenario === item.key ? 'session active' : 'session'} onClick={() => chooseScenario(item.key as Scenario)}><Glyph name="chat" size={16} /><span>{item.label}</span></button>)}
      <div className="nav-section-label older">昨天</div>
      <button className="session" onClick={() => { chooseScenario('documents'); setNotice('已打开保留成果的会话示例') }}><Glyph name="chat" size={16} /><span>整理项目资料</span></button>
      <button className="session" onClick={() => { chooseScenario('code'); setNotice('已打开历史对话示例') }}><Glyph name="chat" size={16} /><span>梳理应用的使用流程</span></button>
    </div>
    <div className="nav-footer"><div className="local-status"><span /> 本机工作区</div><button onClick={() => setModal('settings')}><span className="profile-avatar">M</span><span><strong>我的空间</strong><small>连接与偏好设置</small></span><Glyph name="settings" size={17} /></button></div>
  </aside>

  const artifactButtons = <div className="artifact-list">{visibleFiles.map(file => <button className={activeFile === file.id ? 'artifact-row selected' : 'artifact-row'} key={file.id} onClick={() => openFile(file.id)}><span className={`file-type type-${file.kind}`}>{file.kind === 'code' ? '</>' : file.name.split('.').at(-1)?.toUpperCase()}</span><span className="artifact-name">{file.name}<small>{file.status === 'modified' ? '已修改' : '新建文件'}</small></span><span className="file-open-arrow">↗</span></button>)}</div>

  const outline = <div className="task-outline"><div className="outline-heading"><span>当前任务</span><span className={`phase-dot ${phase}`} /></div><h3>{title}</h3><span className="outline-subtitle">{scenario === 'new' ? '开始一个新任务' : phaseNames[phase]}</span>{scenario !== 'new' && <ol className="task-steps"><li><span>✓</span>了解需求与已有资料</li><li><span>✓</span>{isCode ? '定位搜索逻辑' : '整理结构与设计方向'}</li><li><span>{phase === 'complete' ? '✓' : '3'}</span>{isCode ? '修改与验证' : '制作文档与配图'}</li></ol>}<div className="outline-heading artifacts-heading">成果 <span>{visibleFiles.length}</span></div>{artifactButtons}<p className="outline-footnote">{scenario === 'new' ? '完成任务后，交付的文件会列在这里' : '成果随这段对话保存'}</p></div>

  const header = <header className="chat-header"><button className="icon-button" aria-label="打开会话导航" onClick={() => { if (variant === 'A' && innerWidth > 700) setNavHidden(!navHidden); else setNavOpen(!navOpen) }}><Glyph name="panel" /></button><div className="header-title">{title}<span>仅在本机</span></div><div className="header-actions"><span className="prototype-label">原型 · 示例数据</span><button className="quiet-button results-toggle" onClick={() => setMenu(menu === 'results' ? null : 'results')}><Glyph name="file" size={16} />成果 <span>{visibleFiles.length}</span></button><button className="icon-button" aria-label="对话选项" onClick={() => setMenu(menu === 'conversation' ? null : 'conversation')}><Glyph name="more" /></button></div>
    {menu === 'results' && <div className="header-popover results-popover"><div className="popover-title">本次成果 <small>只包含交付给你的文件</small></div>{artifactButtons}<details><summary>查看边界情况示例</summary>{previewFiles.slice(6).map(file => <button className="boundary-file" key={file.id} onClick={() => openFile(file.id)}><Glyph name="file" size={15} />{file.name}</button>)}</details></div>}
    {menu === 'conversation' && <div className="header-popover"><button onClick={() => { setMenu(null); chooseScenario('new') }}>新建对话</button><button onClick={() => { setMenu(null); setModal('settings') }}>会话设置</button></div>}
  </header>

  const progress = <div className={`progress-block phase-${phase}`}>
    <div className="progress-heading"><span className={`status-symbol ${phase}`}>{phase === 'complete' ? <Glyph name="check" size={16} /> : phase === 'running' ? <span className="spinner" /> : phase === 'approval' ? '?' : '!'}</span><strong>{phase === 'complete' ? '全部准备好了' : phase === 'running' ? isCode ? '正在检查搜索结果…' : '正在完善提案与预算…' : phase === 'approval' ? '需要你确认一个操作' : denied ? '本次操作未获允许' : '文件暂时无法保存'}</strong><span className="progress-time">{phase === 'complete' ? '用时 2 分 18 秒' : '当前任务'}</span></div>
    {phase === 'approval' && <div className="approval-content"><p>{isCode ? '将修改项目中的搜索文件，保留修改前的版本供你对照。' : '将更新「品牌提案」文件夹中的预算表，保留原文件备份。'}</p><div className="permission-path"><Glyph name="folder" size={16} />{isCode ? 'E:\\项目\\Myrk\\search.ts' : 'E:\\项目\\品牌提案\\budget.xlsx'}</div><div className="inline-actions"><button className="primary-button" onClick={() => setPhase('running')}>允许本次</button><button className="quiet-button" onClick={() => { setDenied(true); setPhase('error'); setNotice('已拒绝示例操作') }}>拒绝</button></div></div>}
    {phase === 'error' && <div className="approval-content"><p>{denied ? '你拒绝了这次操作，文件没有改动。可以重新确认，或继续讨论其他方案。' : isCode ? 'search.ts 暂时无法写入。可以检查文件是否只读，已有对话和成果仍保留。' : 'budget.xlsx 正在被其他程序占用。关闭文件后可以继续，已有对话和成果仍保留。'}</p><button className="quiet-button outlined" onClick={() => { setPhase(denied ? 'approval' : 'running'); setDenied(false) }}>{denied ? '重新确认' : '重新尝试'}</button></div>}
    <details className="execution-details"><summary>{phase === 'complete' ? '查看处理过程' : '查看当前进展'}<span>{phase === 'complete' ? '4 个步骤' : '已完成 2 / 4'}</span></summary><ol><li><span>✓</span>{isCode ? '阅读搜索组件与已有检查' : '阅读品牌资料与原始预算'}</li><li><span>✓</span>{isCode ? '调整排序和空结果提示' : '整理提案结构与视觉方向'}</li><li><span>{phase === 'complete' ? '✓' : '·'}</span>{isCode ? '运行现有检查：通过' : '制作文档、预算表与演示稿'}</li><li><span>{phase === 'complete' ? '✓' : '·'}</span>核对成果与交付说明</li></ol><div className="execution-note">示例执行记录 · 读取的资料与日志不会列入成果</div></details>
  </div>

  const conversation = <section className="conversation-pane" aria-label="对话">
    <div className="conversation-scroll" data-testid="conversation-scroll">
      {scenario === 'new' ? <div className="welcome"><Mark /><h1>今天想一起完成什么？</h1><p>从一个问题、一份资料，或一个想法开始。</p><div className="welcome-prompts"><button onClick={() => chooseScenario('documents')}><Glyph name="file" />准备一份品牌提案</button><button onClick={() => chooseScenario('code')}><Glyph name="folder" />改进我的项目</button></div></div> : <div className="message-column">
        <div className="user-message">{isCode ? '帮我改进项目中的搜索：优先显示标题匹配的结果，也把没有结果时的提示整理一下。' : '请根据品牌资料，准备一份秋季活动提案，包含执行预算和汇报用的演示稿。整体自然、清晰，别太商业化。'}<div className="user-attachments"><span><Glyph name="folder" size={14} />{isCode ? 'Myrk' : '品牌资料'}<span>工作文件夹</span></span></div></div>
        <article className="assistant-message"><div className="assistant-identity"><Mark small /><span>Myrk</span></div>{progress}
          {phase === 'complete' ? <div className="answer-body"><h1>{isCode ? '搜索结果更贴近你要找的内容了。' : '让秋季活动，从一次自然的相遇开始。'}</h1><p>{isCode ? '我把标题匹配放到了前面，保留正文匹配作为补充。没有结果时，也会给出更具体的搜索建议。' : '我围绕「向自然靠近一点」整理了整套提案。重点放在轻量的线下体验，让活动从品牌表达延伸到实际参与。'}</p>
          <ul>{isCode ? <><li><strong>结果排序：</strong>标题完整匹配、标题部分匹配、正文匹配。</li><li><strong>空结果提示：</strong>保留关键词，并提示尝试更短的词。</li></> : <><li><strong>提案：</strong>活动主题、参与方式和执行节奏。</li><li><strong>预算：</strong>按场地、物料、人员拆分，可逐项核对。</li><li><strong>演示稿：</strong>从背景到落地，适合 10 分钟汇报。</li></>}</ul>
          <div className="delivery-files">{visibleFiles.filter(file => isCode ? file.name.endsWith('.ts') || file.name.endsWith('.md') : /\.(docx|xlsx|pptx)$/.test(file.name)).map(file => <button className="delivery-card" key={file.id} onClick={() => openFile(file.id)}><span className={`file-type type-${file.kind}`}>{file.name.split('.').at(-1)?.toUpperCase()}</span><span><strong>{file.name}</strong><small>{file.status === 'modified' ? '已修改 · 点击查看' : '新建 · 点击预览'}</small></span><span>↗</span></button>)}</div>
          {!isCode && <><p className="image-intro">视觉方向可以参考这张配图：</p><button className="inline-image" aria-label="打开配图大图" onClick={() => { setZoom(100); setModal('image') }}><SampleImage /><span><Glyph name="image" size={14} />秋季视觉方向.svg <span>点击放大 ↗</span></span></button><p>预算中的场地费用仍是估算值，确定活动城市后可以再细化。</p></>}
          {isCode && <p>打开右侧代码查看区，可以查看完整文件，也可以对照这次修改。</p>}
          </div> : <div className="answer-body"><h1>{phase === 'running' ? isCode ? '让每次搜索，都更有方向。' : '把一个自然的想法，变成可执行的提案。' : phase === 'approval' ? '确认后，我会继续完成这一步。' : denied ? '这一步已停下，等你决定下一步。' : '处理好文件访问后，可以接着完成。'}</h1><p>{phase === 'running' ? isCode ? '已梳理标题与正文的匹配顺序，接下来核对空结果提示和现有检查。你也可以继续补充要求。' : '资料和整体方向已经整理好。我正在核对预算与演示稿，已有成果仍可以从右上角打开。' : '你可以继续提问或查看已有成果，当前对话会保留在这里。'}</p></div>}<div className="message-actions"><button onClick={() => setNotice('已标记这条示例回复有帮助')}>有帮助</button><span>·</span><button onClick={() => { composerRef.current?.focus(); setDraft('请再调整一下：') }}>继续调整</button></div>
        </article>
        {messages.map((message, index) => <div key={index} className="follow-up"><div className="user-message">{message}</div><div className="assistant-identity"><Mark small /><span>Myrk</span></div><p>收到，我会结合这个要求继续调整，并把更新后的文件放在这段对话的成果里。</p></div>)}
      </div>}
    </div>
    <div className="composer-area">
      {queued.length > 0 && <div className="queued-tasks"><span>接下来处理 · {queued.length}</span>{queued.map((task, i) => <div key={i}><span>{task}</span><button className="icon-button" aria-label={`移除待办 ${i + 1}`} onClick={() => setQueued(queued.filter((_, j) => i !== j))}><Glyph name="close" size={13} /></button></div>)}</div>}
      {phase === 'running' && <div className="running-controls"><span><span className="spinner" /> 正在处理，你可以继续补充</span><button onClick={() => { setPhase('complete'); setNotice('已停止示例任务') }}><Glyph name="stop" size={13} />停止</button></div>}
      <form className="composer" onSubmit={event => { event.preventDefault(); send() }}>
        {phase === 'running' && <div className="compose-modes">{['补充要求', '稍后处理'].map(item => <button key={item} className={composeMode === item ? 'selected' : ''} onClick={() => setComposeMode(item)} type="button">{item}</button>)}</div>}
        {attached && <div className="attached-file"><Glyph name="file" size={14} />品牌资料.pdf <button type="button" className="icon-button" aria-label="移除附件" onClick={() => setAttached(false)}><Glyph name="close" size={12} /></button></div>}
        <textarea ref={composerRef} aria-label="发送消息" placeholder={scenario === 'new' ? '有什么需要我帮忙的？' : '补充你的想法，或继续调整…'} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() } }} rows={2} />
        <div className="composer-tools"><div className="composer-left"><div className="popover-anchor"><button type="button" className="icon-button" aria-label="添加资料" onClick={() => setMenu(menu === 'attach' ? null : 'attach')}><Glyph name="plus" /></button>{menu === 'attach' && <div className="composer-popover"><button type="button" onClick={() => { setAttached(true); setMenu(null) }}><Glyph name="file" size={15} />附加示例资料</button><button type="button" onClick={() => { setModal('folder'); setMenu(null) }}><Glyph name="folder" size={15} />选择工作文件夹</button></div>}</div><button className="folder-pill" type="button" onClick={() => setModal('folder')}><Glyph name="folder" size={14} />{folder}</button></div><div className="composer-right"><div className="popover-anchor"><button className="model-button" type="button" onClick={() => setMenu(menu === 'model' ? null : 'model')}>{model}<Glyph name="down" size={13} /></button>{menu === 'model' && <div className="composer-popover model-popover"><span className="menu-caption">选择模型 · 仅演示</span>{['DeepSeek V3.2', 'Claude Sonnet', 'Gemini Pro'].map(item => <button type="button" key={item} onClick={() => { setModel(item); setMenu(null) }}>{item}{model === item && ' ✓'}</button>)}</div>}</div><button className="send-button" type="submit" aria-label={composeMode === '稍后处理' && phase === 'running' ? '加入待办' : '发送消息'} disabled={!draft.trim()}><Glyph name="arrow" size={19} /></button></div></div>
      </form><div className="composer-caption"><button onClick={() => setMenu(menu === 'approval' ? null : 'approval')}><Glyph name="shield" size={12} />{approval}<Glyph name="down" size={11} /></button><span>Enter 发送 · Shift + Enter 换行</span>{menu === 'approval' && <div className="approval-menu"><span className="menu-caption">批准方式 · 仅演示</span>{['操作前询问', '仅查看文件', '本文件夹内自动执行'].map(item => <button key={item} onClick={() => { setApproval(item); setMenu(null) }}>{item}{item === approval ? ' ✓' : ''}</button>)}</div>}</div>
    </div>
  </section>

  return <><div className={navHidden ? 'prototype-host nav-hidden' : 'prototype-host'}><LayoutVariants variant={variant} nav={nav} header={header} conversation={conversation} workspace={activeFile ? <PreviewWorkspace fileId={activeFile} onClose={() => setActiveFile(null)} onSelect={openFile} /> : null} outline={outline} workspaceOpen={!!activeFile} navOpen={navOpen} onNavClose={() => setNavOpen(false)} /></div>
    {import.meta.env.DEV && <div className="prototype-switcher" aria-label="原型布局与状态"><button aria-label="上一个布局" onClick={() => changeVariant(variants[(variants.indexOf(variant) + 2) % 3])}>←</button><span className="variant-label">{variant} <strong>{variantNames[variant]}</strong></span><button aria-label="下一个布局" onClick={() => changeVariant(variants[(variants.indexOf(variant) + 1) % 3])}>→</button><span className="switcher-divider" /><select aria-label="演示任务状态" value={phase} onChange={event => { setDenied(false); setPhase(event.target.value as Phase) }}>{Object.entries(phaseNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><span className="switcher-help">← → 切换</span></div>}
    {notice && <div className="toast" role="status">{notice}</div>}
    {modal && <div className={`modal-backdrop ${modal === 'image' ? 'image-backdrop' : ''}`} onMouseDown={event => { if (event.target === event.currentTarget) setModal(null) }}><div ref={modalRef} className={modal === 'image' ? 'image-dialog' : 'app-dialog'} role="dialog" aria-modal="true" aria-label={modal === 'image' ? '图片预览' : modal === 'search' ? '搜索对话' : modal === 'settings' ? '连接与偏好设置' : '选择工作文件夹'}>
      <div className="dialog-header"><strong>{modal === 'image' ? '秋季视觉方向.svg' : modal === 'search' ? '搜索对话' : modal === 'settings' ? '连接与偏好设置' : '选择工作文件夹'}</strong><button className="icon-button" aria-label="关闭浮层" onClick={() => setModal(null)}><Glyph name="close" /></button></div>
      {modal === 'image' ? <><div className="image-canvas"><div className="zoomed-image" style={{ width: `${zoom}%` }}><SampleImage /></div></div><div className="image-toolbar"><button aria-label="缩小图片" onClick={() => setZoom(Math.max(50, zoom - 25))} disabled={zoom <= 50}>−</button><button onClick={() => setZoom(100)}>{zoom}%</button><button aria-label="放大图片" onClick={() => setZoom(Math.min(250, zoom + 25))} disabled={zoom >= 250}>＋</button><span>Esc 关闭并回到对话</span></div></> : modal === 'search' ? <><label className="search-field"><Glyph name="search" /><input autoFocus aria-label="搜索会话内容" placeholder="搜索标题或关键词…" value={query} onChange={event => setQuery(event.target.value)} /></label>{[{ scenario: 'documents', label: '准备一份秋季品牌提案', detail: '提案 · 预算 · 演示稿' }, { scenario: 'code', label: '让搜索结果更准确', detail: '标题匹配 · 空结果提示' }].filter(item => (item.label + item.detail).includes(query)).map(item => <button className="search-result" key={item.scenario} onClick={() => chooseScenario(item.scenario as Scenario)}><Glyph name="chat" /><span>{item.label}<small>{item.detail}</small></span><span>↗</span></button>)}{query && !('准备一份秋季品牌提案提案预算演示稿让搜索结果更准确标题匹配空结果提示'.includes(query)) && <p className="empty-search">没有找到相关对话，试试更短的关键词。</p>}</> : modal === 'settings' ? <div className="settings-content"><p className="dialog-note">原型设置仅保存在本次页面内。</p><label>当前模型<select value={model} onChange={event => setModel(event.target.value)}>{['DeepSeek V3.2', 'Claude Sonnet', 'Gemini Pro'].map(item => <option key={item}>{item}</option>)}</select></label><label>批准方式<select value={approval} onChange={event => setApproval(event.target.value)}>{['操作前询问', '仅查看文件', '本文件夹内自动执行'].map(item => <option key={item}>{item}</option>)}</select></label><div className="settings-row"><span>外观</span><span>浅色 · 跟随本次设计</span></div><div className="settings-row"><span>文件预览</span><span>右侧工作区</span></div><button className="primary-button" onClick={() => setModal(null)}>完成</button></div> : <div className="folder-picker"><p className="dialog-note">选择示例文件夹，供本次对话使用。</p>{['品牌提案', 'Myrk'].map(item => <button key={item} onClick={() => { setFolder(item); setModal(null) }}><Glyph name="folder" /><span>{item}<small>E:\项目\{item}</small></span>{item === folder && <Glyph name="check" />}</button>)}</div>}
    </div></div>}
  </>
}
