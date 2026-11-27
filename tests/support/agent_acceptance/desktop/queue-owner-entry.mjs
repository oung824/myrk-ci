import { closeSync, openSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { save, verifyEntryAuthority } from './queue-entry-authority.mjs'

export function prepareOwner(directory, project, hash) {
  const authority = verifyEntryAuthority(directory, hash)
  const runtime = authority.runtime
  const history = []
  const stdout = openSync(join(runtime, 'relay/stdout.log'), 'wx')
  const stderr = openSync(join(runtime, 'relay/stderr.log'), 'wx')
  return {
    options: { entry: join(dirname(fileURLToPath(import.meta.url)), 'credential-relay.mjs'),
      env: { ...process.env, MYRK_CONNECTION_OWNER_CASE: '', MYRK_ACCEPTANCE_ROLE: 'relay',
        MYRK_TEST_NORMAL_EXIT_FLAG: join(runtime, 'relay/normal-exit.flag') },
      provider: authority.provider_config, caseNonce: authority.case_nonce, authorizationSha256: hash,
      verify: () => { verifyEntryAuthority(directory, hash) }, stdout, stderr,
      onState: state => { history.push(state); save(join(runtime, 'connection-owner-state.json'), { ...state, history }) } },
    afterSpawn(child) { closeSync(stdout); closeSync(stderr); save(join(runtime, 'owner-handoff.json'), { owner_pid: process.pid, child_pid: child.pid }) },
  }
}
