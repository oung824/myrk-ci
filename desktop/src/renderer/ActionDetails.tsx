import type { Action } from '../shared/protocol'
import { RequestObservationDetails } from './RequestObservationDetails'
import { approvalLabels, requestActorLabels } from './ui-labels'

export function ActionDetails({ action, sharedRequests, onApprove, onRetry, retryDisabled, onNotice }: {
  action: Action
  sharedRequests: boolean
  onApprove?: (approved: boolean) => void
  onRetry?: () => void
  retryDisabled?: boolean
  onNotice: (message: string) => void
}) {
  const isMemoryRecall = action.kind === 'memory_recall'
  const recallContent = isMemoryRecall && action.status === 'awaiting_approval' ? action.approval_preview : undefined
  const approvalPreviewMissing = isMemoryRecall && recallContent === undefined
  const status = action.status === 'denied' && action.denial_source === 'review_incomplete' ? '审核未完成，动作未执行'
    : action.status === 'awaiting_approval' && action.approval_settings?.reviewer === 'assistant' && !action.review?.human_fallback ? '独立助理正在审核'
      : ({ started: '执行中', awaiting_approval: '等待你批准', completed: '已完成', denied: '已拒绝', failed: '失败', conflict: isMemoryRecall ? '待取用内容或接收方已变化' : '原文已变，等待重新判断', cancelled: '已停止' })[action.status]
  return <section className={`action-detail action-${action.status}`} aria-label={`动作：${action.target}`}>
    <p className="action-title"><strong>{({ create: '新建', write: '整份写入', command: '运行程序', list: '列举', read: '读取', search: '搜索', edit: '精确修改', calculate: '文字表格计算', check: '指定检查', delete: '删除', send: '新增外发', network: '联网' } as Record<string, string>)[action.kind] ?? '实际动作'}：{action.target}</strong><span className="action-status">{status}</span></p>
    {action.impact && <p>{action.impact}</p>}
    {recallContent !== undefined && <div><p>待提供给模型的笔记内容</p><pre aria-label="待提供给模型的笔记内容">{recallContent}</pre></div>}
    {action.approval_settings && <p>本次动作访问范围：{action.approval_settings.access.sandbox === 'full_access' ? '完全访问（移除一般本地文件与联网限制）' : action.approval_settings.access.legacy_scope ? '原记录授权范围（沿用已确认的旧限制）' : '普通系统限制（由电脑系统限制文件写入与联网）'}；准备时方式：{approvalLabels[action.approval_settings.mode]}。</p>}
    <p>本次动作审核者：{action.approval_settings ? ({ user: '你本人（由你决定是否执行这一步）', assistant: '独立助理（另外负责判断这一步的助理）', none: '无须普通逐项审核（按本次已保存的访问安排执行）' })[action.approval_settings.reviewer] : '原记录未记载（旧记录没有保存由谁决定）'}</p>
    <details className="task-record action-parameters"><summary>动作详情与参数</summary>
      <p>动作编号：{action.id}；开始：{action.started_at}{action.finished_at && `；结束：${action.finished_at}`}</p>
      {action.tool_name && <p>实际工具：{action.tool_name}</p>}
      {action.tool_source && <p>调用来源：{action.tool_source === 'extension' ? '扩展工具入口' : action.tool_source}</p>}
      {action.tool_schema_version && <p>工具定义版本（用于核对参数格式）：{action.tool_schema_version}</p>}
      {action.tool_arguments && <div><p>实际调用参数：</p><pre aria-label="实际调用参数">{JSON.stringify(action.tool_arguments, null, 2)}</pre></div>}
      {action.sandbox_type && <p>实际限制方式：{action.sandbox_type === 'windowsMxc' ? 'windowsMxc（微软原生隔离，由电脑系统把程序限制在允许范围）' : action.sandbox_type === 'none' ? 'none（未采用内层电脑系统限制）' : `${action.sandbox_type}（执行程序返回的电脑系统限制方式名称）`}</p>}
      {action.delegation_id && <p>所属分工：{action.delegation_id}</p>}
      {action.output && <pre>{action.output}</pre>}
      {action.cwd && <p>真实工作位置：{action.cwd}；退出情况：{action.exit_code ?? '尚未取得'}{action.timed_out ? '；超时' : ''}</p>}
      {action.stdout && <pre>正常输出：{action.stdout.length > 12000 ? `${action.stdout.slice(0, 6000)}\n中间部分已省略，全文见动作输出位置。\n${action.stdout.slice(-6000)}` : action.stdout}</pre>}
      {action.stderr && <pre>错误输出：{action.stderr.length > 12000 ? `${action.stderr.slice(0, 6000)}\n中间部分已省略，全文见动作输出位置。\n${action.stderr.slice(-6000)}` : action.stderr}</pre>}
      {action.backup_path && <p>原件恢复位置：{action.backup_path}</p>}
      {action.full_output_path && <p>省略部分的全文位置：{action.full_output_path}</p>}
    </details>
    {action.review && <details className="task-record" open={action.review.status === 'running' || action.review.status === 'denied' || action.review.human_fallback}>
      <summary>独立审核：{({ running: '正在核对', allowed: '已允许', denied: '已拒绝', failed: '未完成', timeout: '已超时', cancelled: '已停止', context_changed: '背景已变，原判断失效' })[action.review.status]}</summary>
      {action.review.risk && <p>影响风险：{({ low: '低（范围小、容易恢复）', medium: '中（影响有边界或可恢复）', high: '高（可能难以恢复或中断重要工作）', critical: '极高（可能泄露连接密码或造成重大不可逆损失）' })[action.review.risk]}。</p>}
      {action.review.authorization && <p>本人同意程度：{({ unknown: '未知（没有同意依据）', low: '低（只有含糊或松散关联的要求）', medium: '中（已同意实质效果）', high: '高（已明确要求这一步及相关影响）' })[action.review.authorization]}。</p>}
      <p>{action.review.reason}</p>
      <p>审核回答程序：{action.review.model}；接收地址：{action.review.provider}。</p>
      <p>本次审核尝试{action.review.attempts}次，用时{action.review.seconds.toFixed(1)}秒。读取检查和再次尝试共用本次审核的时间上限；实际请求已计入所属助理的次数。</p>
      {!!action.review.request_attempts?.length && <details aria-label="审核每次真实请求"><summary>审核每次真实请求（已计入实际次数，不重复累计）</summary>
        <p>审核所属助理：{action.review.request_owner_id === null ? '主助理' : `分工 ${action.review.request_owner_id}`}。</p>
        {action.review.request_attempts.map(attempt => <div key={attempt.number}>
          <p>整项第{attempt.number}次；{sharedRequests ? '共用账本' : '本助理'}第{attempt.own_number ?? '未记载'}次；用途：{requestActorLabels[attempt.actor ?? 'main']}；{attempt.status === 'completed' ? '已收到回应' : attempt.status === 'failed' ? '失败' : '等待回应'}。</p>
          <p>回答程序：{attempt.model}；接收地址：{attempt.provider}。</p>
          {attempt.http_status !== null && <p>HTTP（网络回应状态）：{attempt.http_status}。</p>}
          {attempt.error && <p>{attempt.error}</p>}<p>{attempt.decision}</p>
          <p>此次服务实际用量：{attempt.input_tokens === null || attempt.output_tokens === null ? '未知' : `输入${attempt.input_tokens}、输出${attempt.output_tokens}个文字计量单位（服务统计文字量时使用的单位）`}。</p>
          <RequestObservationDetails observation={attempt.observation}/>
        </div>)}
      </details>}
      {action.review.human_fallback && <p>必要资料超过独立审核可处理的数量，现交由你本人决定；这不表示动作已经获准。</p>}
      {action.review.user_override && <p>你已授权这一个具体动作再审核一次；仍由独立助理判断，不代表直接执行。</p>}
    </details>}
    {onRetry && <div><p>只对这一个动作再审一次，不代表直接执行；仍须独立助理根据上面的具体影响判断。</p><button className="button-secondary" disabled={retryDisabled} onClick={onRetry}>授权此动作再审核一次</button></div>}
    {onApprove && <>
      {approvalPreviewMissing && <p>当前无法查看待取用的笔记内容，暂时无法批准。</p>}
      <div className="action-decisions"><button className="button-primary" disabled={approvalPreviewMissing} onClick={() => onApprove(true)}>批准这次动作</button><button className="button-secondary" onClick={() => onApprove(false)}>拒绝这次动作</button></div>
    </>}
    {action.error && <p role="alert">{action.error}</p>}
    {action.effect === 'unknown' && <p className="attention-summary">实际影响尚未完全确定，请结合已保留文件、输出与错误核对。</p>}
    {action.status === 'completed' && ['create', 'write', 'edit', 'read', 'calculate'].includes(action.kind) && /^[A-Za-z]:[\\/]/.test(action.target) && <p><a className="file-location" href={`file:///${action.target.replace(/\\/g, '/')}`} aria-label={`在文件夹中查看：${action.target}`} onClick={event => {
      event.preventDefault()
      void window.myrk.revealFile(action.target).then(() => onNotice('已在文件夹中选中文件；请自行查看内容。'), () => onNotice('无法显示该文件位置；文件可能已移动或删除。'))
    }}>在文件夹中查看：{action.target}</a></p>}
  </section>
}
