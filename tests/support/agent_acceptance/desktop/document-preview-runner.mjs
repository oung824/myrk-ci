import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { startDesktop } from '../../../../desktop/tests/desktop-process.ts'
const request=JSON.parse(await readFile(process.argv[2],'utf8'))
let report, error
const desktop=await startDesktop(join(request.root,'desktop'), request.driver, request.environment)
try {
  const deadline=Date.now()+request.timeout
  while(desktop.child.exitCode===null){assert.ok(Date.now()<deadline,desktop.diagnostic());await pause(25)}
  assert.equal(await desktop.closed,0,desktop.diagnostic())
} catch(failure){error=failure.stack;process.exitCode=1}
finally {report=await desktop.finish();await writeFile(join(request.root,'processes.json'),JSON.stringify({error,...report},null,2))}
