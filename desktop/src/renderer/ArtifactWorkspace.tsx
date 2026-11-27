import { lazy, Suspense, useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import type { DeliveredArtifact } from '../shared/protocol'
import type { ArtifactPreviewResult } from '../shared/artifact-preview'
import { DeliveredFiles } from './DeliveredFiles'
import { SnapshotImage } from './ImagePreview'
import './ArtifactWorkspace.css'

const DocumentPreview = lazy(() => import('./DocumentPreview').then(module => ({ default: module.DocumentPreview })))

export function ArtifactWorkspace({ conversationId, artifacts, onImageReadingChange }: { conversationId: string; artifacts: DeliveredArtifact[]; onImageReadingChange: (open: boolean) => void }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [preview, setPreview] = useState<ArtifactPreviewResult | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [comparison, setComparison] = useState(false)
  useEffect(() => {
    if (!selected) return
    let disposed = false
    const requestId = crypto.randomUUID()
    setLoading(true); setPreview(null); setError(''); setComparison(false)
    void window.myrk.openArtifactPreview({ conversationId, artifactId: selected, requestId }).then(result => {
      if (disposed) return
      setPreview(result); setLoading(false)
    }, reason => { if (!disposed) { setLoading(false); setError(reason instanceof Error ? reason.message : '预览未能打开，请刷新重试。') } })
    return () => { disposed = true; void window.myrk.closeArtifactPreview({ requestId }).catch(() => {}) }
  }, [conversationId, selected, refresh])
  const content = preview?.status === 'ready' ? preview.content : null
  const selectedArtifact = artifacts.find(item => item.id === selected)
  return <div className={`artifact-workspace${selected ? ' has-preview' : ''}`}>
    {selected ? <div className="artifact-switcher"><select aria-label="切换会话成果" value={selected} onChange={event => { setSelected(event.currentTarget.value); setRefresh(value => value + 1) }}>{artifacts.map(artifact => <option key={artifact.id} value={artifact.id}>{artifact.name} · {artifact.change_kind === 'created' ? '新建' : '修改'} · {new Date(artifact.delivered_at).toLocaleString()}</option>)}</select><button type="button" aria-label="查看全部成果" onClick={() => setSelected(null)}>全部成果</button></div> : <DeliveredFiles artifacts={artifacts} onOpen={id => { setSelected(id); setRefresh(value => value + 1) }} selected={selected}/>}
    {selected && <section className="artifact-preview" aria-label="成果预览">
      <header><details className="artifact-metadata"><summary>文件信息</summary><p>{selectedArtifact?.path}</p><p>{selectedArtifact?.size_bytes.toLocaleString()} 字节 · {selectedArtifact?.delegation_id ? '分工助理交付' : '主助理交付'}</p>
        {preview?.status === 'ready' && <p className="preview-state">这里是打开时的固定内容。刷新时重新读取。</p>}
        {content?.kind === 'text' && content.history.status === 'available' && <p>对应真实动作：{content.history.actionId}</p>}
      </details><div><button type="button" aria-label="刷新预览" onClick={() => setRefresh(value => value + 1)}>刷新</button><button type="button" aria-label="在文件夹中定位成果" onClick={() => {
        void window.myrk.revealArtifact({ conversationId, artifactId: selected }).catch(reason => setError(reason instanceof Error ? reason.message : '无法定位文件。'))
      }}>定位</button></div></header>
      {loading && <p role="status">正在读取文件快照…</p>}
      {error && <p role="alert">{error}</p>}
      {preview && preview.status !== 'ready' && <p role="status">{preview.message}</p>}
      {preview?.status === 'ready' && preview.deliveryState === 'changed' && <p className="preview-warning" role="status">文件在交付后已有变化；这里是打开时的固定内容。</p>}
      {content?.kind === 'text' && <>
        {content.history.status === 'available' ? <div className="preview-tabs"><button type="button" aria-pressed={!comparison} onClick={() => setComparison(false)}>全文</button><button type="button" aria-pressed={comparison} onClick={() => setComparison(true)}>修改对照</button></div> : <p className="field-help">{content.history.message}</p>}
        {comparison && content.history.status === 'available' ? <div className="artifact-comparison"><p>此次修改的前后全文</p><section><h3>修改前</h3><pre aria-label="修改前">{content.history.beforeText}</pre></section><section><h3>修改后</h3><pre aria-label="修改后">{content.history.afterText}</pre></section></div> : <div className={`artifact-fulltext ${content.format}`} aria-label="成果全文">{content.format === 'markdown' ? <Markdown components={{ img: ({ alt }) => <span>{alt || '图片（仅显示文字说明）'}</span> }}>{content.text}</Markdown> : <pre>{content.text}</pre>}</div>}
      </>}
      {content?.kind === 'image' && <SnapshotImage content={content} name={preview!.name} onReadingChange={onImageReadingChange}/>}
      {content?.kind === 'document' && preview?.status === 'ready' && <Suspense fallback={<p role="status">正在打开文档页面…</p>}><DocumentPreview key={preview.snapshotId} snapshotId={preview.snapshotId} content={content}/></Suspense>}
    </section>}
  </div>
}
