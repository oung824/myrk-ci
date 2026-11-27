import { useEffect, useState } from 'react';
import { usePrototype } from './PrototypeContext';
import { ActivityList, ChatThread, Composer, ConversationList, Icon, StatusChip } from './PrototypeShared';
import './VariantB.css';

export function VariantB() {
  const p = usePrototype();
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 1179px)').matches);
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 759px)').matches);
  const [progressOpen, setProgressOpen] = useState(true);
  const [progressDrawerOpen, setProgressDrawerOpen] = useState(false);
  const [navigationOpen, setNavigationOpen] = useState(false);

  useEffect(() => {
    const progressMedia = window.matchMedia('(max-width: 1179px)');
    const navigationMedia = window.matchMedia('(max-width: 759px)');
    const updateProgress = () => {
      setNarrow(progressMedia.matches);
      setProgressDrawerOpen(false);
    };
    const updateNavigation = () => {
      setMobile(navigationMedia.matches);
      setNavigationOpen(false);
    };
    progressMedia.addEventListener('change', updateProgress);
    navigationMedia.addEventListener('change', updateNavigation);
    return () => {
      progressMedia.removeEventListener('change', updateProgress);
      navigationMedia.removeEventListener('change', updateNavigation);
    };
  }, []);

  const progressVisible = narrow ? progressDrawerOpen : progressOpen;
  const navigationVisible = !mobile || navigationOpen;
  const progressLabel = {
    welcome: '从一个目标开始',
    running: '工作正在推进',
    approval: '有一项决定待处理',
    delivered: '成果等待你验收',
    failed: '工作需要你的处理',
  }[p.scenario];

  function toggleProgress() {
    if (narrow) {
      setNavigationOpen(false);
      setProgressDrawerOpen(!progressDrawerOpen);
    } else {
      setProgressOpen(!progressOpen);
    }
  }

  return (
    <div
      className="vb"
      data-progress-open={progressVisible}
      data-scenario={p.scenario}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          setNavigationOpen(false);
          setProgressDrawerOpen(false);
        }
      }}
    >
      <nav className="vb-rail" aria-label="主要功能">
        <button className="vb-brand" onClick={p.newChat} title="Myrk · 新建会话" aria-label="Myrk，新建会话">m<span>·</span></button>
        <button className="vb-rail-button vb-new-button" onClick={p.newChat} title="新建会话" aria-label="新建会话"><Icon name="plus" size={21} /></button>
        <div className="vb-rail-divider" />
        <button
          className="vb-rail-button vb-rail-selected"
          title="会话"
          aria-label={mobile ? (navigationOpen ? '收起会话列表' : '打开会话列表') : '会话列表'}
          aria-expanded={navigationVisible}
          aria-controls="vb-conversations"
          onClick={() => {
            if (mobile) {
              setProgressDrawerOpen(false);
              setNavigationOpen(!navigationOpen);
            } else {
              document.querySelector<HTMLInputElement>('.vb-conversations .p-search input')?.focus();
            }
          }}
        ><Icon name="message" size={20} /></button>
        <button className="vb-rail-button" onClick={p.toggleWorkspace} title="任务文件夹" aria-label="选择任务文件夹"><Icon name="folder" size={20} /></button>
        <button className={`vb-rail-button${p.scenario === 'approval' ? ' vb-needs-decision' : ''}`} onClick={toggleProgress} title="当前进展" aria-label="打开或收起当前进展" aria-expanded={progressVisible} aria-controls="vb-progress"><Icon name="activity" size={20} />{p.scenario === 'approval' && <span className="vb-indicator" />}</button>
        <div className="vb-rail-spacer" />
        <button className="vb-rail-button" onClick={p.openSettings} title="设置" aria-label="打开设置"><Icon name="settings" size={20} /></button>
        <div className="vb-profile" title="本地工作空间">你</div>
      </nav>

      {mobile && navigationOpen && <button className="vb-backdrop" onClick={() => setNavigationOpen(false)} aria-label="关闭会话列表" />}
      <aside id="vb-conversations" className="vb-conversations" hidden={!navigationVisible} aria-label="会话列表">
        <div className="vb-conversations-heading">
          <div><span className="vb-small-label">MYRK</span><h2>会话</h2></div>
          <button className="p-icon-button vb-sidebar-new" onClick={mobile ? () => setNavigationOpen(false) : p.newChat} title={mobile ? '关闭会话列表' : '新建会话'} aria-label={mobile ? '关闭会话列表' : '新建会话'}><Icon name={mobile ? 'close' : 'plus'} size={18} /></button>
        </div>
        <div className="vb-conversation-scroll"><ConversationList compact /></div>
        <button className="vb-workspace" onClick={p.toggleWorkspace} title={p.workspace ?? '选择本次任务的工作位置'}>
          <span className="vb-workspace-icon"><Icon name="folder" size={17} /></span>
          <span><span className="vb-workspace-caption">任务文件夹</span><strong>{p.workspace ? p.workspace.split(/[\\/]/).filter(Boolean).pop() : '选择文件夹'}</strong></span>
          <Icon name="chevronRight" size={14} />
        </button>
      </aside>

      <main className="vb-main">
        <header className="vb-main-header">
          <div className="vb-heading"><span className="vb-small-label">{p.scenario === 'welcome' ? '准备开始' : '当前会话'}</span><h1>{p.title}</h1></div>
          <div className="vb-header-actions"><StatusChip /><span className="vb-header-separator" /><button className={`vb-progress-toggle${p.scenario === 'approval' ? ' vb-progress-attention' : ''}`} onClick={toggleProgress} aria-expanded={progressVisible} aria-controls="vb-progress" title={progressVisible ? '收起当前进展' : '展开当前进展'}><Icon name="activity" size={16} /><span>{p.scenario === 'approval' ? '待你决定' : '当前进展'}</span><Icon name={progressVisible ? 'chevronRight' : 'chevronLeft'} size={14} /></button></div>
        </header>
        <div className="vb-chat-scroll"><div className="vb-chat-content"><ChatThread /></div></div>
        <div className="vb-composer-area"><Composer /><div className="vb-composer-note"><span><span className="vb-note-dot" />可以继续提问或补充要求</span>{p.queueCount > 0 && <span>{p.queueCount} 项新工作已排队</span>}</div></div>
      </main>

      {narrow && progressDrawerOpen && <button className="vb-backdrop" onClick={() => setProgressDrawerOpen(false)} aria-label="关闭当前进展" />}
      <aside id="vb-progress" className="vb-progress" hidden={!progressVisible} aria-label="当前进展">
        <header className="vb-progress-header"><div><span className="vb-small-label">工作面板</span><h2>当前进展</h2></div><button className="p-icon-button" onClick={toggleProgress} title="收起当前进展" aria-label="收起当前进展"><Icon name={narrow ? 'close' : 'chevronRight'} size={18} /></button></header>
        <div className={`vb-progress-summary${p.scenario === 'approval' ? ' vb-summary-attention' : ''}`}><span className="vb-progress-marker" /><span>{progressLabel}</span></div>
        <div className="vb-progress-scroll"><ActivityList /></div>
        <footer className="vb-progress-footer"><Icon name="activity" size={14} /><p>进展在这里持续更新。<br /><span>需要你决定时，会停下来等你。</span></p></footer>
      </aside>
    </div>
  );
}
