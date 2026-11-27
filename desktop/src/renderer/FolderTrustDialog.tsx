import { useEffect, useRef } from 'react'
import type { ApprovalMode } from '../shared/protocol'

export function FolderTrustDialog({ path, mode, busy, error, onTrust, onClose }: {
  path: string
  mode: ApprovalMode
  busy: boolean
  error: string
  onTrust(): void
  onClose(): void
}) {
  const window = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = window.current!
    dialog.showModal()
    return () => dialog.close()
  }, [])

  return <dialog ref={window} className="folder-trust" aria-label="信任此文件夹吗？" aria-labelledby="folder-trust-title" onCancel={event => { event.preventDefault(); onClose() }} onClose={onClose}>
    <button type="button" className="trust-close" aria-label="关闭文件夹确认" onClick={onClose}>×</button>
    <h2 id="folder-trust-title">信任此文件夹吗？</h2>
    <p className="trust-path">{path}</p>
    <p>作为默认工作位置和项目资料来源。信任后加载受支持的项目设置，不能改动你明确选择的权限和连接密码。</p>
    <p className="trust-mode">当前选择：{({ request_approval: '请求批准（需要批准时由你决定）', auto_review: '帮我批准（由独立助理判断待批动作）', full_access: '完全访问权限（移除一般本地文件与联网限制）' })[mode]}。</p>
    <details className="trust-details"><summary>查看范围与资料使用说明</summary>
      <p>项目信任会独立保存，换回答服务或批准方式不必重新信任。项目自带的设置不能突破其他更高层要求。</p>
      <p>必要资料仍按本次授权提供给所选回答服务；更换旧工作资料的接收方仍须单独审阅，不会自动上传整个文件夹。</p>
      <p className="trust-help">任务文件夹不是全部读取边界：普通方式默认可读取当前账号允许的本机资料；明确禁止的内容仍不可读。允许位置内可以新建、写入、修改、删除文件和运行程序；超出范围或受限制联网按当前批准方式决定。完全访问权限会移除一般本地文件与联网隔离（电脑系统设置的访问限制），任务文件夹仍是默认工作位置。</p>
    </details>
    {error && <p role="alert">{error}</p>}
    <div className="trust-controls"><button type="button" className="trust-primary" aria-label="信任文件夹" disabled={busy} onClick={onTrust}>{busy ? '正在保存…' : '信任文件夹'}</button>
      <button type="button" className="trust-cancel" aria-label="取消文件夹确认" autoFocus onClick={onClose}>取消</button></div>
  </dialog>
}
