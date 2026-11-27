import { useState } from 'react'
import type { CompactionEvidence, CompactionSource, CompactionStatus } from '../shared/protocol'

function recordStatus(record: CompactionStatus, activeTaskId: string | null): string {
  return record.status === 'completed' ? '已整理较早记录' : record.status === 'failed' ? '整理失败，原始记录保留' : record.status === 'cancelled' ? '整理已取消，未采用候选摘要' : record.task_id === activeTaskId ? '正在整理较早记录' : '整理曾中断，未采用候选摘要'
}

export function CompactionRecords({ records, activeTaskId, evidence, onRead }: { records: CompactionStatus[]; activeTaskId: string | null; evidence: CompactionEvidence | null; onRead: (summaryId: string, source: Pick<CompactionSource, 'message_index' | 'part_index'>, start: number) => void }) {
  const [selected, setSelected] = useState<Record<string, number>>({})

  if (!records.length) return null
  return <details className="task-record" aria-label="较早记录整理情况"><summary>较早记录整理情况 · {recordStatus(records.at(-1)!, activeTaskId)}</summary>
    <p>工作摘要只供继续原工作参考；完整聊天和原始工具证据保留。它不会新增授权或表示动作已经执行。</p>
    {records.map(record => {
      const source = record.source[selected[record.id] ?? 0]
      const current = evidence?.summary_id === record.id ? evidence : null
      const measurement = ({ local_text_estimate: '本机文字估算', local_text_estimate_after_compaction: '整理后本机文字估算', provider_usage_plus_local_delta: '服务已报告用量加新增内容的本机估算' } as Record<string, string>)[record.measurement_source] ?? `估算来源未识别（${record.measurement_source}）`
      return <section key={record.id}>
        <p>{recordStatus(record, activeTaskId)}；型号：{record.model}；布局版本：{record.layout_version}。</p>
        <p>整理编号：{record.id}；原因：{record.trigger === 'capacity_compression' ? '待发资料接近容量' : record.trigger}；替换较早原文{record.covered.length}个部件，近期原文保留{record.retained.length}个部件。</p>
        <p>待发输入计量：整理前{record.before_tokens}，整理后{record.after_tokens ?? '未知'}；可用输入预算{record.input_budget_tokens} token（服务统计输入和输出大小的单位）。计量来源：{measurement}；此处为估算或当前输入预算，不表示总窗口的确定比例。缓存读取包含在输入中，不扣作可用容量。</p>
        <p>本次整理实际请求序号：{record.request_numbers.join('、') || '尚未请求'}；用量见所属助理的逐次请求记录，已包含在整项累计次数中。</p>
        {record.error && <p role="alert">{record.error}</p>}
        {record.status === 'completed' && <details><summary>查看工作摘要</summary><pre>{record.summary}</pre></details>}
        {source && <div>
          <label>登记的原文部件<select aria-label={`整理来源：${record.id}`} value={selected[record.id] ?? 0} onChange={event => setSelected(previous => ({ ...previous, [record.id]: Number(event.target.value) }))}>
            {record.source.map((ref, index) => <option key={`${ref.message_index}:${ref.part_index}`} value={index}>消息{ref.message_index + 1}／部件{ref.part_index + 1}</option>)}
          </select></label>
          <button type="button" onClick={() => onRead(record.id, source, 0)}>回查整理原文</button>
        </div>}
        {current && <div aria-label="整理来源原文">
          <p>原文消息{current.message_index + 1}／部件{current.part_index + 1}，从第{current.start + 1}个字符起；按登记位置与指纹核对后读取。</p>
          {current.error ? <p role="alert">{current.error}</p> : <pre>{current.text}</pre>}
          {current.start > 0 && <button type="button" onClick={() => onRead(record.id, current, Math.max(0, current.start - 8000))}>上一段原文</button>}
          {current.next_start !== null && <button type="button" onClick={() => onRead(record.id, current, current.next_start!)}>下一段原文</button>}
        </div>}
      </section>
    })}
  </details>
}
