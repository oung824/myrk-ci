import type { DeliveredArtifact } from '../shared/protocol'
import { Icon } from './Icon'

export function DeliveredFiles({ artifacts, onOpen, selected }: { artifacts: DeliveredArtifact[]; onOpen?: (id: string) => void; selected?: string | null }) {
  return <section className="delivered-files" aria-label="本会话已交付文件">
    <p className="field-help">本会话已交付的文件。重新打开会话后仍可回看。</p>
    <ul>{artifacts.map(artifact => <li key={artifact.id}>
      <Icon name="file" size={19}/>
      <div className="delivered-file-name"><strong>{onOpen ? <button type="button" aria-label={`预览 ${artifact.name}`} aria-pressed={selected === artifact.id} onClick={() => onOpen(artifact.id)}>{artifact.name}</button> : artifact.name}</strong><span>{artifact.path}</span>
        <small>{artifact.size_bytes.toLocaleString()} 字节{artifact.delegation_id ? ' · 分工助理交付' : ''}</small>
      </div>
      <span className="delivered-change">{artifact.change_kind === 'created' ? '新建' : '修改'}</span>
    </li>)}</ul>
  </section>
}
