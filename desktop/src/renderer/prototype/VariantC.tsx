import { useState } from 'react';
import { usePrototype } from './PrototypeContext';
import { ActivityList, ChatThread, Composer, ConversationList, Icon, StatusChip } from './PrototypeShared';
import './VariantC.css';

export function VariantC() {
  const { scenario, title, workspace, queueCount, running, showNotice, newChat, openSettings, toggleWorkspace } = usePrototype();
  const [conversationsOpen, setConversationsOpen] = useState(false);
  const [taskOpen, setTaskOpen] = useState(false);

  const task = {
    welcome: { label: '准备就绪', description: '说说你想完成什么，我会在这里同步进展。', pending: '还没有待处理事项', step: 0 },
    running: { label: '正在处理任务', description: '我会继续推进，并在需要你决定时停下来。', pending: '工作进行中，你可以随时补充要求', step: 1 },
    approval: { label: '方案已准备好', description: '接下来需要执行一项你尚未批准的操作。', pending: '1 项操作等待你批准', step: 1 },
    delivered: { label: '本次工作已完成', description: '成果和检查结果已整理在回答中。', pending: '成果等待你检查和接受', step: 2 },
    failed: { label: '这一步暂时中断', description: '已有进展会保留，可以从当前步骤继续。', pending: '需要处理一次连接中断', step: 1 },
  }[scenario];

  return (
    <div className="vc" onKeyDown={event => { if (event.key === 'Escape') setConversationsOpen(false) }}>
      <header className="vc-header">
        <div className="vc-header-start">
          <button className="p-icon-button vc-menu-button" aria-label="打开会话列表" aria-expanded={conversationsOpen} onClick={() => setConversationsOpen(!conversationsOpen)}>
            <Icon name="menu" size={19} />
          </button>
          <button className="vc-brand" onClick={() => showNotice('Myrk · 你的桌面助理')} aria-label="Myrk">
            <span className="vc-brand-mark">m</span><span>Myrk</span>
          </button>
          <span className="vc-header-divider" />
          <nav className="vc-tabs" aria-label="已打开的会话">
            <button className="vc-tab vc-tab-active" onClick={() => showNotice('正在查看当前会话')}>
              <Icon name="message" size={15} /><span>{title}</span><span className="vc-tab-dot" />
            </button>
            <button className="vc-add-tab" aria-label="新建会话" onClick={newChat}><Icon name="plus" size={17} /></button>
          </nav>
        </div>
        <div className="vc-header-actions">
          <button className="vc-workspace-button" onClick={toggleWorkspace}><Icon name="folder" size={16} /><span>{workspace ? 'Myrk 项目' : '选择任务文件夹'}</span><Icon name="chevronDown" size={12} /></button>
          <button className="p-icon-button" aria-label="打开设置" onClick={openSettings}><Icon name="settings" size={18} /></button>
        </div>
      </header>

      <main className="vc-document">
        <div className="vc-document-inner">
          <div className="vc-document-heading">
            <div className="vc-document-meta"><span>会话</span><span className="vc-meta-dot" /> <span>今天</span></div>
            <div className="vc-title-row"><h1>{scenario === 'welcome' ? '今天，一起完成什么？' : title}</h1><StatusChip /></div>
            <div className="vc-document-caption">{scenario === 'welcome' ? '从一个想法、一份资料，或手边的工作开始。' : '回答留在这里，工作进展在下方。'}</div>
          </div>
          <div className="vc-document-content"><ChatThread /></div>
        </div>
      </main>

      <section className={`vc-dock ${taskOpen ? 'vc-dock-open' : 'vc-dock-collapsed'}`} aria-label="当前任务与输入区">
        <div className="vc-task-panel">
          <button className="vc-panel-heading" onClick={() => setTaskOpen(!taskOpen)} aria-expanded={taskOpen}>
            <span className={`vc-task-indicator ${running ? 'vc-task-indicator-running' : ''}`}><Icon name={scenario === 'delivered' ? 'check' : 'activity'} size={16} /></span>
            <span className="vc-panel-heading-text">当前任务</span>
            <span className="vc-task-heading-status">{task.label}</span>
            <span className="vc-panel-pending"><span className={scenario === 'approval' || scenario === 'failed' ? 'vc-pending-dot vc-pending-dot-active' : 'vc-pending-dot'} />{task.pending}</span>
            <span className={`vc-collapse-icon ${taskOpen ? 'vc-collapse-icon-open' : ''}`}><Icon name="chevronDown" size={16} /></span>
          </button>

          {taskOpen && (
            <div className="vc-task-body">
              <div className="vc-progress-area">
                <div className="vc-section-label">工作进展</div>
                <div className="vc-step-track">
                  {['理解任务', '进行工作', '交付结果'].map((label, index) => (
                    <div className={`vc-step ${index < task.step ? 'vc-step-done' : ''} ${index === task.step ? 'vc-step-current' : ''}`} key={label}>
                      <span className="vc-step-node">{index < task.step ? <Icon name="check" size={12} /> : index + 1}</span>
                      <span>{label}</span>
                    </div>
                  ))}
                </div>
                <p className="vc-current-step">{task.label}</p>
                <p className="vc-current-description">{task.description}</p>
                <button className="vc-detail-button" onClick={() => showNotice('用量详情：此处可以查看当前任务的助理请求和处理时间。')}><Icon name="clock" size={13} /><span>查看本次用量</span><Icon name="chevronRight" size={12} /></button>
              </div>
              <div className="vc-actions-area">
                <div className="vc-section-label"><span>待处理事项</span>{queueCount > 0 && <span className="vc-queue-count">另有 {queueCount} 项排队工作</span>}</div>
                <div className="vc-activity-content"><ActivityList compact /></div>
              </div>
            </div>
          )}
        </div>
        <div className="vc-composer-area"><Composer compact /></div>
        <div className="vc-bottom-note"><span className="vc-bottom-note-dot" />内容和操作只属于当前会话<span className="vc-bottom-note-separator">·</span>重要结果请检查</div>
      </section>

      {conversationsOpen && (
        <div className="vc-drawer-layer">
          <button className="vc-drawer-backdrop" aria-label="关闭会话列表" onClick={() => setConversationsOpen(false)} />
          <aside className="vc-conversation-drawer" aria-label="会话列表">
            <div className="vc-drawer-heading"><span className="vc-drawer-title">你的会话</span><button className="p-icon-button" aria-label="关闭会话列表" onClick={() => setConversationsOpen(false)}><Icon name="close" size={18} /></button></div>
            <button className="vc-new-conversation" onClick={() => { newChat(); setConversationsOpen(false); }}><Icon name="plus" size={17} />新建会话</button>
            <div className="vc-drawer-list"><ConversationList /></div>
            <div className="vc-drawer-footer"><Icon name="shield" size={15} /><span>各会话的任务与授权分别保存</span></div>
          </aside>
        </div>
      )}
    </div>
  );
}
