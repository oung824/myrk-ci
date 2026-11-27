import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import type { ChatModel, ModelSelection } from '../shared/protocol'
import './ModelControls.css'

interface ModelControlsProps {
  selection: ModelSelection
  models: ChatModel[]
  disabled: boolean
  busy: boolean
  legacy: boolean
  error: string
  onChange: (model: string, reasoning: string) => void
  onRefresh: () => void
}

export function ModelControls({ selection, models, disabled, busy, legacy, error, onChange, onRefresh }: ModelControlsProps) {
  const [panel, setPanel] = useState<'model' | 'reasoning' | null>(null)
  const [position, setPosition] = useState<CSSProperties>({ right: 12, bottom: 80, maxHeight: 350 })
  const controls = useRef<HTMLDivElement>(null)
  const popup = useRef<HTMLDivElement>(null)
  const opener = useRef<HTMLButtonElement | null>(null)
  const model = models.find(model => model.id === selection.provider_config.model)
  const options = model?.reasoning_options ?? []
  const optionIndex = options.findIndex(option => option.id === selection.reasoning)
  const savedLabel: Record<string, string> = { minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '超高', max: '最高', dynamic: '动态' }
  const label = legacy ? '原设置' : options[optionIndex]?.label ?? (selection.reasoning === 'none' ? '未启用推理' : savedLabel[selection.reasoning] ?? (options.length ? '待选择' : '不可调'))
  const modelName = model?.name ?? selection.provider_config.model

  function close() {
    setPanel(null)
    opener.current?.focus()
  }

  useEffect(() => {
    if (disabled) setPanel(null)
  }, [disabled])

  useLayoutEffect(() => {
    if (!panel) return
    function positionPanel() {
      const rect = controls.current!.getBoundingClientRect()
      const above = rect.top - 20
      const below = window.innerHeight - rect.bottom - 20
      const opensAbove = above >= 160 || above >= below
      setPosition({ right: Math.max(12, window.innerWidth - rect.right), maxHeight: Math.max(60, opensAbove ? above : below), ...(opensAbove ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }) })
    }
    positionPanel()
    const initialFocus = popup.current?.querySelector<HTMLElement>(panel === 'reasoning' ? 'input:not(:disabled), .reasoning-initial-choice' : '[aria-selected="true"]:not(:disabled), [role="option"]:not(:disabled)') ?? popup.current
    initialFocus?.focus()
    function outside(event: PointerEvent) {
      if (!controls.current?.contains(event.target as Node) && !popup.current?.contains(event.target as Node)) close()
    }
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); close() }
    }
    window.addEventListener('resize', positionPanel)
    document.addEventListener('scroll', positionPanel, true)
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('resize', positionPanel)
      document.removeEventListener('scroll', positionPanel, true)
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [panel])

  return <div className="model-controls" ref={controls} aria-busy={busy}>
    <div className="model-selection-pill">
      <button type="button" aria-label="选择模型" aria-expanded={panel === 'model'} aria-haspopup="listbox" title={selection.provider_config.model} disabled={disabled || busy} onClick={event => { opener.current = event.currentTarget; setPanel(panel === 'model' ? null : 'model') }}><span className="selected-model-name">{modelName}</span></button>
      <button type="button" className="reasoning-choice" aria-label="选择推理强度" aria-expanded={panel === 'reasoning'} aria-haspopup="dialog" disabled={disabled || busy || legacy} onClick={event => { opener.current = event.currentTarget; setPanel(panel === 'reasoning' ? null : 'reasoning') }}>{busy ? '保存中' : label}<span aria-hidden="true">⌄</span></button>
    </div>
    {error && <span className="model-controls-error" role="alert">{error}</span>}
    {panel && createPortal(<div ref={popup} tabIndex={-1} className={`model-controls-popup ${panel === 'reasoning' ? 'reasoning-panel' : 'model-list-panel'}`} style={position} role={panel === 'reasoning' ? 'dialog' : undefined} aria-label={panel === 'reasoning' ? '调整推理强度' : '可选模型'}>
      {panel === 'model' ? <>
        <div className="model-list-heading"><span>选择模型</span><button type="button" aria-label="刷新可选模型" disabled={busy} onClick={onRefresh}>刷新</button></div>
        <div role="listbox" aria-label="对话模型" onKeyDown={event => {
          if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)'))
          const index = items.indexOf(document.activeElement as HTMLButtonElement)
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
          items[next]?.focus()
        }}>
          {(models.length ? models : [{ id: selection.provider_config.model, name: modelName, capability: 'unknown' as const, reasoning_options: options, default_reasoning: 'default', reasoning_note: null, tools_supported: null, unavailable_reason: null }]).map(item => <button type="button" role="option" aria-selected={item.id === selection.provider_config.model} key={item.id} disabled={busy || Boolean(item.unavailable_reason)} title={item.unavailable_reason ?? item.id} onClick={() => { onChange(item.id, item.default_reasoning); close() }}><span>{item.name}<small>{item.unavailable_reason ?? (item.capability === 'unknown' ? '能力未确认' : item.name !== item.id ? item.id : '')}</small></span><span aria-hidden="true">{item.id === selection.provider_config.model ? '✓' : ''}</span></button>)}
        </div>
      </> : <>
        <header><span className="reasoning-bolt" aria-hidden="true">ϟ</span><div><strong>{label}</strong><span>{modelName}</span></div>{model && options.length > 0 && <button type="button" aria-label="恢复初始推理强度" title="恢复此型号初始强度" disabled={busy} onClick={() => onChange(selection.provider_config.model, model.default_reasoning)}>↺</button>}</header>
        {optionIndex >= 0 ? <>
          <input aria-label="推理强度" aria-valuetext={label} type="range" min="0" max={options.length - 1} step="1" value={optionIndex} disabled={options.length < 2} onChange={event => onChange(selection.provider_config.model, options[Number(event.target.value)].id)} />
          <div className="reasoning-endpoints"><span>{options[0].label}</span><span>{options.length > 1 ? options.at(-1)?.label : '不可调节'}</span></div>
        </> : options.length > 0 && <button type="button" className="reasoning-initial-choice" disabled={busy} onClick={() => onChange(selection.provider_config.model, options[0].id)}>选择{options[0].label}档</button>}
        {(model?.reasoning_note || !options.length) && <p>{model?.reasoning_note ?? '此型号尚无可确认的推理档位。'}</p>}
      </>}
    </div>, document.body)}
  </div>
}
