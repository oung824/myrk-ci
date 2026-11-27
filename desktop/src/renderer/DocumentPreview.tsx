import { useEffect, useRef, useState } from 'react'
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFWorker, RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { DocumentPreviewContent } from '../shared/artifact-preview'
import './document-preview.css'

export function DocumentPreview({ content, snapshotId }: { content: DocumentPreviewContent; snapshotId?: string }) {
  const [section, setSection] = useState(0)
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(0)
  const [scale, setScale] = useState(1)
  const [fitWidth, setFitWidth] = useState(true)
  const [renderedScale, setRenderedScale] = useState(1)
  const [width, setWidth] = useState(0)
  const [rendered, setRendered] = useState(false)
  const [error, setError] = useState('')
  const canvas = useRef<HTMLCanvasElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const selected = content.sections[section] ?? content.sections[0]

  useEffect(() => { setSection(0); setPage(1); setFitWidth(true) }, [content, snapshotId])
  useEffect(() => {
    const element = scroller.current!
    const measure = () => setWidth(element.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    let current = true
    let task: PDFDocumentLoadingTask | undefined
    let worker: PDFWorker | undefined
    let port: Worker | undefined
    setPdf(null); setPages(0); setPage(1); setRendered(false); setError('')
    if (!selected) { setError('文档没有可显示的页面或可见工作表。'); return }
    void (async () => {
      const pdfjs = await import('pdfjs-dist')
      if (!current) return
      port = new Worker(new URL(workerUrl, document.baseURI), { type: 'module', name: 'myrk-document-preview' })
      port.addEventListener('error', () => {
        if (current) setError('本机 PDF 显示程序未能启动，请关闭预览后重新打开。')
      })
      worker = pdfjs.PDFWorker.create({ port })
      const data = Uint8Array.from(atob(selected.pdfBase64), character => character.charCodeAt(0))
      const assets = new URL('./pdf-assets/', document.baseURI)
      task = pdfjs.getDocument({
        data, worker, cMapUrl: new URL('cmaps/', assets).href, cMapPacked: true,
        standardFontDataUrl: new URL('standard_fonts/', assets).href, wasmUrl: new URL('wasm/', assets).href,
        useWorkerFetch: false, isEvalSupported: false, useSystemFonts: true,
      })
      const loaded = await task.promise
      if (current) { setPdf(loaded); setPages(loaded.numPages) }
    })().catch(reason => {
      if (!current) return
      const name = reason instanceof Error ? reason.name : ''
      setError(name === 'PasswordException' ? '此 PDF 需要密码，无法预览。请在原应用中打开。'
        : name === 'InvalidPDFException' ? 'PDF 已损坏或格式无效，无法预览。'
          : '本机未能显示此文档。可刷新重试，或在文件夹中查看原文件。')
    })
    return () => {
      current = false
      const terminate = () => { worker?.destroy(); port?.terminate() }
      if (task) void task.destroy().then(terminate, terminate)
      else terminate()
    }
  }, [selected?.pdfBase64, snapshotId])

  useEffect(() => {
    if (!pdf || !width) return
    let current = true
    let rendering: RenderTask | undefined
    setRendered(false)
    void pdf.getPage(page).then(async loaded => {
      if (!current) return
      const actualScale = fitWidth ? Math.min(2, Math.max(.1, (width - 32) / loaded.getViewport({ scale: 1 }).width)) : scale
      const viewport = loaded.getViewport({ scale: actualScale })
      const element = canvas.current!
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      element.width = Math.ceil(viewport.width * ratio)
      element.height = Math.ceil(viewport.height * ratio)
      element.style.width = `${viewport.width}px`
      element.style.height = `${viewport.height}px`
      rendering = loaded.render({ canvas: element, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] })
      await rendering.promise
      if (current) { setRenderedScale(actualScale); setRendered(true) }
    }).catch(reason => {
      if (current && !(reason instanceof Error && reason.name === 'RenderingCancelledException')) setError('这一页未能显示。请刷新重试，或在文件夹中查看原文件。')
    })
    return () => { current = false; rendering?.cancel() }
  }, [pdf, page, scale, fitWidth, width])

  const zoom = (difference: number) => {
    setRendered(false); setScale(Math.min(3, Math.max(.25, renderedScale + difference))); setFitWidth(false)
  }
  return <div className="document-preview">
    {content.format === 'xlsx' || content.format === 'pptx' ? <label className="document-section-picker">{content.format === 'xlsx' ? '工作表' : '幻灯片'}
      <select aria-label={content.format === 'xlsx' ? '工作表' : '幻灯片'} value={selected?.id ?? ''} onChange={event => {
        setRendered(false); setSection(content.sections.findIndex(item => item.id === event.target.value)); setPage(1)
      }}>{content.sections.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
    </label> : null}
    <div className="document-toolbar" role="toolbar" aria-label="文档阅读工具">
      <button type="button" aria-label="上一页" disabled={!pdf || page <= 1} onClick={() => { setRendered(false); setPage(value => value - 1); scroller.current?.scrollTo(0, 0) }}>上一页</button>
      <span aria-live="polite">{pages ? `${page} / ${pages}` : '— / —'}</span>
      <button type="button" aria-label="下一页" disabled={!pdf || page >= pages} onClick={() => { setRendered(false); setPage(value => value + 1); scroller.current?.scrollTo(0, 0) }}>下一页</button>
      <span className="document-toolbar-separator"/>
      <button type="button" aria-label="缩小" disabled={!pdf || renderedScale <= .25} onClick={() => zoom(-.25)}>−</button>
      <span>{fitWidth ? '适合宽度' : `${Math.round(scale * 100)}%`}</span>
      <button type="button" aria-label="放大" disabled={!pdf || renderedScale >= 3} onClick={() => zoom(.25)}>＋</button>
      <button type="button" onClick={() => { setRendered(false); setFitWidth(true) }} disabled={!pdf || fitWidth}>适合宽度</button>
    </div>
    {error && <p role="alert" className="document-preview-error">{error}</p>}
    {!error && !rendered && <p role="status" className="document-preview-loading">正在显示页面…</p>}
    <div className="document-pages" ref={scroller} tabIndex={0} aria-label="文档页面">
      <canvas ref={canvas} hidden={!pdf || !!error} aria-label={`第 ${page} 页`} data-rendered={rendered ? 'true' : 'false'}/>
    </div>
  </div>
}
