import { useEffect, useRef, useState } from 'react'
import './PrototypeSettings.css'

type SettingsPage = 'provider' | 'work' | 'memory' | 'records'
type Provider = 'deepseek' | 'gemini' | 'custom'
type ApprovalMode = 'request_approval' | 'auto_review' | 'full_access'
type MemoryScope = 'personal' | 'project'

const providers = {
  deepseek: { label: 'DeepSeek', detail: '专用连接', address: 'https://api.deepseek.com', models: ['deepseek-flash', 'deepseek-reasoner'] },
  gemini: { label: 'Gemini', detail: 'Google 官方服务', address: 'https://generativelanguage.googleapis.com', models: ['gemini-2.5-flash', 'gemini-2.5-pro'] },
  custom: { label: '自定义服务', detail: '兼容连接', address: 'https://service.example/v1', models: ['my-model'] },
}

const pages: { id: SettingsPage; label: string; detail: string; icon: string }[] = [
  { id: 'provider', label: '回答服务', detail: '连接与型号', icon: '◈' },
  { id: 'work', label: '工作偏好', detail: '批准方式与助理轮数', icon: '◷' },
  { id: 'memory', label: '长期记忆', detail: '以后可以复用的笔记', icon: '◇' },
  { id: 'records', label: '记录与导入', detail: '查看与保留会话', icon: '↥' },
]

const approvalOptions: { id: ApprovalMode; label: string; detail: string }[] = [
  { id: 'request_approval', label: '请求批准', detail: '需要批准的动作，由你本人决定。' },
  { id: 'auto_review', label: '帮我批准', detail: '保持访问限制，由独立助理判断待批动作。' },
  { id: 'full_access', label: '完全访问权限', detail: '移除本地文件与联网隔离，免普通逐项批准。' },
]

