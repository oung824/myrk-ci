import { createHash } from 'node:crypto'
import { lstat, readFile } from 'node:fs/promises'
import { basename, dirname, isAbsolute, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { ConnectionManager } from './connection-manager'
import { startConnectionHandoff } from './connection-handoff'

export async function attachConnectionOwner(manager: ConnectionManager, project: string, directory: string) {
  const root = resolve(project)
  const caseRoot = resolve(directory)
  const inside = (path: string, parent: string) => {
    const part = relative(parent, path)
    return part === '' || !part.startsWith('..') && !isAbsolute(part)
  }
  if (!isAbsolute(directory) || !inside(caseRoot, resolve(root, '.scratch'))) throw new Error('Invalid connection owner case')
  for (let location = resolve(caseRoot, 'authorization.json'); inside(location, root); location = dirname(location)) {
    if ((await lstat(location)).isSymbolicLink()) throw new Error('Linked connection owner authority')
    if (location === root) break
  }
  const raw = await readFile(resolve(caseRoot, 'authorization.json'))
  const value: unknown = JSON.parse(raw.toString('utf8'))
  if (typeof value !== 'object' || value === null || !('source_hashes' in value) || !Array.isArray(value.source_hashes)) throw new Error('Missing connection owner sources')
  const sources = new Set<string>()
  for (const item of value.source_hashes) {
    if (typeof item !== 'object' || item === null || typeof item.path !== 'string' || typeof item.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(item.sha256)) throw new Error('Invalid connection owner source')
    const path = resolve(item.path)
    if (!isAbsolute(item.path) || !inside(path, root) || sources.has(path) || !/\.(?:py|mjs|js|cjs|ps1|ts)$/.test(path)) throw new Error('Invalid connection owner source path')
    for (let location = path; inside(location, root); location = dirname(location)) {
      if ((await lstat(location)).isSymbolicLink()) throw new Error('Linked connection owner source')
      if (location === root) break
    }
    if (createHash('sha256').update(await readFile(path)).digest('hex') !== item.sha256) throw new Error('Connection owner source changed')
    sources.add(path)
  }
  const entries = [...sources].filter(path => basename(path) === 'queue-owner-entry.mjs')
  if (entries.length !== 1) throw new Error('An exact connection owner adapter is required')
  const entry = entries[0]
  for (const required of [entry, resolve(dirname(entry), 'queue-entry-authority.mjs'),
    ...['index.ts', 'connection-manager.ts', 'connection-handoff.ts', 'connection-owner-bootstrap.ts'].map(name => resolve(root, 'desktop/src/main', name))]) {
    if (!sources.has(required)) throw new Error('Connection owner source was not authorized')
  }
  const adapter = await import(/* @vite-ignore */ pathToFileURL(entry).href)
  const prepared = adapter.prepareOwner(caseRoot, root, createHash('sha256').update(raw).digest('hex'))
  const handoff = startConnectionHandoff(manager, prepared.options)
  try { prepared.afterSpawn(handoff.child) }
  catch {
    await handoff.shutdown()
    throw new Error('The connection owner process receipt was not completed')
  }
  return handoff
}
