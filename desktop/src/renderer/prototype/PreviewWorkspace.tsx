import { useEffect, useState } from 'react'
import './workspace.css'

export type PreviewFile = {
  id: string
  name: string
  kind: 'docx' | 'xlsx' | 'pptx' | 'pdf' | 'code' | 'markdown' | 'text' | 'unsupported'
  status: 'new' | 'modified'
}

export const previewFiles: PreviewFile[] = [
  { id: 'brief', name: 'brief.docx', kind: 'docx', status: 'new' },
  { id: 'budget', name: 'budget.xlsx', kind: 'xlsx', status: 'new' },
  { id: 'proposal', name: 'proposal.pptx', kind: 'pptx', status: 'new' },
  { id: 'research', name: 'research.pdf', kind: 'pdf', status: 'new' },
  { id: 'search', name: 'search.ts', kind: 'code', status: 'modified' },
  { id: 'notes', name: 'notes.md', kind: 'markdown', status: 'new' },
  { id: 'changed', name: '行前提醒.txt', kind: 'text', status: 'modified' },
  { id: 'missing', name: '场地平面图.pdf', kind: 'pdf', status: 'new' },
  { id: 'legacy', name: '往期预算.xls', kind: 'unsupported', status: 'new' },
  { id: 'no-history', name: 'format-date.ts', kind: 'code', status: 'modified' },
]

const code = `export function searchDocuments(query: string, documents: Document[]) {
  const keyword = query.trim().toLocaleLowerCase()
  if (!keyword) return documents

  return documents
    .map((document) => {
      const title = document.title.toLocaleLowerCase()
      const body = document.body.toLocaleLowerCase()
      const score = title === keyword ? 3
        : title.includes(keyword) ? 2
        : body.includes(keyword) ? 1 : 0
      return { document, score }
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .map(({ document }) => document)
}`

const diff = [
  ['same', 'export function searchDocuments(query: string, documents: Document[]) {'],
  ['remove', '  return documents.filter((document) => document.title.includes(query))'],
  ...code.split('\n').slice(1, -1).map((line) => ['add', line]),
  ['same', '}'],
]

