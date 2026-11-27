import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createHash, randomUUID } from 'node:crypto'
import { readFile, writeFile, unlink } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runDeliverables({ authority, request, command, events, execute, state }) {
  const require=createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT,'desktop/package.json'))
  const { dialog }=require('electron')
  const root=authority.scenario_root
  const save=async(name,value)=>writeFile(join(root,name),JSON.stringify(value,null,2))
  const until=async(check,label)=>{const deadline=Date.now()+120000;while(!await check()){assert.ok(Date.now()<deadline,label);await pause(25)}}
  const digest=bytes=>createHash('sha256').update(bytes).digest('hex')
  const preview=async(conversationId,artifactId)=>execute(`window.myrk.openArtifactPreview(${JSON.stringify({conversationId,artifactId,requestId:randomUUID()})})`)
  const rejectedPreview=async(conversationId,artifactId)=>execute(`window.myrk.openArtifactPreview(${JSON.stringify({conversationId,artifactId,requestId:randomUUID()})}).then(value=>({unexpected_result:value}),error=>({public_main_rejection:String(error)}))`)
  const close=async value=>{if(value.status==='ready')await execute(`window.myrk.closeArtifactPreview(${JSON.stringify({snapshotId:value.snapshotId})})`)}
  if(authority.phase==='work') {
    dialog.showOpenDialog=async()=>({canceled:false,filePaths:[request.scope.root]})
    assert.equal(await execute('window.myrk.selectWorkspace()'),request.scope.root)
    await command({action:'send',prompt:request.prompt,scope:request.scope,approval_mode:request.approval_mode})
    await until(async()=>(await events()).some(item=>item.event==='finished'),'actual multi-file task')
    const finished=(await events()).findLast(item=>item.event==='finished')
    assert.equal(finished.turn.status,'completed',finished.turn.error)
    assert.equal(finished.saved,true)
    assert.equal(finished.turn.artifacts.length,5)
    assert.equal(finished.turn.delivery_status,'awaiting_acceptance')
    assert.equal(finished.turn.checks_passed,null)
    const body=await execute('document.body.innerText')
    for(const name of ['new.txt','existing.txt','command.txt','child.txt'])assert.ok(body.includes(name),'Actual renderer lists '+name)
    await save('deliverables-original-state.json',{path:finished.path,task_id:finished.turn.task.id,turn:finished.turn,body})
    state.deliverables={actual_renderer_names:true,initial_delivery:'awaiting_acceptance',checks_passed:finished.turn.checks_passed}
  } else {
    const original=JSON.parse(await readFile(join(root,'deliverables-original-state.json'),'utf8'))
    await command({action:'open',path:original.path})
    await until(async()=>(await events()).some(item=>item.event==='conversation'&&item.path===original.path),'saved source conversation')
    const opened=(await events()).findLast(item=>item.event==='conversation'&&item.path===original.path)
    assert.deepEqual(opened.turns.at(-1).artifacts,original.turn.artifacts)
    assert.equal(opened.turns.at(-1).task.id,original.task_id)
    assert.equal(opened.turns.at(-1).task.total_requests,original.turn.task.total_requests)
    assert.equal(opened.turns.at(-1).delivery_status,'awaiting_acceptance')
    assert.equal((await events()).some(item=>item.event==='started'),false)
    const exchange=JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8'))
    assert.equal(exchange.exchanges.length,0)
    const artifacts=opened.turns.at(-1).artifacts
    const observations=[]
    for(const artifact of artifacts) {
      const before=await readFile(artifact.path)
      const result=await preview(original.path,artifact.id)
      assert.equal(result.status,'ready')
      assert.equal(result.sha256,digest(before))
      assert.equal(result.content.kind,'text')
      assert.equal(result.content.text,before.toString('utf8'))
      assert.equal(result.deliveryState,artifact.delivered_sha256===result.sha256?'unchanged':'changed')
      if(artifact.name==='existing.txt'&&result.deliveryState==='unchanged') {
        assert.equal(result.content.history.status,'available')
        assert.equal(result.content.history.beforeText,'header\nfirst-8420\nkeep-1084\n')
        assert.equal(result.content.history.afterText,'header\nfinal-9531\nkeep-1084\n')
      }
      await close(result)
      assert.equal(digest(await readFile(artifact.path)),digest(before),'Preview preserves original bytes')
      observations.push({artifact,result,source_before:digest(before),source_after:digest(await readFile(artifact.path))})
    }
    const current=artifacts.filter(item=>item.name==='existing.txt').at(-1)
    const action=opened.turns.flatMap(turn=>turn.actions).find(item=>item.id===current.source_action_id)
    const change=action.file_changes.find(item=>item.id===current.file_change_id)
    const backup=await readFile(change.backup_path)
    await unlink(change.backup_path)
    const lostSource=await preview(original.path,current.id)
    assert.equal(lostSource.status,'ready');assert.equal(lostSource.content.history.status,'unavailable')
    await close(lostSource);await writeFile(change.backup_path,backup)
    const artifact=artifacts.find(item=>item.name==='new.txt')
    const bytes=await readFile(artifact.path)
    await writeFile(artifact.path,'external-change-6092\n')
    const changed=await preview(original.path,artifact.id)
    assert.equal(changed.status,'ready');assert.equal(changed.deliveryState,'changed')
    assert.equal(changed.content.text,'external-change-6092\n')
    await close(changed)
    await unlink(artifact.path)
    const missing=await preview(original.path,artifact.id)
    assert.equal(missing.status,'missing')
    await writeFile(artifact.path,bytes)
    await command({action:'accept_delivery',path:original.path,task_id:original.task_id})
    await until(async()=>(await events()).some(item=>item.event==='delivery'&&item.turn.delivery_status==='accepted'),'controlled UI acceptance')
    await command({action:'new'})
    await until(async()=>(await events()).some(item=>item.event==='conversation'&&item.path!==original.path),'another real Conversation')
    const other=(await events()).findLast(item=>item.event==='conversation'&&item.path!==original.path)
    assert.equal(other.turns.flatMap(turn=>turn.artifacts??[]).length,0)
    assert.equal(other.turns.some(turn=>turn.delivery_status==='accepted'),false)
    const foreign=await rejectedPreview(other.path,artifact.id)
    assert.ok(foreign.public_main_rejection.includes('真实交付依据'))
    const inactive=await rejectedPreview(original.path,artifact.id)
    assert.ok(inactive.public_main_rejection.includes('所属会话'))
    assert.equal(digest(await readFile(artifact.path)),artifact.delivered_sha256)
    await command({action:'open',path:original.path})
    await until(async()=>(await events()).filter(item=>item.event==='conversation'&&item.path===original.path).length===2,'restore owner Conversation')
    const finalExchange=JSON.parse(await readFile(join(authority.runtime,'receiver/exchange.json'),'utf8'))
    assert.equal(finalExchange.exchanges.length,0)
    state.deliverables={reopen_identity_and_zero_send:true,cross_chat_isolation:true,changed_and_missing_honest:true,
      controlled_user_acceptance:'accepted_by_synthetic_UI_actor_only',real_user_acceptance:'pending',
      preview_observations:observations,missing_source:lostSource,changed,missing,foreign,inactive,other_conversation:other}
    await save('deliverables-reopen-state.json',state.deliverables)
  }
  state.passed=true
}
