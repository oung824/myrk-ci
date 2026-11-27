import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { ApprovalMode, Command, ConnectionStorageState, MemoryChange, MemoryFact, MemoryState, ModelCatalogResult, ProviderConfig } from '../shared/protocol'
import { defaultProvider } from './provider-defaults'
import { connectionIdentity } from '../shared/connection'
import { Icon } from './Icon'
import './SettingsDialog.css'

export interface SettingsValues {
  providerConfig: ProviderConfig
  delegationMaxTurns: number
  approvalMode: ApprovalMode
}

interface SettingsDialogProps {
  value: SettingsValues
  configured: boolean
  busy: boolean
  error: string
  memoryState: MemoryState | null
  modelCatalog: ModelCatalogResult | null
  connectionStorage: ConnectionStorageState | null
  onForget: (provider: ProviderConfig) => void
  onFetchCatalog: (request: Extract<Command, { action: 'fetch_model_catalog' }>) => Promise<void>
  onSave: (value: SettingsValues, key: string) => void
  onApprovalSave: (mode: ApprovalMode) => void
  onClose: () => void
  onMemoryList: () => void
  onMemorySettings: (approveWrites: boolean) => void
  onMemoryChange: (changes: MemoryChange[], project?: string) => void
  onImport: () => void
}

type SettingsPage = 'provider' | 'work' | 'memory' | 'records'

