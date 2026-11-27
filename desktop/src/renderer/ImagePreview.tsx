import { useEffect, useRef, useState } from 'react'
import type { ArtifactPreviewResult, ImagePreviewContent } from '../shared/artifact-preview'
import type { DeliveredArtifact } from '../shared/protocol'

export function SnapshotImage({ content, name, onReadingChange }: { content: ImagePreviewContent; name: string; onReadingChange: (open: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const [zoom, setZoom] = useState(1)
  const dialog = useRef<HTMLDialogElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const source = `data:${content.mimeType};base64,${content.base64}`
  useEffect(() => {
    if (!open) return
    const modal = dialog.current!
    modal.showModal()
    return () => { modal.close(); onReadingChange(false) }
  }, [open])
  function close(): void { dialog.current?.close(); setOpen(false); trigger.current?.focus() }
  return <div className="artifact-image">
    <button ref={trigger} type="button" className="artifact-thumbnail" aria-label={`放大图片 ${name}`} onClick={event => { event.currentTarget.focus(); setZoom(1); onReadingChange(true); setOpen(true) }}><img src={source} alt={name}/></button>
    {open && <dialog ref={dialog} className="image-preview-dialog" aria-label={`图片大图 ${name}`} onCancel={event => { event.preventDefault(); close() }} onKeyDown={event => {
      if (event.key === 'Escape') event.stopPropagation()
      if (event.key === '+' || event.key === '=') setZoom(value => Math.min(4, value + .25))
      if (event.key === '-') setZoom(value => Math.max(.25, value - .25))
    }}>
      <header><strong>{name}</strong><div><button type="button" aria-label="缩小图片" disabled={zoom <= .25} onClick={() => setZoom(value => Math.max(.25, value - .25))}>−</button><output aria-label="图片缩放">{Math.round(zoom * 100)}%</output><button type="button" aria-label="放大图片" disabled={zoom >= 4} onClick={() => setZoom(value => Math.min(4, value + .25))}>+</button><button type="button" aria-label="关闭大图" onClick={close}>关闭</button></div></header>
      <div className="image-preview-scroll"><img src={source} alt={name} style={{ width: content.width * zoom, maxWidth: 'none', height: 'auto' }}/></div>
    </dialog>}
  </div>
}

export function DeliveredImage({ artifact, conversationId, onReadingChange }: { artifact: DeliveredArtifact; conversationId: string; onReadingChange: (open: boolean) => void }) {
  const [preview, setPreview] = useState<ArtifactPreviewResult | null>(null)
  useEffect(() => {
    let disposed = false
    const requestId = crypto.randomUUID()
    setPreview(null)
    void window.myrk.openArtifactPreview({ conversationId, artifactId: artifact.id, requestId }).then(result => {
      if (!disposed) setPreview(result)
    }).catch(() => {})
    return () => { disposed = true; void window.myrk.closeArtifactPreview({ requestId }).catch(() => {}) }
  }, [conversationId, artifact.id])
  if (!preview) return null
  return <div className="delivered-image">
    {preview.status !== 'ready' && <p className="preview-warning" role="status">{artifact.name}：{preview.status === 'missing' ? '图片目前已丢失。' : preview.status === 'unreadable' ? '图片目前无法读取。' : ''}{preview.message} 成果入口仍保留，可从“查看会话成果”查看文件信息。</p>}
    {preview.status === 'ready' && <>
      {preview.deliveryState === 'changed' && <p className="preview-warning" role="status">图片在交付后已有变化；当前显示的是后来版本的固定快照。</p>}
      {preview.content.kind === 'image' && <SnapshotImage content={preview.content} name={artifact.name} onReadingChange={onReadingChange}/>}
    </>}
  </div>
}
