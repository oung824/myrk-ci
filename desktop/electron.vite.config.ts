import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'
import { readFileSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import type { Plugin } from 'vite'

function localPdfAssets(): Plugin {
  const directory = dirname(createRequire(import.meta.url).resolve('pdfjs-dist/package.json'))
  const folders = ['cmaps', 'standard_fonts', 'wasm']
  return {
    name: 'myrk-local-pdf-assets',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'pdf-assets/LICENSE.pdfjs.txt', source: readFileSync(join(directory, 'LICENSE')) })
      for (const folder of folders) for (const file of readdirSync(join(directory, folder))) {
        this.emitFile({ type: 'asset', fileName: `pdf-assets/${folder}/${file}`, source: readFileSync(join(directory, folder, file)) })
      }
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = request.url?.split('?')[0] ?? ''
        const match = /^\/pdf-assets\/(cmaps|standard_fonts|wasm)\/([\w.-]+)$/.exec(path)
        if (!match || !readdirSync(join(directory, match[1])).includes(match[2])) { next(); return }
        response.setHeader('Content-Type', match[2].endsWith('.wasm') ? 'application/wasm' : 'application/octet-stream')
        response.end(readFileSync(join(directory, match[1], match[2])))
      })
    },
  }
}

export default defineConfig(({ command }) => ({
  main: {},
  preload: {
    build: {
      rolldownOptions: {
        output: { format: 'cjs', entryFileNames: 'index.cjs' },
      },
    },
  },
  renderer: {
    resolve: { preserveSymlinks: true },
    plugins: [react(), localPdfAssets(), {
      name: 'myrk-development-content-policy',
      transformIndexHtml(html) {
        return command === 'serve' ? html.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'") : html
      },
    }],
  },
}))
