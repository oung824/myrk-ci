import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runDelegationScenario({ authority, request, command, events, execute, state }) {
  const require=createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT,'desktop/package.json'))
  const { dialog }=require('electron')
  dialog.showOpenDialog=async()=>({canceled:false,filePaths:[request.scope.root]})
  assert.equal(await execute('window.myrk.selectWorkspace()'),request.scope.root)
  if (request.delegation_max_turns !== 250) {
    assert.equal(request.delegation_max_turns,3)
    await command({action:'configure',delegation_max_turns:request.delegation_max_turns,provider_config:authority.provider_config})
    state.explicit_child_limit_configured=true
    state.configuration_storage=(await events()).findLast(item=>item.event==='connection_storage')
    assert.ok(state.configuration_storage.error, 'Synthetic receiver refuses credential persistence during public settings update')
  }
  await command({action:'send',prompt:request.prompt,scope:request.scope,approval_mode:request.approval_mode})
  const deadline=Date.now()+180000
  while (!(await events()).some(item=>item.event==='finished')) {
    const all=await events()
    state.denial_observations ??= []
    for (const value of all) {
      if (value.event!=='action' || value.action.status!=='awaiting_approval' || state.denial_observations.some(item=>item.action.id===value.action.id)) continue
      assert.equal(authority.case_id,'formal.delegation-scope-deny')
      assert.equal(value.action.target,join(authority.scenario_root,'outside-created.txt'))
      assert.equal(value.action.kind,'create')
      assert.ok(value.action.delegation_id)
      assert.equal(value.action.approval_settings.reviewer,'user')
      assert.equal(existsSync(value.action.target),false)
      const decision={action:value.action,task_id:value.task_id,approved:false,origin:'actual public Main approval event',observed_at:new Date().toISOString()}
      state.denial_observations.push(decision)
      await command({action:'approve_action',path:value.path,task_id:value.task_id,action_id:value.action.id,approval_id:value.action.approval_id,approved:false})
    }
    assert.ok(!all.some(item=>item.event==='error' && item.fatal),JSON.stringify(all.filter(item=>item.event==='error')))
    assert.ok(Date.now()<deadline,'Delegation finishes through the public Main command')
    await pause(25)
  }
  const finished=(await events()).findLast(item=>item.event==='finished')
  assert.equal(finished.turn.status,'completed',finished.turn.error)
  const record=JSON.parse(await readFile(finished.path,'utf8'))
  assert.equal(record.turns.at(-1).task.id,finished.turn.task.id)
  state.task_id=finished.turn.task.id
  state.passed=true
}
