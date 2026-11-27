export interface ArtifactPreviewRequest { conversationId: string; artifactId: string }
export interface ArtifactPreviewOpenRequest extends ArtifactPreviewRequest { requestId: string }
export type ArtifactPreviewCloseRequest = { snapshotId: string } | { requestId: string }

export interface TextPreviewContent {
  kind: 'text'
  text: string
  format: 'plain' | 'markdown' | 'code'
  language: string | null
  history: { status: 'available'; actionId: string; beforeText: string; afterText: string }
    | { status: 'unavailable' | 'created'; message: string }
}

export interface ImagePreviewContent {
  kind: 'image'
  mimeType: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'
  base64: string
  width: number
  height: number
}

export interface DocumentPreviewContent {
  kind: 'document'
  format: 'pdf' | 'docx' | 'xlsx' | 'pptx'
  sections: { id: string; label: string; pdfBase64: string }[]
}

export interface ReadyArtifactPreview extends ArtifactPreviewRequest {
  status: 'ready'
  snapshotId: string
  name: string
  sha256: string
  deliveryState: 'unchanged' | 'changed'
  content: TextPreviewContent | ImagePreviewContent | DocumentPreviewContent
}

export type ArtifactPreviewResult = ReadyArtifactPreview | (ArtifactPreviewRequest & {
  status: 'missing' | 'unreadable' | 'unsupported' | 'too_large'
  name: string
  message: string
})

export interface DocumentPreviewInput {
  snapshotId: string
  name: string
  extension: string
  bytes: Uint8Array
  sha256: string
  runtimeRoot: string
  projectRoot: string
  signal: AbortSignal
}

export interface PreparedDocumentPreview {
  content: DocumentPreviewContent
  close(): Promise<void>
}
