import { app, safeStorage } from 'electron'
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { installNormalExit } from './desktop-control.mjs'

installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.setPath('sessionData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
async function run() {
await app.whenReady()
try {
  const { ConnectionStore } = await import(pathToFileURL(process.env.MYRK_CONNECTION_STORE_MODULE).href)
  const directory = process.env.MYRK_CONNECTION_STORE_DIRECTORY
  const phase = process.env.MYRK_CONNECTION_STORE_PHASE
  const file = join(directory, 'connections.bin')
  const store = new ConnectionStore(file, safeStorage)
  const provider = { name: 'Example', base_url: 'https://example.test/v1', model: 'chat-example', protocol: 'openai-chat' }
  if (phase === 'save') {
    await store.save({ version: 1, connections: [{ provider_config: provider, key: 'synthetic-native-store-secret' }], selected_provider: provider })
    assert.ok(!(await readFile(file)).includes(Buffer.from('synthetic-native-store-secret')), 'Stored file must contain ciphertext')
  } else {
    const restored = await store.load()
    assert.ok(restored.connections.length === 1 && restored.connections[0].key === 'synthetic-native-store-secret', 'Saved password must restore in the new process')
    assert.deepEqual(restored.selected_provider, provider)
    await store.save({ version: 1, connections: [], selected_provider: provider })
    assert.equal((await store.load()).connections.length, 0)
  }
  await writeFile(join(directory, `${phase}.json`), JSON.stringify({ phase, complete: true, electron: process.versions.electron }))
  app.quit()
} catch (error) {
  console.error(error.message)
  app.exit(1)
}
}
void run()
