import { useEffect, useRef, type ReactNode } from 'react'
import { Icon } from './Icon'

export function ConversationFilesPanel({ onClose, children }: { onClose: () => void; children?: ReactNode }) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => heading.current?.focus(), [])

  return <aside className="conversation-files" aria-label="右侧文件查看区" onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); onClose() }
  }}>
    <header className="files-heading">
      <Icon name="file" size={19}/><h2 ref={heading} tabIndex={-1}>会话成果</h2>
      <button type="button" aria-label="返回聊天" onClick={onClose}><Icon name="close" size={17}/></button>
    </header>
    <div className="files-content">{children ?? <div className="files-empty">
      <span className="files-empty-symbol"><Icon name="file" size={30}/></span>
      <h3>本会话尚未登记已交付文件</h3>
      <p>文件交付后，可以在这里打开并继续讨论。</p>
      <button type="button" className="button-secondary" onClick={onClose}>返回聊天</button>
    </div>}</div>
  </aside>
}
