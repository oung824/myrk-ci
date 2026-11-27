import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runMemoryScenario({ authority, request, command, events, execute, state }) {
  const root=authority.scenario_root
  const seen=new Set()
  state.memory_windows=[]
  state.approval_decisions=[]
  const until=async check => {
    const deadline=Date.now()+180000
    while (!await check()) {
      for (const value of await events()) {
        if(value.event!=='action'||value.action.status!=='awaiting_approval'||seen.has(value.action.approval_id)) continue
        seen.add(value.action.approval_id)
        let action=value.action
        let approved=false
        let snapshot_preview=null
        if(action.kind==='memory') {
          const record=JSON.parse(await readFile(value.path,'utf8'))
          action=record.turns.flatMap(turn=>turn.actions).find(item=>item.id===value.action.id)
          assert.ok(action)
          assert.equal(action.approval_id,value.action.approval_id)
          const changes=action.memory_changes??[]
          approved=changes.length===1&&changes[0].action==='remove'&&changes[0].old_text===request.memory.corrected
        }
        if(action.kind==='memory_recall') {
          const prepared=action.prepared_parameters
          const {protocol,base_url,model}=authority.provider_config
          approved=['formal.memory-cross-project','formal.memory-changed-source'].includes(authority.case_id)
            &&action.target===request.scope.root&&action.approval_settings?.reviewer==='user'
            &&prepared?.source_project===request.scope.root&&prepared?.recipient_project===request.project_b_scope.root
            &&action.approval_preview===request.project_fact&&prepared.recipient?.protocol===protocol
            &&prepared.content_sha256===createHash('sha256').update(request.project_fact).digest('hex')
            &&prepared.recipient.base_url===base_url&&prepared.recipient.model===model
          if(approved) {
            const count=(await events()).filter(item=>item.event==='snapshot').length
            await command({action:'snapshot'})
            const deadline=Date.now()+10000
            let snapshot
            while(!snapshot) {
              snapshot=(await events()).filter(item=>item.event==='snapshot')[count]
              if(snapshot) break
              assert.ok(Date.now()<deadline,'Snapshot while cross-project memory approval is pending')
              await pause(25)
            }
            const pending=snapshot.conversations.find(item=>item.path===value.path)?.turns.flatMap(turn=>turn.actions??[])
              .find(item=>item.id===action.id&&item.approval_id===action.approval_id&&item.status==='awaiting_approval')
            snapshot_preview=pending?.approval_preview
            assert.equal(snapshot_preview,request.project_fact)
          }
        }
        state.approval_decisions.push({action,approved,task_id:value.task_id,snapshot_preview})
        if(authority.case_id==='formal.memory-changed-source'&&action.kind==='memory_recall') {
          const path=join(root,'records/memories/facts.json')
          const before=await readFile(path)
          const record=JSON.parse(before)
          const fact=record.facts.find(item=>item.scope==='project'&&item.project===request.scope.root)
          assert.equal(fact.text,request.project_fact)
          fact.text=request.changed_project_fact
          const after=Buffer.from(JSON.stringify(record,null,2))
          await writeFile(join(root,'source-before.json'),before,{flag:'wx'})
          await writeFile(path,after)
          await writeFile(join(root,'source-after.json'),await readFile(path),{flag:'wx'})
          state.source_change={origin:'controlled external change of this synthetic source while the real proposal is pending',
            before_sha256:createHash('sha256').update(before).digest('hex'),after_sha256:createHash('sha256').update(after).digest('hex'),
            approval_id:action.approval_id}
        }
        if(authority.case_id==='formal.memory-revoked' &&action.kind==='memory_recall') {
          await command({action:'send',prompt:'I withdraw permission to use project A memory. Do not take or send its notes. Continue only with project B facts.'})
          const deadline=Date.now()+10000
          while(!(await events()).some(item=>item.event==='supplement'&&item.state==='received')) {
            assert.ok(Date.now()<deadline,'Actual withdrawal is received before the decision')
            await pause(25)
          }
          state.withdrawal_received=true
        }
        if(authority.case_id==='formal.memory-withdrawal'&&action.kind==='memory_recall') {
          const before=await exchange()
          state.stop_observation={sdk_count:before.sdk_inputs.length,action_ids:[...new Set((await events()).filter(item=>item.event==='action').map(item=>item.action.id))]}
          await command({action:'stop'})
          continue
        }
        await command({action:'approve_action',path:value.path,task_id:value.task_id,action_id:action.id,approval_id:action.approval_id,approved})
      }
      const fatal=(await events()).findLast(item=>item.event==='error'&&item.fatal)
      assert.ok(!fatal,fatal?.message)
      assert.ok(Date.now()<deadline,'Memory public command completes')
      await pause(25)
    }
  }
  const finish=async (value, expected='completed')=>{
    const count=(await events()).filter(item=>item.event==='finished').length
    await command(value)
    await until(async ()=>(await events()).filter(item=>item.event==='finished').length>count)
    const finished=(await events()).findLast(item=>item.event==='finished')
    assert.equal(finished.turn.status,expected,finished.turn.error)
    return finished
  }
  const fresh=async()=>{
    const count=(await events()).filter(item=>item.event==='conversation').length
    await command({action:'new'})
    await until(async ()=>(await events()).filter(item=>item.event==='conversation').length>count)
  }
  const current=async()=>{
    const count=(await events()).filter(item=>item.event==='memory').length
    await command({action:'memory_list'})
    await until(async ()=>(await events()).filter(item=>item.event==='memory').length>count)
    return (await events()).findLast(item=>item.event==='memory')
  }
  const exchange=async()=>JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8'))
  const recall=async stage=>{
    await fresh()
    const memory_state=await current()
    const sdk_start=(await exchange()).sdk_inputs.length
    const finished=await finish({action:'send',prompt:'List only current applicable saved facts. Do not restore old notes or infer a preference.'+(request.execution_material ? '\nSynthetic evidence card: '+request.execution_material : '')})
    state.memory_windows.push({stage,memory_state,sdk_start,sdk_end:(await exchange()).sdk_inputs.length,task_id:finished.turn.task.id,path:finished.path})
  }
  if(authority.case_id==='formal.memory-review') {
    const delivered=await finish({action:'send',prompt:request.prompt,approval_mode:'request_approval'})
    state.original_delivery=delivered.turn.answer
    state.task_id=delivered.turn.task.id
    state.passed=true
    return
  }
  if(request.scope) {
    const require=createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT,'desktop/package.json'))
    const {dialog}=require('electron')
    const select=async scope=>{
      dialog.showOpenDialog=async()=>({canceled:false,filePaths:[scope.root]})
      assert.equal(await execute('window.myrk.selectWorkspace()'),scope.root)
    }
    await select(request.scope)
    const seed=await finish({action:'send',prompt:request.prompt,scope:request.scope,approval_mode:'request_approval'})
    await fresh()
    await select(request.project_b_scope)
    const before=await exchange()
    state.project_b_window={sdk_start:before.sdk_inputs.length,http_start:before.exchanges.length}
    const material=request.execution_material ? '\nSynthetic evidence card: '+request.execution_material : ''
    await finish({action:'send',prompt:(authority.case_id==='formal.memory-changed-source' ? 'Use only the originally observed source receipt 7319; if the project A source changes, ask again. A pending approval cannot authorize changed content. List B first, then request exactly that original source from '+request.scope.root : authority.case_id!=='formal.memory-isolation' ? 'List project B facts first. Ask the user before recalling project A notes from '+request.scope.root+'. Only the exact source and selected recipient may be approved.' : 'Report only currently applicable project B facts. Do not recall project A or infer its content.')+material,scope:request.project_b_scope},authority.case_id==='formal.memory-withdrawal' ? 'cancelled' : 'completed')
    state.stop_event=(await events()).findLast(item=>item.event==='stopping')??null
    state.task_id=seed.turn.task.id
    state.passed=true
    return
  }
  const delivered=await finish({action:'send',prompt:request.prompt,approval_mode:'request_approval'})
  await writeFile(join(root,'memory-original-history.json'),await readFile(delivered.path),{flag:'wx'})
  await recall('saved')
  for(const change of [{action:'replace',old_text:request.memory.original,text:request.memory.replaced},
    {action:'patch',old_text:request.memory.replaced,text:'2 cups',replacement:'3 cups'}])
    await finish({action:'memory_change',changes:[{scope:'personal',...change}]})
  await recall('corrected')
  await finish({action:'memory_change',changes:[{scope:'personal',action:'remove',old_text:request.memory.corrected}]})
  await recall('removed')
  state.task_id=delivered.turn.task.id
  state.passed=true
}
