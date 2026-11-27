import { useState } from 'react'
import { usePrototype } from './PrototypeContext'
import { ActivityList, ChatThread, Composer, ConversationList, Icon, StatusChip } from './PrototypeShared'
import './VariantA.css'

export function VariantA() {
  const p = usePrototype()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [activityOpen, setActivityOpen] = useState(true)
  return <div className="va-shell" onKeyDown={event => { if (event.key === 'Escape') setSidebarOpen(false) }}>
    {sidebarOpen && <button className="va-scrim" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)}/>}
    <aside className={`va-sidebar${sidebarOpen ? ' is-open' : ''}`} aria-label="会话导航"><div className="va-brand"><span className="va-brand-mark">M</span><strong>Myrk</strong><span>桌面助理</span><button className="p-icon-button va-sidebar-close" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)}><Icon name="close"/></button></div><button className="va-new" onClick={() => {p.newChat();setSidebarOpen(false)}}><Icon name="plus" size={18}/><span>新建会话</span><kbd>＋</kbd></button><ConversationList/><div className="va-sidebar-space"/><div className="va-project"><span className="p-eyebrow">当前任务文件夹</span><button onClick={p.toggleWorkspace}><Icon name="folder"/><span><strong>{p.workspace ? 'Myrk' : '尚未选择'}</strong><small>{p.workspace ?? '发送前可以选择'}</small></span><Icon name="chevronDown" size={13}/></button></div><footer className="va-sidebar-footer"><button onClick={p.openSettings}><Icon name="settings" size={18}/>设置</button><span><i/>本机</span></footer></aside>
    <main className="va-main"><header className="va-header"><button className="p-icon-button va-sidebar-toggle" aria-label="打开会话列表" onClick={() => setSidebarOpen(true)}><Icon name="menu"/></button><div className="va-breadcrumb"><span>会话</span><Icon name="chevronRight" size={13}/><strong>{p.scenario === 'welcome' ? '新会话' : p.title}</strong></div><div className="p-spacer"/><StatusChip/><button className="p-icon-button" aria-label="查看会话信息" onClick={() => p.showNotice('本机会话 · 任务文件夹 Myrk · 完整记录保留。')}><Icon name="more"/></button></header>
      <section className="va-chat" aria-label="对话内容"><ChatThread/></section>
      <div className="va-composer-region">{p.scenario !== 'welcome' && <section className="va-current-work" aria-label="当前工作"><button className="va-work-heading" aria-expanded={activityOpen} onClick={() => setActivityOpen(value => !value)}><span className="va-work-heading-icon"><Icon name="activity" size={16}/></span><strong>当前工作</strong><span>{p.scenario === 'approval' ? '1 项等你批准' : p.scenario === 'delivered' ? '3 个步骤已完成' : p.scenario === 'failed' ? '保留进展，可继续' : '2 / 3 步骤完成'}</span><Icon name="chevronDown" size={15}/></button>{activityOpen && <div className="va-work-body"><ActivityList compact/></div>}</section>}<Composer/></div>
    </main>
  </div>
}
