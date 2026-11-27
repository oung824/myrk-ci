import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'

export function projectRoot(source) {
  source = realpathSync(source)
  let root = source
  for (let current = source; ; current = dirname(current)) {
    if (existsSync(join(current, '.git'))) { root = current; break }
    if (dirname(current) === current) break
  }
  const marker = join(root, '.git')
  if (existsSync(marker) && statSync(marker).isFile()) {
    const registration = readFileSync(marker, 'utf8').trim()
    if (!registration.startsWith('gitdir:')) throw new Error('Git worktree registration is incomplete')
    let directory = resolve(root, registration.slice('gitdir:'.length).trim())
    const common = join(directory, 'commondir')
    if (existsSync(common)) directory = resolve(directory, readFileSync(common, 'utf8').trim())
    if (basename(directory) === '.git' && existsSync(directory) && statSync(directory).isDirectory()) root = dirname(directory)
  }
  return realpathSync(root)
}
