import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { readFile, writeFile } from 'node:fs/promises'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityFiles({ authority, request, command, events, execute, state }) {
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const wait = async check => { const deadline=Date.now()+120000; while(!await check()){assert.ok(Date.now()<deadline,'quality actual Main event'); await pause(25)} }
  dialog.showOpenDialog = async () => ({ canceled:false, filePaths:[request.scope.root] })
  assert.equal(await execute('window.myrk.selectWorkspace()'),request.scope.root)
  if(request.document){ dialog.showOpenDialog=async()=>({canceled:false,filePaths:[request.document]}); assert.equal(await execute('window.myrk.selectDocument()'),request.document) }
  await command({ action:'send', prompt:request.prompt, scope:request.scope, ...(request.implementation_negative==='approval_mode_reset'?{approval_mode:'request_approval'}:{}), ...(request.document?{document:request.document}:{}) })
  await wait(async()=>(await events()).some(item=>item.event==='finished'))
  let finished=(await events()).findLast(item=>item.event==='finished')
  if(request.interrupt_then_continue){
    assert.equal(finished.saved,true)
    assert.equal(finished.turn.status,'failed')
    assert.equal(finished.turn.service_failure,true)
    const original=await readFile(finished.path)
    const originalTurn=JSON.parse(original.toString('utf8')).turns.at(-1)
    assert.equal(originalTurn.status,'failed')
    assert.equal(originalTurn.service_failure,true)
    assert.equal(originalTurn.answer,finished.turn.answer)
    assert.ok(originalTurn.answer.trim())
    assert.equal(originalTurn.task.id,finished.turn.task.id)
    const originalSha=createHash('sha256').update(original).digest('hex')
    await writeFile(join(authority.scenario_root,'interrupted.original.json'),original)
    const exchangeBefore=JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8')).exchanges.length
    await command({action:'open',path:finished.path})
    await wait(async()=>(await events()).some(item=>item.event==='conversation'&&item.path===finished.path))
    const reopened=(await events()).findLast(item=>item.event==='conversation'&&item.path===finished.path)
    assert.equal(reopened.turns.at(-1).answer,originalTurn.answer)
    assert.equal(reopened.turns.at(-1).task.id,originalTurn.task.id)
    const exchangeAfter=JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8')).exchanges.length
    const previousRecovery=(await events()).length
    await command({action:'verify_recovery',path:finished.path,task_id:finished.turn.task.id})
    const currentRecovery=async()=>(await events()).slice(previousRecovery).find(item=>
      item.event==='recovery'&&item.path===finished.path&&item.recovery.task_id===finished.turn.task.id)
    await wait(currentRecovery)
    const verified=await currentRecovery()
    assert.equal(verified.saved,true)
    const recovery=verified.recovery
    assert.equal(recovery.can_resume,true)
    const verifiedCount=JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8')).exchanges.length
    const count=(await events()).filter(item=>item.event==='finished').length
    await command({action:'resume_task',path:finished.path,task_id:finished.turn.task.id})
    await wait(async()=>(await events()).filter(item=>item.event==='finished').length>count)
    finished=(await events()).findLast(item=>item.event==='finished')
    await writeFile(join(authority.scenario_root,'interruption-observation.json'),JSON.stringify({open_sends:exchangeAfter-exchangeBefore,verification_sends:verifiedCount-exchangeAfter,original_sha256:originalSha,recovery}))
  }
  assert.equal(finished.turn.status,'completed',finished.turn.error)
  assert.equal(finished.saved,true)
  state.task_id=finished.turn.task.id
  state.quality_finished=finished
  state.passed=true
}
