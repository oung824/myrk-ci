import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityGovernance({ authority, request, command, events, execute, state }) {
  const require=createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT,'desktop/package.json'))
  const { dialog }=require('electron')
  dialog.showOpenDialog=async()=>({canceled:false,filePaths:[request.scope.root]})
  assert.equal(await execute('window.myrk.selectWorkspace()'),request.scope.root)
  if(request.project_trusted) await command({action:'set_project_trust',root:request.scope.root,trusted:true})
  state.approval_decisions=[]
  const seen=new Set()
  const mockStop = request.governance_adapter === 'qg3-9' && authority.grant.transport === 'mock'
  const arrived = join(authority.scenario_root, 'governance-stop-arrived.json')
  await command({action:'send',prompt:request.prompt,scope:request.scope})
  const deadline=Date.now()+180000
  while(!(await events()).some(item=>item.event==='finished')) {
    const all=await events()
    for(const value of all) {
      const action=value.action
      if(value.event!=='action'||action.status!=='awaiting_approval'||action.approval_settings?.reviewer!=='user')continue
      const key=`${value.task_id}:${action.id}:${action.approval_id}`
      if(seen.has(key))continue
      seen.add(key)
      assert.equal(action.kind,'create')
      assert.equal(action.target,request.governance_candidate)
      assert.equal(action.prepared_parameters.path,request.governance_candidate)
      assert.equal(existsSync(action.target),false)
      const approved=request.governance_adapter!=='qg3-9' && action.prepared_parameters.content===request.governance_content
      const decision={action_id:action.id,approval_id:action.approval_id,task_id:value.task_id,target:action.target,
        prepared_parameters:action.prepared_parameters,before_exists:false,approved,origin:'public_main_action_event',
        prepared_action_sha256:action.prepared_action_sha256,approval_context_sha256:action.approval_context_sha256}
      state.approval_decisions.push(decision)
      await command({action:'approve_action',path:value.path,task_id:value.task_id,action_id:action.id,approval_id:action.approval_id,approved})
    }
    const denied = request.governance_adapter === 'qg3-9' && all.find(item =>
      item.event === 'action' && item.action.status === 'denied' && state.approval_decisions.some(decision =>
        decision.approved === false && decision.task_id === item.task_id && decision.action_id === item.action.id))
    if (denied && !state.stop_sent && (!mockStop || existsSync(arrived))) {
      if (mockStop) {
        const held = JSON.parse(await readFile(arrived, 'utf8'))
        assert.equal(typeof held.sdk_input_id, 'string')
        state.stop_held_sdk_input_id = held.sdk_input_id
      }
      await command({ action: 'stop', path: denied.path })
      state.stop_sent = true
      const stopDeadline = Date.now() + 30000
      while (!(await events()).some(item =>
        item.event === 'stopping' && item.task_id === denied.task_id && item.path === denied.path)) {
        assert.ok(Date.now() < stopDeadline, 'The denied task must accept the explicit stop')
        await pause(25)
      }
      if (mockStop) await writeFile(join(authority.scenario_root, 'governance-stop-release.txt'), 'Accepted stop\n', { flag: 'wx' })
    }
    assert.ok(!all.some(item=>item.event==='error'&&item.fatal),JSON.stringify(all.filter(item=>item.event==='error')))
    assert.ok(Date.now()<deadline,'Canonical governance task finishes through public Main')
    await pause(25)
  }
  const all=await events()
  const finished=all.findLast(item=>item.event==='finished')
  assert.equal(finished.saved,true)
  assert.equal(finished.turn.status,request.governance_adapter==='qg3-9'?'cancelled':'completed',finished.turn.error)
  const record=JSON.parse(await readFile(finished.path,'utf8'))
  for(const decision of state.approval_decisions) {
    const action=record.turns.flatMap(turn=>turn.actions).find(item=>item.id===decision.action_id)
    assert.ok(action)
    assert.equal(action.approval_id,decision.approval_id)
    assert.equal(action.target,decision.target)
    assert.deepEqual(action.prepared_parameters,decision.prepared_parameters)
    decision.saved_candidate_matches=true
    decision.prepared_action_sha256_after_finished=action.prepared_action_sha256
    decision.approval_context_sha256_after_finished=action.approval_context_sha256
  }
  state.stop_accepted = state.stop_sent === true && all.some(item =>
    item.event === 'stopping' && item.task_id === finished.task_id && item.path === finished.path)
  state.task_id=finished.turn.task.id
  state.quality_finished=finished
  state.passed=true
}
