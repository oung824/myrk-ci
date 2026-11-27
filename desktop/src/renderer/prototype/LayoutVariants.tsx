import type { ReactNode } from 'react'
import './layout.css'

export type Variant = 'A' | 'B' | 'C'

export type LayoutProps = {
  variant: Variant
  nav: ReactNode
  header: ReactNode
  conversation: ReactNode
  workspace: ReactNode
  outline: ReactNode
  workspaceOpen: boolean
  navOpen: boolean
  onNavClose: () => void
}

export function VariantA(props: LayoutProps) {
  return (
    <div className={`prototype-layout variant-a ${props.workspaceOpen ? 'has-workspace' : ''} ${props.navOpen ? 'nav-open' : ''}`}>
      <button className="layout-nav-scrim" aria-label="关闭会话导航" onClick={props.onNavClose} />
      <aside className="layout-navigation" aria-label="会话导航">{props.nav}</aside>
      <div className="layout-main">
        <header className="layout-header">{props.header}</header>
        <div className="layout-split">
          <main className="layout-conversation" aria-label="对话">{props.conversation}</main>
          {props.workspaceOpen && <aside className="layout-workspace" aria-label="右侧文件查看区">{props.workspace}</aside>}
        </div>
      </div>
    </div>
  )
}

export function VariantB(props: LayoutProps) {
  return (
    <div className={`prototype-layout variant-b ${props.workspaceOpen ? 'has-workspace' : ''} ${props.navOpen ? 'nav-open' : ''}`}>
      <button className="layout-nav-scrim" aria-label="关闭会话导航" onClick={props.onNavClose} />
      <aside className="layout-navigation" aria-label="会话导航">{props.nav}</aside>
      <header className="layout-header">{props.header}</header>
      <section className="layout-overview" aria-label="任务概览">{props.outline}</section>
      <div className="layout-workbench">
        <main className="layout-conversation" aria-label="对话">{props.conversation}</main>
        {props.workspaceOpen && <aside className="layout-workspace" aria-label="右侧文件查看区">{props.workspace}</aside>}
        {!props.workspaceOpen && (
          <section className="layout-workspace-prompt" aria-label="文件查看区提示">
            <span className="layout-file-symbol" aria-hidden="true">▤</span>
            <h2>把成果展开，边看边聊</h2>
            <p>从任务概览或对话中打开文件，<br />它会留在这里，供你继续阅读。</p>
            <small>文件只读 · 支持多文件切换</small>
          </section>
        )}
      </div>
    </div>
  )
}

export function VariantC(props: LayoutProps) {
  return (
    <div className={`prototype-layout variant-c ${props.workspaceOpen ? 'has-workspace' : ''} ${props.navOpen ? 'nav-open' : ''}`}>
      <button className="layout-nav-scrim" aria-label="关闭会话导航" onClick={props.onNavClose} />
      <aside className="layout-navigation" aria-label="会话导航">{props.nav}</aside>
      <header className="layout-header">{props.header}</header>
      <div className="layout-task-body">
        <aside className="layout-task-outline" aria-label="任务步骤与成果">{props.outline}</aside>
        <main className="layout-conversation" aria-label="对话">{props.conversation}</main>
        {props.workspaceOpen && <aside className="layout-workspace" aria-label="右侧文件查看区">{props.workspace}</aside>}
      </div>
    </div>
  )
}

export function LayoutVariants(props: LayoutProps) {
  if (props.variant === 'B') return <VariantB {...props} />
  if (props.variant === 'C') return <VariantC {...props} />
  return <VariantA {...props} />
}
