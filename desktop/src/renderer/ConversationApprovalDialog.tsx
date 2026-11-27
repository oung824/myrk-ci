import { useEffect, useRef, useState } from 'react'
import type { ApprovalMode } from '../shared/protocol'

export function ConversationApprovalDialog({ mode, busy, onClose, onSave }: {
  mode: ApprovalMode
  busy: boolean
  onClose(): void
  onSave(mode: ApprovalMode): void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [selected, setSelected] = useState(mode)
  useEffect(() => {
    const opener = document.activeElement
    const window = dialog.current!
    window.showModal()
    return () => {
      window.close()
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])

  return <dialog ref={dialog} className="conversation-approval-dialog" aria-label="会话批准方式" onCancel={event => { event.preventDefault(); onClose() }} onClose={onClose}>
    <button type="button" className="approval-close" aria-label="关闭会话批准方式" onClick={onClose}>×</button>
    <h2>会话批准方式</h2>
    <p>保存后用于这个聊天之后的新执行轮（从开始到结束的一次办事过程），不会改变正在办理的一轮、已准备动作或现有分工。保存不需要回答服务的连接密码。</p>
    <label htmlFor="conversation-approval-mode">批准方式（需要扩大操作范围时如何决定）</label>
    <select id="conversation-approval-mode" value={selected} disabled={busy} onChange={event => setSelected(event.target.value as ApprovalMode)}>
      <option value="request_approval">请求批准</option><option value="auto_review">帮我批准</option><option value="full_access">完全访问权限</option>
    </select>
    <p>请求批准：需要扩大范围时由你决定。帮我批准：保持同样的电脑系统限制，由另一位助理审核。完全访问权限：移除一般本地文件与联网限制，免普通逐项批准，仍受电脑账号和其他有效要求限制。</p>
    <footer><button type="button" aria-label="取消会话批准方式" onClick={onClose}>取消</button><button type="button" aria-label="保存会话批准方式" disabled={busy} onClick={() => onSave(selected)}>{busy ? '正在保存…' : '保存'}</button></footer>
    {busy && <p>已提交的保存仍会完成；关闭窗口不会撤回。</p>}
  </dialog>
}
