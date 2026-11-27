import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

export const digest = bytes => createHash('sha256').update(bytes).digest('hex')
export const save = (path, value) => writeFileSync(path, JSON.stringify(value, null, 2) + '\n')
export function verifyEntryAuthority(directory, expected) {
  const path = join(directory, 'authorization.json')
  const bytes = readFileSync(path)
  if (expected) assert.equal(digest(bytes), expected)
  const value = JSON.parse(bytes)
  for (const item of value.source_hashes) assert.equal(digest(readFileSync(item.path)), item.sha256)
  const checked = spawnSync(process.env.MYRK_PYTHON, ['-B', '-m', 'tests.support.agent_acceptance.formal_cli', 'validate', path],
    { windowsHide: true, env: process.env, encoding: 'utf8' })
  assert.equal(checked.status, 0, checked.stderr)
  return { ...value, authorization_sha256: digest(bytes) }
}
