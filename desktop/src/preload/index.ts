import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopAPI, ServiceEvent } from '../shared/protocol'

const api: DesktopAPI = {
  openArtifactPreview: request => ipcRenderer.invoke('myrk:open-artifact-preview', request),
  closeArtifactPreview: request => ipcRenderer.invoke('myrk:close-artifact-preview', request),
  revealArtifact: request => ipcRenderer.invoke('myrk:reveal-artifact', request),
  command: command => ipcRenderer.invoke('myrk:command', command),
  subscribe(listener) {
    const receive = (_event: Electron.IpcRendererEvent, event: ServiceEvent) => listener(event)
    ipcRenderer.on('myrk:event', receive)
    ipcRenderer.send('myrk:connect')
    return () => ipcRenderer.removeListener('myrk:event', receive)
  },
  copy: text => ipcRenderer.invoke('myrk:copy', text),
  revealFile: path => ipcRenderer.invoke('myrk:reveal-file', path),
  importConversation: () => ipcRenderer.invoke('myrk:import-conversation'),
  selectDocument: () => ipcRenderer.invoke('myrk:select-document'),
  selectWorkspace: () => ipcRenderer.invoke('myrk:select-workspace'),
  quit: () => ipcRenderer.invoke('myrk:quit'),
}

contextBridge.exposeInMainWorld('myrk', api)