export function SettingsDialog({ value, configured, busy, error, memoryState, modelCatalog, connectionStorage, onForget, onFetchCatalog, onSave, onApprovalSave, onClose, onMemoryList, onMemorySettings, onMemoryChange, onImport }: SettingsDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null)
  const form = useRef<HTMLFormElement>(null)
  const password = useRef('')
  const [hasPassword, setHasPassword] = useState(false)
  const [page, setPage] = useState<SettingsPage>('provider')
  const [provider, setProvider] = useState(value.providerConfig)
  const [custom, setCustom] = useState(value.providerConfig.protocol === 'openai-chat')
  const google = provider.protocol === 'google'
  const catalogRequest = useRef<string | null>(null)
  const [catalog, setCatalog] = useState<ModelCatalogResult | null>(null)
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [catalogError, setCatalogError] = useState('')
  const [delegationMaxTurns, setDelegationMaxTurns] = useState(String(value.delegationMaxTurns))
  const [approvalMode, setApprovalMode] = useState(value.approvalMode)
  const [validation, setValidation] = useState('')
  const [invalidField, setInvalidField] = useState('')
  const [editingFact, setEditingFact] = useState<MemoryFact | null>(null)
  const [factText, setFactText] = useState('')
  const matchesConnection = (candidate: ProviderConfig) => connectionIdentity(provider) === connectionIdentity(candidate)
  const sameConnection = connectionStorage ? connectionStorage.available.some(matchesConnection) : configured && matchesConnection(value.providerConfig)
  const savedConnection = connectionStorage?.saved.some(matchesConnection) ?? false
  const canSave = page === 'provider' || page === 'work'

  function chooseModel(model: string) {
    setProvider(previous => previous.model === model ? previous : { ...previous, model,
      ...(previous.context_window_override === undefined ? {} : { context_window_override: null }),
      ...(previous.input_token_limit_override === undefined ? {} : { input_token_limit_override: null }),
    })
  }

  useEffect(() => {
    const opener = document.activeElement
    const window = dialog.current!
    window.showModal()
    return () => {
      password.current = ''
      catalogRequest.current = null
      window.close()
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])

  useEffect(() => {
    if (modelCatalog?.request_id === catalogRequest.current) {
      setCatalog(modelCatalog)
      setCatalogLoading(false)
      setCatalogError(modelCatalog.error ?? '')
    }
  }, [modelCatalog])

  function clearConnectionState() {
    password.current = ''
    setHasPassword(false)
    catalogRequest.current = null
    setCatalog(null)
    setCatalogLoading(false)
    setCatalogError('')
    form.current!.querySelector<HTMLInputElement>('#connection-password')!.value = ''
  }

  function fetchCatalog(force = false) {
    if (!google || busy || !password.current.trim() && !sameConnection || !force && catalogRequest.current !== null) return
    const request_id = crypto.randomUUID()
    catalogRequest.current = request_id
    setCatalog(null)
    setCatalogError('')
    setCatalogLoading(true)
    const key = password.current.trim()
    void onFetchCatalog({ action: 'fetch_model_catalog', request_id, ...(key ? { key } : {}), provider_config: { name: provider.name, base_url: provider.base_url, protocol: 'google' } }).catch(() => {
      if (catalogRequest.current === request_id) {
        setCatalogLoading(false)
        setCatalogError('未能请求模型目录，请重试；也可手动填写型号。')
      }
    })
  }

  useEffect(() => {
    if (google && sameConnection) fetchCatalog()
  }, [google])

  useEffect(() => {
    if (invalidField) form.current?.querySelector<HTMLElement>(`#${invalidField}`)?.focus()
  }, [page, invalidField])

  function save(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    const invalid = Array.from(form.current!.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-settings-field]')).find(control => !control.checkValidity())
    if (invalid) {
      const invalidPage = invalid.closest<HTMLElement>('[data-page]')!.dataset.page as SettingsPage
      const details = invalid.closest('details')
      if (details) details.open = true
      setPage(invalidPage)
      setValidation(invalid.id === 'connection-password' ? '请填写所选服务的连接密码。' : `请检查“${form.current!.querySelector<HTMLLabelElement>(`label[for="${invalid.id}"]`)!.textContent}”。`)
      setInvalidField(invalid.id)
      if (invalidPage === page) invalid.focus()
      return
    }
    const key = password.current.trim()
    if (!sameConnection && !key) {
      setPage('provider')
      setValidation('请填写所选服务的连接密码。')
      setInvalidField('connection-password')
      if (page === 'provider') form.current!.querySelector<HTMLInputElement>('#connection-password')!.focus()
      return
    }
    password.current = ''
    setHasPassword(false)
    form.current!.querySelector<HTMLInputElement>('#connection-password')!.value = ''
    setValidation('')
    onSave({ providerConfig: { ...provider, name: provider.name.trim(), base_url: provider.base_url.trim(), model: provider.model.trim() }, delegationMaxTurns: Number(delegationMaxTurns), approvalMode }, key)
  }

  return <dialog ref={dialog} className="settings settings-dialog" aria-labelledby="settings-title" onCancel={event => { event.preventDefault(); dialog.current!.close() }} onClose={onClose}>
    <header><div><span>Myrk · 桌面助理</span><h2 id="settings-title">设置</h2></div><button type="button" aria-label="关闭设置" onClick={() => dialog.current!.close()}><Icon name="close" size={20} /></button></header>
    <form ref={form} onSubmit={save} noValidate>
      <div className="settings-layout">
        <nav className="settings-navigation" aria-label="设置分类">
          {([
            ['provider', '回答服务', '连接与型号', '回答服务', 'server'],
            ['work', '工作偏好', '轮数与批准方式', '工作偏好', 'sliders'],
            ['memory', '长期记忆', '以后可复用的事实笔记', '查看长期记忆', 'memory'],
            ['records', '记录与导入', '复制导入，原件保留', '记录与导入', 'import'],
          ] as const).map(([key, label, help, ariaLabel, icon]) => <button key={key} type="button" aria-label={ariaLabel} aria-current={page === key ? 'page' : undefined} onClick={() => {
            setPage(key)
            setValidation('')
            setInvalidField('')
            if (key === 'memory') { setEditingFact(null); onMemoryList() }
          }}><span aria-hidden="true" className="settings-nav-icon"><Icon name={icon} size={18} /></span><span>{label}<small>{help}</small></span></button>)}
        </nav>
        <div className="settings-content">
          {(error || validation) && <p className="settings-error" role="alert">{validation || error}</p>}
          <section data-page="provider" hidden={page !== 'provider'} aria-label="回答服务设置">
            <div className="settings-page-title"><h3>回答服务</h3><p>选择谁来理解问题、生成回答，再填写连接资料。</p></div>
            <div className="provider-choices">
              <button type="button" aria-label="DeepSeek 回答服务" aria-pressed={!custom && !google} disabled={busy} onClick={() => {
                if (!custom && !google) return
                setCustom(false); setProvider(defaultProvider)
                clearConnectionState()
              }}><span className="provider-choice-icon" aria-hidden="true">D</span><span><strong>DeepSeek</strong><small>已有专用连接方式</small></span>{!custom && !google && <span className="provider-choice-check" aria-hidden="true"><Icon name="check" size={16} /></span>}</button>
              <button type="button" aria-label="Gemini 官方回答服务" aria-pressed={google} disabled={busy} onClick={() => {
                if (google) return
                setCustom(false); setProvider({ name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com', model: '', protocol: 'google' })
                clearConnectionState()
              }}><span className="provider-choice-icon" aria-hidden="true">G</span><span><strong>Gemini</strong><small>Google 官方服务 · AI Studio API Key</small></span>{google && <span className="provider-choice-check" aria-hidden="true"><Icon name="check" size={16} /></span>}</button>
              <button type="button" aria-label="自定义兼容服务" aria-pressed={custom} disabled={busy} onClick={() => {
                if (custom) return
                setCustom(true); setProvider({ name: '', base_url: '', model: '', protocol: 'openai-chat' })
                clearConnectionState()
              }}><span className="provider-choice-icon" aria-hidden="true"><Icon name="plus" size={17} /></span><span><strong>自定义兼容服务</strong><small>按相同问答约定接收资料</small></span>{custom && <span className="provider-choice-check" aria-hidden="true"><Icon name="check" size={16} /></span>}</button>
            </div>
            <fieldset disabled={busy} className="settings-fields">
              <legend>连接资料</legend>
              {custom && <><label htmlFor="service-name">服务商名称</label><input id="service-name" data-settings-field required value={provider.name} onChange={event => setProvider(previous => ({ ...previous, name: event.target.value }))} placeholder="填写显示名称" /></>}
              <label htmlFor="connection-password">连接密码</label>
              <input id="connection-password" data-settings-field type="password" required={!sameConnection} autoComplete="off" defaultValue="" onChange={event => {
                password.current = event.target.value
                setHasPassword(Boolean(event.target.value.trim()))
                catalogRequest.current = null
                setCatalog(null); setCatalogLoading(false); setCatalogError('')
              }} onBlur={() => fetchCatalog()} placeholder={sameConnection ? '已为当前服务设置；留空继续使用' : '填写所选服务的连接密码'} />
              <p className="field-help">{savedConnection ? '已保存到本机，留空继续使用；填写新密码可替换。' : sameConnection ? '本次可用，留空继续使用；保存成功后重开软件可继续使用。' : '保存后在本机加密记住，重开软件后恢复；不写进聊天记录。'}</p>
              {connectionStorage?.error && <p className="settings-error" role="alert">{connectionStorage.error}</p>}
              {(sameConnection || savedConnection) && <><button type="button" className="settings-secondary" onClick={() => { clearConnectionState(); onForget(provider) }}>忘记此连接密码</button><p className="field-help">仅忘记本机保存和后续工作的连接密码；已开始的工作继续，聊天和型号选择保留。</p></>}
              {google && <div className="model-catalog">
                <button type="button" className="settings-secondary" disabled={!hasPassword && !sameConnection} onClick={() => fetchCatalog(true)}>刷新模型目录</button>
                {catalogLoading && <p role="status">正在获取完整模型目录…</p>}
                {catalogError && <p className="settings-error" role="alert">{catalogError}</p>}
                {catalog && !catalog.error && catalog.models.length === 0 && <p className="field-help">目录读取成功，没有可列出的生成型号；仍可手动填写型号。</p>}
                {catalog && !catalog.error && catalog.models.length > 0 && <><label htmlFor="google-model-catalog">从模型目录选择</label><select id="google-model-catalog" value={catalog.models.some(item => item.id === provider.model) ? provider.model : ''} onChange={event => { if (event.target.value) chooseModel(event.target.value) }}>
                  <option value="">请选择型号</option>{catalog.models.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id} · 能力未确认</option>)}
                </select></>}
                <p className="field-help">输入 API Key 后离开输入框会自动获取目录。目录不能确认型号是否支持文字任务和工具，请按实际回应核实；也可在下方手动填写型号。</p>
              </div>}
              <label htmlFor="service-model">回答型号</label><input id="service-model" data-settings-field required value={provider.model} onChange={event => chooseModel(event.target.value)} placeholder="填写服务商提供的型号名称" />
              <p className="field-help">型号就是所选服务中的一种回答程序。</p>
              {google ? <p className="field-help">资料接收方：Google Gemini Developer API；地址：https://generativelanguage.googleapis.com。</p> : <details open={custom} className="advanced-settings" key={String(custom)}>
                <summary>{custom ? '接收地址与收发方式' : '高级连接设置'}</summary>
                <label htmlFor="service-address">具体接收地址</label><input id="service-address" data-settings-field type="url" required value={provider.base_url} onChange={event => setProvider(previous => ({ ...previous, base_url: event.target.value }))} placeholder="填写接收问题和资料的完整网址" />
                <label htmlFor="service-protocol">服务消息格式</label><select id="service-protocol" data-settings-field value={provider.protocol} onChange={event => setProvider(previous => ({ ...previous, protocol: event.target.value as ProviderConfig['protocol'] }))}>
                  <option value="deepseek">DeepSeek（当前服务专用消息格式）</option><option value="openai-chat">OpenAI 普通消息（兼容服务收发问答与动作的约定）</option>
                </select>
              </details>}
              <details className="advanced-settings">
                <summary>模型容量</summary>
                <p className="field-help">单位为 token（服务统计输入和输出大小的单位）。覆盖值只适用于当前型号；切换型号会清空覆盖。留空使用服务已知容量，容量未知时会明确停止。</p>
                <label htmlFor="context-window">总窗口覆盖</label><input id="context-window" data-settings-field type="number" min="1" step="1" value={provider.context_window_override ?? ''} onChange={event => setProvider(previous => ({ ...previous, context_window_override: event.target.value === '' ? null : Number(event.target.value) }))} />
                <label htmlFor="input-token-limit">独立输入上限覆盖</label><input id="input-token-limit" data-settings-field type="number" min="1" step="1" value={provider.input_token_limit_override ?? ''} onChange={event => setProvider(previous => ({ ...previous, input_token_limit_override: event.target.value === '' ? null : Number(event.target.value) }))} />
                <label htmlFor="output-token-reserve">输出预留</label><input id="output-token-reserve" data-settings-field type="number" min="1" step="1" required value={provider.output_token_reserve ?? 8192} onChange={event => setProvider(previous => ({ ...previous, output_token_reserve: Number(event.target.value) }))} />
              </details>
            </fieldset>
            <div className="settings-note">保存只表示设置已填写，不代表已经连通。旧工作要更换接收资料的地址，仍须另行审阅必要历史并批准；同名不同地址也算新接收方。</div>
          </section>
          <section data-page="work" hidden={page !== 'work'} aria-label="工作偏好设置">
            <div className="settings-page-title"><h3>工作偏好</h3><p>设置每名子助理可连续办事的轮数，以及新聊天的默认批准方式。</p></div>
            <fieldset disabled={busy} className="settings-fields"><legend>本机默认批准方式</legend>
              <label htmlFor="approval-mode">批准方式（需要扩大操作范围时由谁决定）</label>
              <select id="approval-mode" value={approvalMode} onChange={event => setApprovalMode(event.target.value as ApprovalMode)}>
                <option value="request_approval">请求批准</option>
                <option value="auto_review">帮我批准</option>
                <option value="full_access">完全访问权限</option>
              </select>
              <p className="field-help">请求批准：普通文件工作由电脑系统限制在允许范围；需要扩大范围时，由你本人决定。帮我批准：保持相同限制，由另外负责判断这一步的助理审核。受限联网通过中转按确切地址请求决定；本机服务可直接连接，内部地址没有额外拦截，不能保证本机连接逐项送审。完全访问权限：移除一般本地文件与联网限制，免普通逐项批准，仍受电脑账号和其他有效要求限制。</p>
              <p className="field-help">此默认值保存在本机，重开软件仍保留；仅用于之后新建的会话，不改变已有聊天保存的选择或已准备动作取得的授权。保存不需要回答服务的连接密码。</p>
              <button type="button" className="settings-secondary" onClick={() => onApprovalSave(approvalMode)}>保存默认批准方式</button>
            </fieldset>
            <fieldset disabled={busy} className="settings-fields"><legend>工作额度</legend>
              <label htmlFor="delegation-max-turns">子助理办事轮数上限</label><input id="delegation-max-turns" data-settings-field type="number" min="1" step="1" required value={delegationMaxTurns} onChange={event => setDelegationMaxTurns(event.target.value)} />
              <p className="field-help">一轮就是助理回答一次并决定下一步；每名子助理默认250轮，分别计数。主助理不设办事轮数上限。真实请求尝试另行统计，重试可能让请求次数多于轮数。子助理到限后交回已有成果，主助理仍可继续。</p>
            </fieldset>
            <label className="settings-switch"><span><strong>自动整理较早记录</strong><small>保存后用于当前服务配置和之后的工作。关闭后保留完整聊天及已有整理记录，容量不足时明确停止；已有执行轮保持原配置。</small></span><input aria-label="自动整理较早记录" type="checkbox" disabled={busy} checked={provider.auto_compaction ?? true} onChange={event => setProvider(previous => ({ ...previous, auto_compaction: event.target.checked }))} /></label>
          </section>
          <section hidden={page !== 'memory'} aria-label="长期记忆管理">
            <div className="settings-page-title"><h3>长期记忆</h3><p>这里保存以后工作可再次使用的精简事实。个人偏好可跨聊天使用，项目经验只用于所属项目；跨项目取用先确认。</p></div>
            {memoryState ? <>
              <label className="settings-switch"><span><strong>保存事实前批准</strong><small>开启后先让你检查准备保存的内容。这个开关立即生效，默认关闭。</small></span><input aria-label="保存事实前批准" type="checkbox" disabled={busy} checked={memoryState.approve_writes} onChange={event => onMemorySettings(event.target.checked)} /></label>
              {memoryState.warnings.map((warning, index) => <p className="settings-error" role="alert" key={index}>{warning}</p>)}
              {memoryState.facts.length === 0 && <p className="settings-empty">尚无当前事实笔记。</p>}
              {memoryState.facts.map(fact => <article className="settings-memory-fact" key={fact.id}>
                <p>{fact.scope === 'personal' ? '个人通用偏好' : `项目经验：${fact.project}`}</p><pre>{fact.text}</pre>
                <div><button type="button" className="settings-secondary" disabled={busy} aria-label={`纠正笔记：${fact.id}`} onClick={() => { setEditingFact(fact); setFactText(fact.text) }}>纠正</button><button type="button" className="settings-secondary settings-danger" disabled={busy} aria-label={`删除笔记：${fact.id}`} onClick={() => onMemoryChange([{ action: 'remove', scope: fact.scope, old_text: fact.text }], fact.project ?? undefined)}>删除所选整条（先查看影响并批准）</button></div>
              </article>)}
              {editingFact && <div className="settings-memory-edit"><label htmlFor="fact-text">纠正后的事实</label><textarea id="fact-text" aria-label="纠正后的事实" value={factText} onChange={event => setFactText(event.target.value)} /><div><button type="button" className="settings-secondary" onClick={() => setEditingFact(null)}>取消纠正</button><button type="button" className="primary" aria-label="保存事实纠正" disabled={busy || !factText.trim()} onClick={() => onMemoryChange([{ action: 'replace', scope: editingFact.scope, old_text: editingFact.text, text: factText }], editingFact.project ?? undefined)}>保存纠正</button></div></div>}
              <p className="memory-location">笔记位置：{memoryState.location}</p>
            </> : <p className="settings-empty">正在读取事实笔记…</p>}
            <div className="settings-note">有限危险文字筛查可能漏判；待审提议可能保存原文。删除只移除所选当前事实，旧聊天、摘要、备份、导出和其他待审提议保留；当前聊天背景到新聊天或资料整理时再读。</div>
          </section>
          <section hidden={page !== 'records'} aria-label="记录与导入设置">
            <div className="settings-page-title"><h3>记录与导入</h3><p>已有聊天仍从左侧记录列表打开。</p></div>
            <div className="settings-import"><span className="settings-import-icon" aria-hidden="true"><Icon name="import" size={24} /></span><h4>导入旧记录</h4><p>选择保存完整聊天的文件，检查合格后复制导入。</p><button type="button" className="primary" aria-label="导入旧记录" disabled={busy} onClick={onImport}>选择记录文件</button><p className="field-help">支持 .json（用固定字段保存完整聊天的文字文件）。<br />原文件留在原处；损坏或不支持的记录会提示原因。</p></div>
          </section>
        </div>
      </div>
      <footer><span>{canSave ? busy ? '已有操作正在处理中；关闭不会撤回已提交的保存请求。' : '保存后生效；取消会丢弃本次未保存的设置。' : '按页面中的具体操作办理。'}</span><div><button type="button" className="settings-secondary" onClick={() => dialog.current!.close()}>{canSave && !busy ? '取消' : '关闭'}</button>{canSave && <button className="primary" type="submit" aria-label="保存设置" disabled={busy}>{busy ? '暂不可保存' : '保存设置'}</button>}</div></footer>
    </form>
  </dialog>
}