export function PreviewWorkspace({ fileId, onClose, onSelect }: {
  fileId: string
  onClose: () => void
  onSelect: (id: string) => void
}) {
  const file = previewFiles.find((item) => item.id === fileId) ?? previewFiles[0]
  const [page, setPage] = useState(1)
  const [sheet, setSheet] = useState('预算总览')
  const [codeView, setCodeView] = useState('全文')
  const [notice, setNotice] = useState('')
  const [revision, setRevision] = useState(1)
  useEffect(() => {
    setPage(1)
    setSheet('预算总览')
    setCodeView('全文')
    setNotice('')
    setRevision(1)
  }, [fileId])
  const office = ['docx', 'xlsx', 'pptx'].includes(file.kind)
  const paged = ['docx', 'pdf', 'pptx'].includes(file.kind) && file.id !== 'missing'
  const pages = file.kind === 'pptx' ? 4 : file.kind === 'pdf' ? 3 : 2
  const chartValues = sheet === '执行对照' ? [2200, 1500, 1050, 960, 0] : [2200, 1600, 1200, 1000, 600]

  return <section className="preview-workspace" aria-label="右侧文件查看区">
    <header className="wp-header">
      <div className={`wp-file-icon wp-file-${file.kind}`}>{file.kind === 'code' ? '</>' : file.kind === 'xlsx' ? 'X' : file.kind === 'pptx' ? 'P' : file.kind === 'docx' ? 'W' : '▤'}</div>
      <div className="wp-file-title"><strong>{file.name}</strong><span>示例预览 · 只读</span></div>
      <button className="wp-icon-button" aria-label="刷新文件预览" title="重新读取示例版本" onClick={() => {
        setRevision((value) => value + 1)
        setNotice(file.id === 'missing' ? '示例文件仍然找不到。可查看记录中的原位置。' : '已重新打开当前示例版本；阅读中不会自动刷新。')
      }}>↻</button>
      <button className="wp-icon-button" aria-label="关闭文件预览" title="返回对话" onClick={onClose}>×</button>
    </header>
    <div className="wp-toolbar">
      <label className="wp-file-picker"><span>文件</span><select aria-label="选择预览文件" value={file.id} onChange={(event) => onSelect(event.target.value)}>{previewFiles.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
      <span className="wp-status">{file.status === 'new' ? '新建' : '已修改'}</span>
    </div>
    {notice && <div className="wp-notice" role="status">{notice}</div>}
    {file.id === 'changed' && <div className="wp-notice wp-warning">文件在交付后有变化 · 当前显示示例版本 v2。本次阅读保持不变，刷新时重新载入。</div>}
    {file.id === 'missing' || file.id === 'legacy' ? <div className="wp-empty">
      <div className="wp-empty-symbol">{file.id === 'missing' ? '▧' : '▤'}</div>
      <h2>{file.id === 'missing' ? '原位置找不到这个文件' : '暂不支持此文件格式'}</h2>
      <p>{file.id === 'missing' ? '成果入口仍然保留。文件可能已被移动、改名或删除。' : '首版预览支持 .docx、.xlsx、.pptx；旧版和带宏文件可在本机应用中打开。'}</p>
      <button onClick={() => setNotice(`原位置（示例）：E:\\项目\\品牌提案\\${file.name}。此原型不会打开本机文件。`)}>查看原文件位置</button>
    </div> : <>
      {file.kind === 'code' && <div className="wp-code-controls"><div className="wp-segment" aria-label="代码查看方式">{['全文', '修改对照'].map((mode) => <button key={mode} aria-pressed={codeView === mode} onClick={() => setCodeView(mode)}>{mode}</button>)}</div><span>{file.id === 'search' ? 'TypeScript · 交付版本示例（未运行）' : 'TypeScript · 当前文本示例（未运行）'}</span></div>}
      {file.kind === 'code' && file.id === 'no-history' && <div className="wp-notice">未保留可靠的修改前版本，无法显示修改对照。仍可查看当前全文。</div>}
      <div className={`wp-content wp-content-${file.kind}`} key={`${file.id}-${revision}`}>
        {file.kind === 'docx' && <article className="wp-paper">
          <div className="wp-letterhead"><span>A LITTLE CLOSER TO NATURE</span><span>活动策划 / 2026.11</span></div>
          {page === 1 ? <><div className="wp-kicker">秋季品牌提案 / 轻量线下体验</div><h1>向自然靠近一点</h1><p className="wp-lead">用一次轻松的自然体验，让品牌走进真实生活。</p><div className="wp-cover-art"><div className="wp-leaf wp-leaf-one"/><div className="wp-leaf wp-leaf-two"/><div className="wp-leaf wp-leaf-three"/><div className="wp-sun"/><div className="wp-stem"/><span>PAUSE / FEEL / CONNECT</span></div><h2>01　活动概览</h2><p>为 24 位参与者设计一场自然、轻松的半日体验。以植物观察、轻手作与围坐交流串联品牌故事，让每个人带走一份亲手完成的小作品，保留充足的交流时间。</p><table className="wp-doc-table"><tbody><tr><th>活动时间</th><td>11 月 21 日（周六）09:30—13:00</td></tr><tr><th>集合地点</th><td>自然主题共享空间 · 城市待定</td></tr><tr><th>适合人群</th><td>品牌社群与对自然生活感兴趣的朋友</td></tr></tbody></table></> : <><div className="wp-kicker">让品牌表达，变成可参与的体验</div><h1>体验与执行安排</h1><h2>02　半日体验</h2><table className="wp-doc-table"><thead><tr><th>时间</th><th>地点</th><th>安排</th></tr></thead><tbody>{[['09:30', '共享空间', '签到、品牌主题介绍'], ['10:00', '庭院绿地', '植物观察、自然体验'], ['11:00', '手作区', '植物小作、轻食补给'], ['12:00', '交流区', '作品分享、社群交流'], ['13:00', '共享空间', '合影、领取行后小卡']].map((row) => <tr key={row[0]}>{row.map((cell) => <td key={cell}>{cell}</td>)}</tr>)}</tbody></table><h2>03　执行准备</h2><p>提前一天确认场地、物料和参与人数，发送集合与天气提醒。活动使用可重复利用的器皿，现场陈列控制在必要范围，让品牌信息自然融入体验。</p><div className="wp-callout">雨天方案：植物观察移入室内，保留手作与分享；活动前一天 18:00 确认调整。</div><h2>04　交付内容</h2><p>品牌提案、执行预算、汇报演示稿与体验调研。总预算预留约 10% 机动费用，场地按估算填写，确定城市后可进一步核实。</p></>}
          <footer className="wp-paper-footer"><span>秋季品牌活动小组</span><span>{page} / 2</span></footer>
        </article>}
        {file.kind === 'xlsx' && <div className="wp-sheet"><div className="wp-formula"><span>ƒx</span><span>{sheet === '预算总览' ? '=SUM(D3:D7)' : sheet === '费用明细' ? '=ROUND(B3*C3,0)' : '=B3-C3'}</span></div><div className="wp-sheet-heading"><span>秋季品牌提案 / {sheet}</span><strong>{sheet === '预算总览' ? '¥ 6,600' : sheet === '费用明细' ? '24 位参与者' : '预算内执行'}</strong></div><div className="wp-grid-scroll"><table className="wp-grid"><thead><tr>{['', 'A', 'B', 'C', 'D'].map((cell) => <th key={cell}>{cell}</th>)}</tr></thead><tbody><tr><th>1</th><td className="wp-grid-title" colSpan={4}>{sheet}</td></tr><tr><th>2</th>{(sheet === '预算总览' ? ['项目', '占比', '负责人', '预算（元）'] : sheet === '费用明细' ? ['项目', '人数 / 数量', '单价（元）', '小计（元）'] : ['项目', '预算（元）', '预计（元）', '余额（元）']).map((value) => <td className="wp-grid-label" key={value}>{value}</td>)}</tr>{(sheet === '预算总览' ? [['人员与策划', '33%', '活动组', '2,200'], ['茶歇与饮水', '24%', '后勤组', '1,600'], ['场地与布置', '18%', '后勤组', '1,200'], ['手作与物料', '15%', '活动组', '1,000'], ['机动费用', '10%', '活动组', '600']] : sheet === '费用明细' ? [['活动策划', '2', '1,100', '2,200'], ['茶歇与饮水', '24', '66.67', '1,600'], ['场地布置', '24', '50', '1,200'], ['手作物料', '24', '41.67', '1,000'], ['机动费用', '1', '600', '600']] : [['人员与策划', '2,200', '2,200', '0'], ['茶歇与饮水', '1,600', '1,500', '100'], ['场地与布置', '1,200', '1,050', '150'], ['手作与物料', '1,000', '960', '40'], ['机动费用', '600', '0', '600']]).map((row, index) => <tr key={row[0]}><th>{index + 3}</th>{row.map((value, column) => <td key={column} className={column ? 'wp-number' : ''}>{value}</td>)}</tr>)}<tr><th>8</th><td className="wp-grid-total">合计</td><td/><td/><td className="wp-grid-total wp-number">{sheet === '执行对照' ? '890' : '6,600'}</td></tr>{[9, 10, 11].map((row) => <tr key={row}><th>{row}</th><td/><td/><td/><td/></tr>)}</tbody></table></div><div className="wp-chart"><h3>{sheet === '执行对照' ? '预计支出分布' : '预算分配'}</h3><svg viewBox="0 0 450 160" role="img" aria-label={`${sheet === '执行对照' ? '预计支出' : '预算'}柱状图：${chartValues.join('、')} 元`}><path d="M42 10V130H435" fill="none" stroke="#d9dedb"/>{chartValues.map((value, index) => <g key={index}><rect x={65 + index * 72} y={130 - value / 2500 * 112} width="30" height={value / 2500 * 112} fill={['#566c5d', '#829585', '#a4b09d', '#b7c1b0', '#cdd3c4'][index]} rx="2"/><text x={80 + index * 72} y="151" textAnchor="middle" fontSize="10" fill="#69746b">{['人员', '茶歇', '场地', '物料', '机动'][index]}</text></g>)}<text x="32" y="18" fontSize="9" textAnchor="end" fill="#7c857d">2,500</text><text x="32" y="133" fontSize="9" textAnchor="end" fill="#7c857d">0</text></svg></div></div>}
        {file.kind === 'pptx' && <div className="wp-slides"><nav className="wp-slide-nav" aria-label="幻灯片列表">{[1, 2, 3, 4].map((slide) => <button key={slide} aria-label={`查看第 ${slide} 张幻灯片`} aria-current={page === slide ? 'page' : undefined} onClick={() => setPage(slide)}><span>{slide}</span><div>{['向自然靠近一点', '一次可以参与的品牌表达', '从想法，到轻量落地', '一起，向自然靠近一点'][slide - 1]}</div></button>)}</nav><div className={`wp-slide wp-slide-${page}`}><span className="wp-slide-brand">A LITTLE CLOSER TO NATURE</span>{page === 1 ? <><p className="wp-slide-eyebrow">秋季品牌提案 / 2026</p><h1>向自然<br/>靠近一点</h1><div className="wp-slide-arch"><div/><span>慢下来<br/>感受自然的美</span></div><p className="wp-slide-meta">2026.11.21 · 09:30<br/>24 位参与者 · 半日体验</p></> : page === 2 ? <><h1>一次可以参与的品牌表达</h1><p className="wp-slide-sub">四个体验环节，让品牌故事变得具体。</p><div className="wp-route-diagram">{['主题相遇', '自然观察', '植物手作', '围坐分享'].map((place, index) => <div key={place}><b>0{index + 1}</b><strong>{place}</strong><span>{['介绍与认识', '发现与感受', '体验与创作', '交流与连接'][index]}</span></div>)}</div></> : page === 3 ? <><h1>从想法，到轻量落地</h1><p className="wp-slide-sub">总预算 ¥6,600 · 含约 10% 机动费用</p><div className="wp-slide-budget">{[['人员与策划', '¥2,200', '100%'], ['茶歇与饮水', '¥1,600', '73%'], ['场地与布置', '¥1,200', '55%'], ['手作与物料', '¥1,000', '45%']].map(([name, amount, width]) => <div key={name}><span>{name}</span><i style={{ width }}/><b>{amount}</b></div>)}</div></> : <><p className="wp-slide-eyebrow">把品牌，带进真实生活</p><h1>一起，<br/>向自然靠近一点。</h1><div className="wp-slide-closing">一点好奇心 · 一次亲手体验 · 一份自然记忆</div><p className="wp-slide-meta">11 月 21 日 / 城市待定<br/>我们在这里等你。</p></>}<span className="wp-slide-page">0{page} / 04</span></div></div>}
        {file.kind === 'pdf' && <article className="wp-paper wp-pdf-paper"><div className="wp-letterhead"><span>FIELD NOTES</span><span>体验调研 · 2026</span></div><div className="wp-kicker">品牌体验观察 / 0{page}</div><h1>{['秋季体验调研', '体验环节与触点', '风险与替代方案'][page - 1]}</h1><p className="wp-lead">以自然、低压力的参与方式，为品牌社群设计一次可感知的相遇。</p><h2>{['体验概况', '空间与品牌触点', '天气与执行'][page - 1]}</h2><p>{['首选带自然采光的小型共享空间，邻近庭院或绿地，容纳 24 位参与者。场地应具备卫生间、饮水与休息位置，便于植物观察、手作和分享自然衔接。', '入口以一张主题卡引入品牌主张，庭院安排植物观察，室内长桌承接手作与茶歇。品牌信息以材料、色彩与引导语呈现，减少广告牌和打卡装置。', '若当日持续降雨，将植物观察改为室内标本与故事交流。活动前一天确认天气、场地安排和物料数量；如参与人数变化，优先调整茶歇与手作份数。'][page - 1]}</p><div className="wp-map"><div className="wp-plan-courtyard"/><div className="wp-plan-flow"/><span className="wp-map-label wp-map-start">主题入口</span><span className="wp-map-label wp-map-middle">自然观察</span><span className="wp-map-label wp-map-end">手作与分享</span></div><h2>执行建议</h2><p>每个体验环节预留自由交流时间，工作人员负责引导节奏与补充物料。参与者不必完成固定任务，允许停留、观察或安静地参与。</p><blockquote>好的品牌体验不只是传达信息，还应给参与者留下感受和交谈的时间。</blockquote><footer className="wp-paper-footer"><span>调研示例 · 场地尚未选定</span><span>{page} / 3</span></footer></article>}
        {file.kind === 'code' && <div className="wp-code-view">{codeView === '修改对照' && file.id === 'search' ? <><div className="wp-diff-caption"><span>执行前保存版本 → 交付版本</span><b>+{diff.filter(([kind]) => kind === 'add').length}　−1</b></div><pre>{diff.map(([kind, text], index) => <div className={`wp-code-line wp-diff-${kind}`} key={index}><span className="wp-line-number">{index + 1}</span><span className="wp-diff-mark">{kind === 'add' ? '+' : kind === 'remove' ? '−' : ' '}</span><code>{text || ' '}</code></div>)}</pre><p className="wp-code-footnote">对照使用原型中成对保存的示例文本，不推断缺失的修改历史。</p></> : <pre>{(file.id === 'search' ? code : `export function formatDate(date: Date) {\n  return new Intl.DateTimeFormat('zh-CN', {\n    month: 'long',\n    day: 'numeric',\n    weekday: 'long',\n  }).format(date)\n}`).split('\n').map((line, index) => <div className="wp-code-line" key={index}><span className="wp-line-number">{index + 1}</span><code>{line || ' '}</code></div>)}</pre>}</div>}
        {file.kind === 'markdown' && <article className="wp-markdown"><span className="wp-kicker">交付说明</span><h1>这次为你准备的文件</h1><p>秋季品牌提案、预算和展示材料已整理好。你可以在右侧切换查看，继续在对话里提出修改。</p><h2>内容一览</h2><ul><li><strong>品牌提案</strong>：主题、体验环节、分工与雨天方案。</li><li><strong>费用预算</strong>：预算总览、费用明细、执行对照三张表。</li><li><strong>汇报演示稿</strong>：四张从主题到执行的介绍页面。</li><li><strong>体验调研</strong>：场地目标、品牌触点与执行建议。</li></ul><h2>接下来可以做</h2><p>确认活动城市、场地和参与人数后，核实预算估算值，补充报名方式和联系人。</p><blockquote>这些文件是用于判断界面与操作的固定示例；当前原型不读取或修改你的文件。</blockquote><hr/><p className="wp-muted">成果随当前示例会话显示；刷新整个原型将重置内存中的演示状态。</p></article>}
        {file.kind === 'text' && <article className="wp-markdown"><span className="wp-kicker">当前示例版本 v2</span><h1>秋季见，向自然靠近一点</h1><p>集合时间：11 月 21 日 09:30</p><p>集合地点：自然主题共享空间（城市待定）</p><p>带着好奇心来就好，现场备有手作材料与茶歇。预计 13:00 结束，作品可以带回家。</p><p>如遇下雨，我们会在前一天 18:00 通知体验环节调整。</p><hr/><p className="wp-muted">交付记录为 v1；此处模拟文件在交付后被修改的提示。阅读时保持这一版本，点击刷新再载入。</p></article>}
      </div>
      {file.kind === 'xlsx' && <nav className="wp-sheet-tabs" aria-label="工作表">{['预算总览', '费用明细', '执行对照'].map((name) => <button key={name} aria-pressed={sheet === name} onClick={() => setSheet(name)}>{name}</button>)}</nav>}
      {paged && <nav className="wp-pagination" aria-label={file.kind === 'pptx' ? '幻灯片翻页' : '文件翻页'}><button aria-label="上一页" disabled={page === 1} onClick={() => setPage((value) => value - 1)}>‹</button><span>{page} / {pages}{file.kind === 'pptx' ? ' 张' : ' 页'}</span><button aria-label="下一页" disabled={page === pages} onClick={() => setPage((value) => value + 1)}>›</button></nav>}
    </>}
    <footer className="wp-footer">{office ? '固定排版示例，尚未接入真实文件渲染' : '固定示例 · 不读取或修改本机文件'}</footer>
  </section>
}
