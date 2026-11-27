import type { RequestObservation } from '../shared/protocol'

const changeReasonLabels = new Map([
  ['new_task', '新任务'],
  ['authorization_changed', '授权范围改变'],
  ['provider_changed', '服务或型号改变'],
  ['document_changed', '授权资料改变'],
  ['goal_changed', '任务目标改变'],
  ['instructions_changed', '指令改变'],
  ['capacity_compression', '容量变化，整理待发记录'],
  ['memory_refresh', '刷新事实笔记'],
  ['core_tools_changed', '核心工具定义改变'],
  ['final_turn_tools_removed', '最后一轮停用动作工具'],
  ['extension_schema_refresh_required', '扩展工具定义需要刷新'],
  ['first_request', '首次请求'],
])

export function RequestObservationDetails({ observation }: { observation?: RequestObservation | null }) {
  if (!observation) return <p>缓存观测：未知（旧记录未保存）。</p>
  const wire = observation.wire
  return <section aria-label="此次请求缓存观测">
    <p>缓存用量：输入总量{observation.input_tokens ?? '未知'}；缓存读取{observation.cache_read_tokens ?? '未知'}；缓存写入{observation.cache_write_tokens ?? '未知'}个文字计量单位。缓存读取已包含在输入总量中，不另行相加。</p>
    {observation.protocol === 'google' && <p>输出用量：输出总量{observation.output_tokens ?? '未知'}；其中思考{observation.thinking_tokens ?? '未知'}个文字计量单位。输出总量包含候选回答和思考；任一计数缺失时，总量保持未知。</p>}
    <p>用量状态：{({ unknown: '未知', partial: '部分字段已取得', complete: '已取得', conflict: '字段冲突，请查看来源' })[observation.usage_status]}。请求耗时：{observation.elapsed_seconds === null ? '未知' : `${observation.elapsed_seconds.toFixed(3)}秒`}；首段输出：{observation.first_output_seconds === null ? '未知' : `${observation.first_output_seconds.toFixed(3)}秒`}（首个非空正文、思考或工具调用片段）。</p>
    <details><summary>缓存来源与请求摘要</summary>
      <p>服务：{observation.provider}；协议：{({ deepseek: 'DeepSeek Chat', 'openai-chat': 'OpenAI Chat', google: 'Google Gemini', unknown: '未知' })[observation.protocol]}；型号：{observation.model}；接收地址：{observation.endpoint}；布局版本：{observation.layout_version ?? '未知'}。</p>
      <p>输入来源：{observation.input_source ?? '未知'}；输出来源：{observation.output_source ?? '未知'}；缓存读取来源：{observation.cache_read_source ?? '未知'}；缓存写入来源：{observation.cache_write_source ?? '未知'}。</p>
      {observation.protocol === 'google' && <p>思考来源：{observation.thinking_source ?? '未知'}；服务原始结束原因：{observation.finish_reason ?? '未知'}。</p>}
      <p>服务用量字段：{observation.usage_present ? '存在' : '未取得'}。</p>
      <dl aria-label="原始用量字段">
        {Object.entries(observation.raw_usage).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}
      </dl>
      <p>字段存在情况：{Object.entries(observation.field_presence).map(([name, present]) => `${name}：${present ? '存在' : '缺失'}`).join('；') || '未知'}。</p>
      <p>SDK（服务连接库）标准计数仅供核对，缺失字段可能被默认成0：{Object.entries(observation.sdk_usage).map(([name, value]) => `${name}：${value}`).join('；') || '未知'}。</p>
      {observation.conflicts.length > 0 && <p>来源冲突：{observation.conflicts.join('；')}。</p>}
      {wire ? <>
        <p>最终发送摘要：消息{wire.message_count}条；工具{wire.tool_count}个；正文{wire.body_bytes}字节。SHA-256（用于比较内容是否改变的摘要），不保存额外正文，也不能据此判断服务端一定命中缓存。</p>
        <p>完整请求：<code>{wire.body_sha256}</code>；系统说明{observation.protocol === 'google' ? '（systemInstruction）' : ''}：<code>{wire.system_sha256}</code>；工具{observation.protocol === 'google' ? '（tools）' : ''}：<code>{wire.tools_sha256}</code>；消息{observation.protocol === 'google' ? '（contents）' : ''}：<code>{wire.messages_sha256}</code>。</p>
        <p>逐条消息摘要：{wire.message_hashes.join('；')}。</p>
        <p>变化原因：{wire.change_reasons.map(reason => changeReasonLabels.get(reason) ?? reason).join('；') || '未记录'}。</p>
      </> : <p>最终发送摘要：未知。</p>}
    </details>
  </section>
}