export function PrototypeSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  const [page, setPage] = useState<SettingsPage>('provider')
  const [provider, setProvider] = useState<Provider>('deepseek')
  const [address, setAddress] = useState(providers.deepseek.address)
  const [model, setModel] = useState('deepseek-flash')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [approval, setApproval] = useState<ApprovalMode>('request_approval')
  const [childRounds, setChildRounds] = useState('250')
  const [confirmMemory, setConfirmMemory] = useState(true)
  const [memoryScope, setMemoryScope] = useState<MemoryScope>('personal')
  const [newMemory, setNewMemory] = useState('')
  const [memory, setMemory] = useState<{ id: number; text: string; scope: MemoryScope }[]>([
    { id: 1, text: '回复先说明结论，再补充必要细节。', scope: 'personal' },
    { id: 2, text: 'Myrk 的界面说明使用中文。', scope: 'project' },
  ])
  const [editingMemory, setEditingMemory] = useState<number | null>(null)
  const [memoryDraft, setMemoryDraft] = useState('')
  const [imported, setImported] = useState(false)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!open) return
    const opener = document.activeElement
    panel.current?.querySelector<HTMLButtonElement>('[aria-label="关闭设置"]')?.focus()
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        close.current()
      }
      if (event.key !== 'Tab') return
      const controls = [...panel.current!.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')]
        .filter(control => control.getClientRects().length > 0)
      const first = controls[0]
      const last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last?.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first?.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('keydown', handleKey)
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [open])

  if (!open) return null

  return <div className="p-settings-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <div ref={panel} className="p-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="p-settings-title" aria-describedby="p-settings-demo">
      <header className="p-settings-header">
        <div><span className="p-settings-eyebrow">MYRK · PREFERENCES</span><h2 id="p-settings-title">设置</h2></div>
        <button type="button" className="p-settings-close" aria-label="关闭设置" onClick={onClose}>×</button>
      </header>
      <div className="p-settings-layout">
        <nav className="p-settings-navigation" aria-label="设置分类">
          {pages.map(item => <button key={item.id} type="button" className="p-settings-nav-item" aria-current={page === item.id ? 'page' : undefined} onClick={() => { setPage(item.id); setNotice('') }}>
            <span className="p-settings-nav-icon" aria-hidden="true">{item.icon}</span><span>{item.label}<small>{item.detail}</small></span>
          </button>)}
          <p id="p-settings-demo" className="p-settings-demo">界面演示<br />本页选择仅保留到刷新前。</p>
        </nav>
        <form className="p-settings-form" onSubmit={event => { event.preventDefault(); setNotice('已保存本次演示选择，刷新页面后恢复。') }}>
          <div className="p-settings-content">
            {page === 'provider' && <section className="p-settings-page" aria-label="回答服务设置">
              <div className="p-settings-page-title"><h3>让哪项服务帮助你？</h3><p>选择用于理解问题、生成回答的服务与型号。</p></div>
              <div className="p-settings-providers">
                {(Object.entries(providers) as [Provider, typeof providers[Provider]][]).map(([id, item]) => <button key={id} type="button" className="p-settings-provider" aria-pressed={provider === id} onClick={() => { setProvider(id); setAddress(item.address); setModel(item.models[0]); setPassword(''); setNotice('') }}>
                  <span className="p-settings-provider-symbol" aria-hidden="true">{id === 'deepseek' ? 'D' : id === 'gemini' ? 'G' : '＋'}</span><strong>{item.label}</strong><small>{item.detail}</small><span className="p-settings-check" aria-hidden="true">{provider === id ? '✓' : ''}</span>
                </button>)}
              </div>
              <label className="p-settings-field" htmlFor="p-settings-address">服务地址<input id="p-settings-address" type="url" value={address} onChange={event => setAddress(event.target.value)} spellCheck={false} /></label>
              <label className="p-settings-field" htmlFor="p-settings-password">演示连接密码<span className="p-settings-password-wrap"><input id="p-settings-password" type={showPassword ? 'text' : 'password'} value={password} onChange={event => setPassword(event.target.value)} placeholder="输入任意演示文字" autoComplete="off" /><button type="button" className="p-settings-password-toggle" aria-label={showPassword ? '隐藏密码' : '显示密码'} aria-pressed={showPassword} onClick={() => setShowPassword(!showPassword)}>{showPassword ? '隐藏' : '显示'}</button></span><small>这里的输入只用于预览表单，不会连接服务。</small></label>
              <div className="p-settings-row">
                <label className="p-settings-field" htmlFor="p-settings-model">回答型号<select id="p-settings-model" value={model} onChange={event => setModel(event.target.value)}>{providers[provider].models.map(item => <option key={item}>{item}</option>)}</select></label>
                <label className="p-settings-field" htmlFor="p-settings-review-model">审核型号<select id="p-settings-review-model" defaultValue="same"><option value="same">跟随回答型号</option>{providers[provider].models.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
              </div>
              <div className="p-settings-service-note"><span className="p-settings-dot" /><span>当前显示样例型号；工作开始前可在输入框旁调整。</span></div>
            </section>}

            {page === 'work' && <section className="p-settings-page" aria-label="工作偏好设置">
              <div className="p-settings-page-title"><h3>按你的方式办事</h3><p>设置新会话的默认批准方式，以及子助理的办事轮数。</p></div>
              <fieldset className="p-settings-fieldset"><legend>默认批准方式</legend><div className="p-settings-approval-options">{approvalOptions.map(item => <label key={item.id} className="p-settings-approval-option" data-selected={approval === item.id}><input type="radio" name="p-settings-approval" value={item.id} checked={approval === item.id} onChange={() => setApproval(item.id)} /><span><strong>{item.label}</strong><small>{item.detail}</small></span></label>)}</div></fieldset>
              <label className="p-settings-field p-settings-rounds" htmlFor="p-settings-child-rounds">每名子助理最多办理多少轮？<span className="p-settings-number-wrap"><input id="p-settings-child-rounds" type="number" min="1" step="1" value={childRounds} onChange={event => setChildRounds(event.target.value)} /><span>轮</span></span><small>一轮是助理判断下一步的一次办事步骤。主助理默认不设轮数上限。</small></label>
              <p className="p-settings-inline-note">会话可以保留自己的批准方式。已经准备好的动作，继续使用准备时的设置。</p>
            </section>}

            {page === 'memory' && <section className="p-settings-page" aria-label="长期记忆设置">
              <div className="p-settings-page-title"><h3>留给下一次的背景</h3><p>保存以后工作可以再次取用的偏好和事实。</p></div>
              <label className="p-settings-toggle-row"><span><strong>写入前让我确认</strong><small>助理提出新增或修改记忆时，先交给你检查。</small></span><input type="checkbox" role="switch" checked={confirmMemory} onChange={event => setConfirmMemory(event.target.checked)} /></label>
              <div className="p-settings-memory-heading"><h4>已保存的示例</h4><span>{memory.length} 条</span></div>
              <div className="p-settings-memories">{memory.map(item => <div key={item.id} className="p-settings-memory"><span className="p-settings-memory-scope">{item.scope === 'personal' ? '个人' : '项目 · Myrk'}</span>{editingMemory === item.id ? <div className="p-settings-memory-edit"><input aria-label="修改记忆内容" value={memoryDraft} onChange={event => setMemoryDraft(event.target.value)} /><button type="button" className="p-settings-text-button" disabled={!memoryDraft.trim()} onClick={() => { setMemory(items => items.map(fact => fact.id === item.id ? { ...fact, text: memoryDraft.trim() } : fact)); setEditingMemory(null); setNotice('这条演示记忆已更新。') }}>完成</button></div> : <p>{item.text}</p>}<div className="p-settings-memory-actions"><button type="button" className="p-settings-text-button" aria-label={`编辑记忆：${item.text}`} onClick={() => { setEditingMemory(item.id); setMemoryDraft(item.text) }}>编辑</button><button type="button" className="p-settings-text-button" aria-label={`删除记忆：${item.text}`} onClick={() => { setMemory(items => items.filter(fact => fact.id !== item.id)); setNotice('已移除这条演示记忆。') }}>删除</button></div></div>)}</div>
              <div className="p-settings-memory-add"><label className="p-settings-field" htmlFor="p-settings-new-memory">增加一条笔记<textarea id="p-settings-new-memory" value={newMemory} onChange={event => setNewMemory(event.target.value)} placeholder="例如：我习惯先看结论。" rows={2} /></label><div className="p-settings-memory-add-actions"><select aria-label="记忆归属" value={memoryScope} onChange={event => setMemoryScope(event.target.value as MemoryScope)}><option value="personal">个人记忆</option><option value="project">项目记忆 · Myrk</option></select><button type="button" className="p-settings-secondary" disabled={!newMemory.trim()} onClick={() => { setMemory(items => [...items, { id: Date.now(), text: newMemory.trim(), scope: memoryScope }]); setNewMemory(''); setNotice('已增加一条演示记忆。') }}>增加笔记</button></div></div>
            </section>}

            {page === 'records' && <section className="p-settings-page" aria-label="记录与导入设置">
              <div className="p-settings-page-title"><h3>把工作接着做下去</h3><p>从会话记录回看问题、进展与成果。</p></div>
              <div className="p-settings-record-summary"><span className="p-settings-record-icon" aria-hidden="true">▤</span><div><h4>本机会话记录</h4><p>12 个示例会话 · 最近保存于 11:24</p></div><span className="p-settings-record-badge">样例</span></div>
              <div className="p-settings-import"><span className="p-settings-import-icon" aria-hidden="true">↥</span><h4>导入以前的会话</h4><p>导入副本后继续查看，原始记录保留。</p><button type="button" className="p-settings-secondary" onClick={() => { setImported(true); setNotice('已显示导入成功的示例。') }}>选择示例记录</button><small>此处演示导入结果，不会打开电脑上的文件。</small></div>
              {imported && <div className="p-settings-imported"><span aria-hidden="true">✓</span><div><strong>会议纪要整理.json</strong><p>示例记录已准备好，可以回到会话查看。</p></div><button type="button" className="p-settings-text-button" onClick={() => { setImported(false); setNotice('已移除导入示例。') }}>移除示例</button></div>}
              <p className="p-settings-inline-note">完整聊天记录与长期记忆分开保存。长期记忆只保留以后仍需要的简短笔记。</p>
            </section>}
          </div>
          <footer className="p-settings-footer"><p className="p-settings-notice" role="status" aria-live="polite">{notice || '所有内容均为本次界面演示。'}</p><div className="p-settings-footer-actions"><button type="button" className="p-settings-secondary" onClick={onClose}>关闭</button><button type="submit" className="p-settings-primary">保存演示设置</button></div></footer>
        </form>
      </div>
    </div>
  </div>
}
