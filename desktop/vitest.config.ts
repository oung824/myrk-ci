import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { join } from 'node:path'
import { testRun } from './tests/run-context.mjs'
import DurationSequencer from './tests/test-sequencer.mjs'

const run = testRun()
const serialTests = ['src/**/*.serial.test.ts', 'src/**/*.serial.test.tsx']

export default defineConfig({
  plugins: [react()],
  cacheDir: join(run.directory, 'cache', 'vite'),
  test: {
    environment: 'jsdom',
    maxWorkers: 4,
    sequence: { sequencer: DurationSequencer },
    setupFiles: ['./tests/dom-dialog.ts'],
    attachmentsDir: join(run.directory, 'attachments'),
    projects: [
      { test: { name: 'desktop', include: ['src/**/*.test.ts', 'src/**/*.test.tsx'], exclude: serialTests, sequence: { groupOrder: 0 } } },
      { test: { name: 'serial', include: serialTests, fileParallelism: false, sequence: { groupOrder: 1 } } },
    ],
  },
})
