import { useState, type ReactNode } from 'react'
import { conversations, scenarioLabels, usePrototype } from './PrototypeContext'

const iconPaths: Record<string, ReactNode> = {
  plus: <path d="M12 5v14M5 12h14"/>, search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></>,
  settings: <><path d="m9 4 1-2h4l1 2 3 2 2 1v4l-2 1-1 3 1 2-3 3-2-1-3 1-2 1-3-3 1-2-1-3-2-1V8l2-1 3-2z"/><circle cx="12" cy="11" r="3"/></>,
  folder: <path d="M3 7V5h6l2 2h10v12H3z"/>, file: <><path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h5"/></>,
  activity: <path d="M3 12h4l3-7 4 14 3-7h4"/>, message: <path d="M4 4h16v12H9l-5 4z"/>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>, panel: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></>,
  chevronLeft: <path d="m14 6-6 6 6 6"/>, chevronRight: <path d="m10 6 6 6-6 6"/>, chevronDown: <path d="m6 9 6 6 6-6"/>,
  check: <path d="m5 12 4 4L19 6"/>, close: <path d="m6 6 12 12M18 6 6 18"/>,
  arrowUp: <path d="M12 19V5m-5 5 5-5 5 5"/>, stop: <rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor" stroke="none"/>,
  more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
  clock: <><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></>,
  shield: <><path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6z"/><path d="m8 12 3 3 5-6"/></>,
  alert: <><path d="m12 3 10 18H2zM12 9v5"/><path d="M12 17v1"/></>,
  copy: <><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H4v13h4"/></>,
  attachment: <path d="m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9"/>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 4M5 14a8 8 0 0 0 13 4"/></>,
  spark: <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>,
  inbox: <><path d="m6 3-4 12v6h20v-6L18 3zM2 15h6l2 3h4l2-3h6"/></>,
}
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg className="p-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{iconPaths[name] ?? iconPaths.more}</svg>
}
export function StatusChip() {
  const { scenario } = usePrototype()
  return <span className={`p-status p-status-${scenario}`}><span className="p-status-dot"/>{scenarioLabels[scenario]}</span>
}
export function ConversationList({ compact = false }: { compact?: boolean }) {
  const p = usePrototype()
  return <div className={`p-conversation-list${compact ? ' p-list-compact' : ''}`}>
    <label className="p-search"><Icon name="search" size={16}/><input aria-label="搜索会话" value={p.conversationQuery} onChange={event => p.setConversationQuery(event.target.value)} placeholder="搜索会话"/><kbd>⌕</kbd></label>
    <div className="p-list-label">最近会话</div>
    {conversations.filter(item => item.title.includes(p.conversationQuery)).map(item => <button className={`p-conversation-item${p.activeConversation === item.id ? ' is-active' : ''}`} key={item.id} aria-current={p.activeConversation === item.id ? 'page' : undefined} onClick={() => p.selectConversation(item.id)}><Icon name="message" size={17}/><span><strong>{item.title}</strong><small>{item.hint}</small></span><span className="p-conversation-dot"/></button>)}
    {p.conversationQuery && !conversations.some(item => item.title.includes(p.conversationQuery)) && <p className="p-muted p-search-empty">没有找到会话</p>}
  </div>
}
export function ChatThread() {
  const p = usePrototype()
  const [copied, setCopied] = useState(false)
  if (p.scenario === 'welcome') return <div className="p-welcome"><div className="p-welcome-mark">M</div><p className="p-eyebrow">你的桌面助理</p><h1>今天想完成什么？</h1><p>从一个问题，或一份需要整理的资料开始。</p><div className="p-suggestions">{['阅读资料，整理重点', '检查项目，找出问题', '把想法变成执行步骤'].map((label, index) => <button key={label} onClick={() => p.setDraft(label)}><Icon name={['file','folder','activity'][index]}/><span>{label}</span><Icon name="chevronRight" size={15}/></button>)}</div></div>
  return <div className="p-thread">
    <div className="p-thread-day">今天 · 14:32</div>
    <div className="p-user-message"><p>{p.submittedPrompt}</p><span><Icon name="folder" size={13}/>Myrk · 任务文件夹</span></div>
    <article className="p-assistant-message">
      <header><span className="p-assistant-mark">M</span><strong>Myrk</strong><span>桌面助理</span></header>
      <div className="p-answer-content">
        <p>我已经读过项目说明和最近的变更记录，正在把内容整理成便于分享的发布说明。</p>
        <h2>{p.scenario === 'delivered' ? '发布资料已整理完成' : '目前已经整理好的内容'}</h2>
        <p>这次变化主要集中在三处：</p>
        <ol className="p-answer-list"><li><strong>模型选择更方便。</strong>在对话中切换型号和推理强度，当前选择可以直接看到。</li><li><strong>较早记录可以整理后继续使用。</strong>摘要与原始聊天分别保留，需要时仍能回查。</li><li><strong>工作过程更容易跟进。</strong>主助理与分工各自记录进度，等待你决定的动作会明确提示。</li></ol>
        <div className="p-file-result"><span className="p-file-icon"><Icon name="file" size={21}/></span><span><strong>release-notes.md</strong><small>{p.scenario === 'delivered' ? '发布说明 · 已完成 · 2.4 KB' : '发布说明 · 草稿已整理'}</small></span><button className="p-icon-button" aria-label="查看发布说明示例" onClick={() => p.showNotice('发布说明示例：已整理模型选择、记录整理与工作进度三项变化。')}><Icon name="chevronRight"/></button></div>
        {p.scenario === 'approval' && <p className="p-answer-note">文中的链接还需要检查。有一项检查动作在等待你批准。</p>}
        {p.scenario === 'running' && <p className="p-working-line"><span/>正在检查文中的链接…</p>}
        {p.scenario === 'failed' && <div className="p-inline-error"><Icon name="alert"/><p>检查暂时中断，已完成的整理仍然保留。你可以继续原工作。</p><button onClick={p.retry}>继续原工作</button></div>}
        {p.scenario === 'delivered' && <p>已核对 6 处链接。发布说明保存在任务文件夹内，你可以检查并接受成果。</p>}
      </div>
      <footer className="p-answer-footer"><button onClick={() => { setCopied(true); p.showNotice('回答复制操作已演示，未读写系统剪贴板。') }}><Icon name={copied ? 'check' : 'copy'} size={15}/>{copied ? '已演示复制' : '复制回答'}</button><span>DeepSeek Chat</span></footer>
    </article>
    <details className="p-record"><summary><Icon name="activity" size={15}/>查看本轮记录<span>3 个步骤 · 4 次请求</span><Icon name="chevronDown" size={14}/></summary><div><p>实际请求：4 次 · 办事轮：3 轮 · 运行时间：18 秒</p><p>模型输入：8,420 tokens · 输出：1,260 tokens · 缓存读取：6,400 tokens</p><p>以上均为统一排版示例，不代表本轮真实模型调用。</p><details><summary>查看请求参数与资料范围</summary><pre>{'{\n  "tool": "check",\n  "cwd": "E:\\\\工作资料\\\\Myrk",\n  "argv": ["python", "scripts/check_links.py"]\n}'}</pre></details><p>上下文摘要：尚未生成 · 原始聊天记录可回查</p></div></details>
  </div>
}
export function ActivityList({ compact = false }: { compact?: boolean }) {
  const p = usePrototype()
  if (p.scenario === 'welcome') return <div className="p-activity-empty"><Icon name="activity"/><p>发送后在这里查看进展</p><span>当前步骤和需要你决定的事项会集中显示。</span></div>
  return <div className={`p-activity${compact ? ' p-activity-compact' : ''}`}>
    <div className="p-step-row"><span className="p-step-icon is-done"><Icon name="check" size={13}/></span><span>阅读项目说明</span><small>已完成</small></div>
    <div className="p-step-row"><span className="p-step-icon is-done"><Icon name="check" size={13}/></span><span>整理发布说明</span><small>已完成</small></div>
    <div className="p-step-row"><span className={`p-step-icon ${p.scenario === 'delivered' ? 'is-done' : 'is-current'}`}><Icon name={p.scenario === 'delivered' ? 'check' : p.scenario === 'approval' ? 'shield' : 'clock'} size={13}/></span><span>检查文中链接</span><small>{p.scenario === 'approval' ? '等待批准' : p.scenario === 'failed' ? '已中断' : p.scenario === 'delivered' ? '已完成' : '进行中'}</small></div>
    {p.scenario === 'approval' && <section className="p-decision" aria-label="待批准动作"><header><Icon name="shield"/><strong>一项检查需要你批准</strong></header><p>运行项目的链接检查脚本，核对发布说明中的网址。</p><div className="p-command"><code>python scripts/check_links.py</code></div><span className="p-decision-scope">工作位置：E:\工作资料\Myrk</span><details><summary>查看范围与参数</summary><p>允许脚本在任务文件夹中运行，并联网检查文中链接。本次批准只适用于该动作。此处为演示，不执行脚本。</p></details><div className="p-decision-actions"><button className="p-button" onClick={p.reject}>拒绝</button><button className="p-button p-button-primary" onClick={p.approve}><Icon name="check" size={15}/>批准这次动作</button></div></section>}
    {p.scenario === 'delivered' && <div className="p-acceptance"><Icon name="check"/><span>{p.accepted ? '成果已接受' : '成果已交付，等你检查'}</span><button className={`p-button ${p.accepted ? '' : 'p-button-primary'}`} disabled={p.accepted} onClick={p.accept}>{p.accepted ? '已接受' : '接受成果'}</button></div>}
    {p.scenario === 'failed' && <div className="p-recovery"><p><Icon name="alert" size={16}/>检查已中断，整理结果仍保留</p><button className="p-button p-button-primary" onClick={p.retry}><Icon name="refresh" size={15}/>继续原工作</button></div>}
    <details className="p-delegation"><summary><Icon name="activity" size={14}/><span>资料整理助理</span><small>已完成</small><Icon name="chevronDown" size={14}/></summary><p>已提取 3 项变化并交给主助理核对。独立办理 2 轮；记录为示例。</p></details>
  </div>
}
export function Composer({ compact = false }: { compact?: boolean }) {
  const p = usePrototype()
  return <div className={`p-composer-wrap${compact ? ' p-composer-compact' : ''}`}>
    {(p.attachment || p.workspace) && <div className="p-context-row">{p.workspace && <button onClick={p.toggleWorkspace} aria-label="移除示例任务文件夹"><Icon name="folder" size={14}/><span>Myrk</span><span className="p-muted">任务文件夹</span><Icon name="close" size={12}/></button>}{p.attachment && <button onClick={p.toggleAttachment}><Icon name="file" size={14}/>{p.attachment}<Icon name="close" size={12}/></button>}</div>}
    {p.queueCount > 0 && <p className="p-queue"><Icon name="clock" size={13}/>{p.queueCount} 项新工作已排队</p>}
    <form className="p-composer" onSubmit={event => { event.preventDefault(); p.running ? p.supplement() : p.send() }}>
      <textarea aria-label="输入消息" placeholder={p.running ? '补充要求，或安排下一项工作…' : '输入消息，开始一项工作…'} value={p.draft} onChange={event => p.setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); p.running ? p.supplement() : p.send() } }}/>
      <div className="p-composer-toolbar"><div className="p-composer-tools"><button type="button" className="p-icon-button" aria-label="添加示例资料" title="添加资料" onClick={p.toggleAttachment}><Icon name="plus"/></button><button type="button" className="p-icon-button" aria-label="切换示例任务文件夹" title="任务文件夹" onClick={p.toggleWorkspace}><Icon name="folder"/></button><span className="p-toolbar-divider"/><label className="p-model-select"><select aria-label="选择模型" value={p.model} disabled={p.running} onChange={event => p.setModel(event.target.value)}><option>DeepSeek Chat</option><option>DeepSeek Reasoner</option><option>Gemini 2.5 Flash</option></select><Icon name="chevronDown" size={12}/></label><label className="p-effort-select"><select aria-label="推理强度" value={p.reasoning} disabled={p.running} onChange={event => p.setReasoning(event.target.value)}><option>标准</option><option>深入</option></select><Icon name="chevronDown" size={12}/></label></div><div className="p-composer-submit">{p.running && <><button type="submit" className="p-text-button" disabled={!p.draft.trim()}>发送补充</button><button type="button" className="p-text-button" disabled={!p.draft.trim()} onClick={p.enqueue}>排队</button></>}<button type={p.running ? 'button' : 'submit'} className="p-send" aria-label={p.running ? '停止回答' : '发送消息'} disabled={!p.running && !p.draft.trim()} onClick={p.running ? p.stop : undefined}><Icon name={p.running ? 'stop' : 'arrowUp'} size={19}/></button></div></div>
    </form>
    <div className="p-composer-caption"><label><Icon name="shield" size={12}/><select aria-label="下一轮批准方式" value={p.approvalMode} onChange={event => p.setApprovalMode(event.target.value)}><option>请求批准</option><option>帮我批准</option><option>完全访问权限</option></select><Icon name="chevronDown" size={11}/></label><span>Shift + Enter 换行</span></div>
  </div>
}
