import { createRoot } from 'react-dom/client'

if (import.meta.env.DEV && new URLSearchParams(window.location.search).get('prototype') === 'frontend') {
  const { PrototypeApp } = await import('../prototype/PrototypeApp')
  createRoot(document.getElementById('root')!).render(<PrototypeApp />)
} else {
  const { App } = await import('../App')
  createRoot(document.getElementById('root')!).render(<App />)
}
