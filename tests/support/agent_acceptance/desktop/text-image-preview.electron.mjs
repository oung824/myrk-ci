import { app, BrowserWindow, dialog } from 'electron'
import assert from 'node:assert/strict'
import { access, readFile, rename, writeFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'
import { ProcessObservation } from '../../../../desktop/tests/process-observation.mjs'

installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData',process.env.MYRK_USER_DATA_DIRECTORY)
const root=process.env.MYRK_PREVIEW_ROOT
const phase=process.env.MYRK_ACCEPTANCE_RESTART_PHASE
const out=join(root,phase)
const expected=join(root,'expected')
const result={phase,pid:process.pid,passed:false,source_checks:[],previews:[],critical_facts:[],display_measurements:{}}
const hash=bytes=>createHash('sha256').update(bytes).digest('hex')
let window, execute
const until=async(check,label)=>{const end=Date.now()+25000;while(!await check()){assert.ok(Date.now()<end,label);await pause(25)}}
const text=name=>readFile(join(expected,name),'utf8')
const click=label=>execute(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===${JSON.stringify(label)}||b.textContent===${JSON.stringify(label)});if(!b||b.disabled)throw new Error('Missing enabled control: '+${JSON.stringify(label)});b.click()})()`)
const command=value=>execute(`window.myrk.command(${JSON.stringify(value)})`)
const events=()=>execute('window.previewEvents')
const capture=async name=>{await execute('document.fonts.ready');await execute('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');await writeFile(join(out,name+'.png'),(await window.webContents.capturePage()).toPNG())}
const checkSources=async(label,report='after.txt')=>{
  const rows=[]
  for(const chat of ['A','B'])for(const [name,source] of [['report.txt',chat==='A'?report:'other.txt'],['long.txt','long.txt'],['picture.png','picture.png']]){
    const actual=await readFile(join(root,chat,name));assert.deepEqual(actual,await readFile(join(expected,source)),label+' leaves original '+chat+'/'+name+' unchanged');rows.push({chat,name,sha256:hash(actual)})
  }
  result.source_checks.push({label,rows})
}
const openApi=async(chat,artifact)=>{
  const value=await execute(`window.myrk.openArtifactPreview(${JSON.stringify({conversationId:chat.path,artifactId:artifact.id,requestId:randomUUID()})})`)
  result.previews.push(value)
  return value
}
const closeApi=value=>value.status==='ready'?execute(`window.myrk.closeArtifactPreview(${JSON.stringify({snapshotId:value.snapshotId})})`):Promise.resolve()
const select=async artifact=>{
  if(await execute('!!document.querySelector("[aria-label=切换会话成果]")'))await execute(`(()=>{const s=document.querySelector('[aria-label="切换会话成果"]');s.value=${JSON.stringify(artifact.id)};s.dispatchEvent(new Event('change',{bubbles:true}))})()`)
  else await click('预览 '+artifact.name)
  await until(()=>execute('!document.querySelector(".artifact-preview [role=status]")?.textContent.includes("正在读取")'),'preview read completed')
}
const openChat=async chat=>{await command({action:'open',path:chat.path});await until(async()=> (await events()).findLast(e=>e.event==='conversation')?.path===chat.path,'actual saved chat opens');await until(()=>execute('!!document.querySelector(".conversation")'),'chat rendered')}
const http=async()=>JSON.parse(await readFile(join(out,'exchange.json'),'utf8')).exchanges.length

async function heldOriginal(chat,artifact,kind="lock"){
  const controls=join(out,'owned-'+kind);const locked=controls+'.locked';const release=controls+'.release'
  const literal=p=>`'${p.replaceAll("'","''")}'`
  const lockScript=`$h=[IO.File]::Open(${literal(artifact.path)},[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None);try{[IO.File]::WriteAllText(${literal(locked)},'locked');while(-not [IO.File]::Exists(${literal(release)})){Start-Sleep -Milliseconds 25}}finally{$h.Dispose()}`
  const aclScript=`$ErrorActionPreference='Stop';$p=${literal(artifact.path)};$a=Get-Acl -LiteralPath $p;$original=$a.Sddl;[IO.File]::WriteAllText(${literal(controls+'.before.sddl')},$original);$id=[Security.Principal.WindowsIdentity]::GetCurrent().User;$r=[Security.AccessControl.FileSystemAccessRule]::new($id,[Security.AccessControl.FileSystemRights]::ReadData,[Security.AccessControl.AccessControlType]::Deny);$a.AddAccessRule($r);try{Set-Acl -LiteralPath $p -AclObject $a;[IO.File]::WriteAllText(${literal(locked)},'owned file ReadData denied');while(-not [IO.File]::Exists(${literal(release)})){Start-Sleep -Milliseconds 25}}finally{$restore=Get-Acl -LiteralPath $p;$restore.SetSecurityDescriptorSddlForm($original);Set-Acl -LiteralPath $p -AclObject $restore;[IO.File]::WriteAllText(${literal(controls+'.after.sddl')},(Get-Acl -LiteralPath $p).Sddl)}`
  const script=kind==='acl'?aclScript:lockScript
  const executable='C:\\Program Files\\PowerShell\\7\\pwsh.exe'
  const argv=['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')]
  const observer=await ProcessObservation.start(join(out,kind+'-observer'))
  const child=spawn(executable,argv,{cwd:out,env:process.env,windowsHide:true,stdio:'ignore'})
  const closed=new Promise(r=>child.once('close',r))
  try{
    const owned=await observer.capture(child,[executable,...argv],out);assert.equal(owned.ownership,'owned')
    await until(()=>access(locked).then(()=>true,()=>false),'real exclusive file lock held')
    const refused=await openApi(chat,artifact);assert.equal(refused.status,'unreadable');assert.match(refused.message,kind==='acl'?/EACCES|EPERM|permission|访问/i:/EBUSY|busy|使用/i)
    await click('刷新预览');await until(()=>execute(`document.querySelector('.artifact-preview [role=status]')?.textContent === ${JSON.stringify(refused.message)}`),'exact real read denial visible')
    result.display_measurements[kind+'_status']=await execute('document.querySelector(".artifact-preview [role=status]").textContent');await capture('unreadable-real-'+kind);result[kind==='acl'?'permission_cause':'unreadable_cause']={kind:kind==='acl'?'owned file ACL ReadData denial':'Windows exclusive handle',message:refused.message,owner:owned}
  }finally{
    await writeFile(release,'release owned handle');await closed;const final=await observer.wait(5000);await observer.close();assert.equal(final.complete,true);result[kind+"_process"]=final
  }
  if(kind==='acl'){const before=await readFile(controls+'.before.sddl','utf8');const after=await readFile(controls+'.after.sddl','utf8');assert.equal(after.replace('D:AI','D:'),before.replace('D:AI','D:'),'owner/group/access entries and protection unchanged; Windows may normalize AUTO_INHERITED');result.original_acl_restored=true;result.acl_restore={original_sddl:before,restored_sddl:after,raw_sddl_identical:before===after,owner_group_access_entries_and_protection_equal:true,normalization_only:before===after?null:'SE_DACL_AUTO_INHERITED'}}
  await checkSources('after actual unreadable '+kind+' restored')
}

async function run(){try{
  const binding=JSON.parse(await readFile(join(root,'execution-bindings.json'),'utf8'))
  for(const [name,checksum] of Object.entries(binding.compiled_sources))assert.equal(hash(await readFile(join(binding.compiled_root,name))),checksum,'compiled input bound before Main')
  for(const [name,checksum] of Object.entries(binding.fixture_bytes))assert.equal(hash(await readFile(join(expected,name))),checksum,'independent fixture frozen before run')
  dialog.showOpenDialog=async()=>({canceled:false,filePaths:[join(root,'A')]})
  await import(builtMain());await app.whenReady();await until(()=>BrowserWindow.getAllWindows().length===1,'actual Main window')
  window=BrowserWindow.getAllWindows()[0];await until(()=>!window.webContents.isLoading(),'actual renderer loaded');execute=code=>window.webContents.executeJavaScript(code)
  await execute('window.previewEvents=[];window.myrk.subscribe(e=>window.previewEvents.push(e));window.myrk.command({action:"snapshot"})')
  await until(async()=> (await events()).some(e=>e.event==='snapshot'),'actual service ready')
  const criteria=JSON.parse(await readFile(join(expected,'criteria.json'),'utf8'))
  if(phase==='work'){
    await command({action:'configure',key:'synthetic-preview-local-only',provider_config:{name:'Acceptance preview synthetic',protocol:'deepseek',base_url:'https://api.deepseek.com',model:'deepseek-flash',available_models:['deepseek-flash']}})
    const chats=[]
    for(const name of ['A','B']){
      if(name==='B'){const previous= (await events()).filter(e=>e.event==='conversation').length;await command({action:'new'});await until(async()=> (await events()).filter(e=>e.event==='conversation').length>previous,'new chat acknowledged before selecting its folder')}
      dialog.showOpenDialog=async()=>({canceled:false,filePaths:[join(root,name)]})
      await execute('window.myrk.selectWorkspace()');await command({action:'set_project_trust',root:join(root,name),trusted:true})
      const previous=(await events()).filter(e=>e.event==='finished').length
      await command({action:'send',prompt:'SETUP-CHAT-'+name+' 只生成并明确交付三个合成测试文件，不代表用户接受。',approval_mode:'full_access',scope:{root:join(root,name),allowed_actions:['read','edit','check'],provider:'deepseek',recipients:['deepseek']}})
      await until(async()=> (await events()).filter(e=>e.event==='finished').length>previous,'actual command and explicit delivery for chat '+name)
      const finished=(await events()).filter(e=>e.event==='finished').at(-1);assert.equal(finished.turn.status,'completed',JSON.stringify(finished));assert.equal(finished.saved,true)
      assert.equal(finished.turn.artifacts.length,3,JSON.stringify(finished));chats.push({name,path:finished.path,turn:finished.turn,artifacts:finished.turn.artifacts})
    }
    await writeFile(join(root,'saved-chats.json'),JSON.stringify(chats,null,2));result.setup_http_attempts=await http();assert.equal(result.setup_http_attempts,10)
    await checkSources('delivery setup completed')
    const [a,b]=chats;const report=a.artifacts.find(x=>x.name==='report.txt');const long=a.artifacts.find(x=>x.name==='long.txt');const image=a.artifacts.find(x=>x.name==='picture.png')
    for(const chat of chats)for(const artifact of chat.artifacts){const action=chat.turn.actions.find(x=>x.id===artifact.source_action_id);assert.equal(action.status,'completed');assert.equal(action.effect,'completed');assert.equal(artifact.source_record_path,chat.path);assert.equal(artifact.task_id,chat.turn.task.id);assert.equal(action.file_changes.find(x=>x.id===artifact.file_change_id).after_sha256,artifact.delivered_sha256)}
    await openChat(a)
    const wrong=await execute(`window.myrk.openArtifactPreview(${JSON.stringify({conversationId:b.path,artifactId:b.artifacts[0].id,requestId:randomUUID()})}).then(value=>({value}),error=>({error:String(error)}))`);assert.match(wrong.error,/所属会话/);result.cross_chat_refused=wrong
    const ready=await openApi(a,report);assert.equal(ready.conversationId,a.path);assert.equal(ready.artifactId,report.id);assert.equal(ready.sha256,hash(await readFile(join(expected,'after.txt'))));assert.equal(ready.content.text,await text('after.txt'));assert.equal(ready.content.history.beforeText,await text('before.txt'));assert.equal(ready.content.history.afterText,await text('after.txt'));assert.equal(ready.content.history.actionId,report.source_action_id)
    await click('查看会话成果');await select(report);await until(()=>execute('!!document.querySelector("[aria-label=成果全文]")'),'full text rendered');assert.equal(await execute('document.querySelector("[aria-label=成果全文]").textContent'),await text('after.txt'));result.display_measurements.report_fulltext=await execute('document.querySelector("[aria-label=成果全文]").textContent');await capture('actual-report-fulltext');await checkSources('text read')
    await click('修改对照');assert.equal(await execute('document.querySelector("[aria-label=修改前]").textContent'),await text('before.txt'));assert.equal(await execute('document.querySelector("[aria-label=修改后]").textContent'),await text('after.txt'));result.display_measurements.comparison_before=await execute('document.querySelector("[aria-label=修改前]").textContent');result.display_measurements.comparison_after=await execute('document.querySelector("[aria-label=修改后]").textContent');await capture('actual-independent-before-after');await checkSources('comparison read')
    await closeApi(ready);await checkSources('API snapshot close')
    await select(long);await until(()=>execute('!!document.querySelector("[aria-label=成果全文]")'),'long text rendered');assert.equal(await execute('document.querySelector("[aria-label=成果全文]").textContent'),await text('long.txt'));result.long_segments=180;result.display_measurements.long_fulltext=await execute('document.querySelector("[aria-label=成果全文]").textContent');await capture('actual-long-unicode');await checkSources('all 180 Unicode segments read')
    await select(report);await until(()=>execute('document.querySelector("[aria-label=成果全文]")?.textContent.includes("预算 245")'),'return to delivered report')
    await writeFile(report.path,await readFile(join(expected,'changed.txt')));assert.equal(await execute('document.querySelector("[aria-label=成果全文]").textContent'),await text('after.txt'));result.display_measurements.fixed_snapshot=await execute('document.querySelector("[aria-label=成果全文]").textContent');await capture('fixed-snapshot-after-external-change');await checkSources('fixed snapshot does not change original','changed.txt')
    await click('刷新预览');await until(()=>execute('document.querySelector("[aria-label=成果全文]")?.textContent.includes("9163")'),'explicit refresh current original');assert.equal(await execute('document.querySelector("[aria-label=成果全文]").textContent'),await text('changed.txt'));assert.match(await execute('document.querySelector(".artifact-preview").textContent'),/交付后已有变化/);assert.equal(await execute('[...document.querySelectorAll("button")].some(x=>x.textContent==="修改对照")'),false);result.display_measurements.refresh_fulltext=await execute('document.querySelector("[aria-label=成果全文]").textContent');result.display_measurements.refresh_state=await execute('document.querySelector(".artifact-preview").textContent');await capture('explicit-refresh-changed');await checkSources('explicit refresh read only','changed.txt')
    await writeFile(report.path,await readFile(join(expected,'after.txt')));await click('刷新预览');await until(()=>execute('document.querySelector("[aria-label=成果全文]")?.textContent.includes("预算 245")'),'restore independent delivered fixture')
    await heldOriginal(a,report);await heldOriginal(a,report,'acl')
    await rename(report.path,report.path+'.owned-missing');const missing=await openApi(a,report);assert.equal(missing.status,'missing');await click('刷新预览');await until(()=>execute('document.querySelector(".artifact-preview [role=status]")?.textContent.includes("丢失")'),'real missing status rendered');result.display_measurements.missing_status=await execute('document.querySelector(".artifact-preview [role=status]").textContent');await capture('actual-missing');await rename(report.path+'.owned-missing',report.path);await checkSources('missing file restored after factual state')
    await click('返回聊天');await until(()=>execute('document.querySelector(".delivered-image img")?.complete'),'actual inline delivered image')
    const pixels=await execute(`(()=>{const i=document.querySelector('.delivered-image img');const c=document.createElement('canvas');c.width=i.naturalWidth;c.height=i.naturalHeight;const g=c.getContext('2d');g.drawImage(i,0,0);return {width:c.width,height:c.height,samples:${JSON.stringify(criteria.image.samples)}.map(([x,y])=>[...g.getImageData(x,y,1,1).data])}})()`);assert.equal(pixels.width,480);assert.equal(pixels.height,320);assert.deepEqual(pixels.samples,criteria.image.samples.map(x=>x[2]));result.image_pixels=pixels
    const imageApi=await openApi(a,image);assert.deepEqual(Buffer.from(imageApi.content.base64,'base64'),await readFile(image.path));await closeApi(imageApi);await checkSources('inline image and exact bytes')
    await execute('document.querySelector(".artifact-thumbnail").scrollIntoView({block:"center"})');await pause(70);const top=await execute('document.querySelector(".conversation").scrollTop');await click('放大图片 picture.png');await until(()=>execute('!!document.querySelector("dialog[open] img")?.complete'),'native image modal');await click('放大图片');const actualZoom=await execute('document.querySelector("[aria-label=图片缩放]").textContent');assert.equal(actualZoom,'125%');assert.equal(await execute('document.querySelector("dialog[open] img").style.width'),'600px');await capture('actual-inline-image-zoom125');await checkSources('image zoom')
    window.webContents.sendInputEvent({type:'keyDown',keyCode:'ESC'});window.webContents.sendInputEvent({type:'keyUp',keyCode:'ESC'});await until(()=>execute('!document.querySelector("dialog[open]")'),'native Escape closes image');assert.equal(await execute('document.activeElement.getAttribute("aria-label")'),'放大图片 picture.png');assert.ok(Math.abs(await execute('document.querySelector(".conversation").scrollTop')-top)<=1);await checkSources('image close preserves focus and chat reading position');result.image_focus_scroll_preserved=true;result.image_reading={beforeTop:top,afterTop:await execute('document.querySelector(".conversation").scrollTop'),focus:await execute('document.activeElement.getAttribute("aria-label")'),zoom:actualZoom}
    await openChat(b);assert.equal(await execute('!!document.querySelector("dialog[open]")'),false);assert.equal(await execute('!!document.querySelector(".artifact-preview")'),false);result.chat_reading_isolated=true;const bReady=await openApi(b,b.artifacts.find(x=>x.name==='report.txt'));assert.equal(bReady.content.text,await text('other.txt'));await closeApi(bReady);await checkSources('same-name B identity is separate');await openChat(a)
    result.preview_http_attempts=(await http())-result.setup_http_attempts;assert.equal(result.preview_http_attempts,0)
  }else{
    const chats=JSON.parse(await readFile(join(root,'saved-chats.json'),'utf8'));for(const chat of chats){await openChat(chat);const opened=(await events()).findLast(e=>e.event==='conversation'&&e.path===chat.path);assert.deepEqual(opened.turns.flatMap(t=>t.artifacts??[]).map(a=>a.id),chat.artifacts.map(a=>a.id));assert.equal(opened.running,false);const report=chat.artifacts.find(a=>a.name==='report.txt');await click('查看会话成果');await select(report);await until(()=>execute('!!document.querySelector("[aria-label=成果全文]")'),'saved correct report readable');assert.equal(await execute('document.querySelector("[aria-label=成果全文]").textContent'),await text(chat.name==='A'?'after.txt':'other.txt'));result.display_measurements['reopen_'+chat.name]=await execute('document.querySelector("[aria-label=成果全文]").textContent');await capture('reopened-chat-'+chat.name);await click('返回聊天')}
    assert.equal((await events()).some(e=>e.event==='started'),false);result.preview_http_attempts=await http();assert.equal(result.preview_http_attempts,0);result.same_artifacts_and_chats_reopened=true;await checkSources('full Main restart opens each original without new work')
  }
  result.passed=true
}catch(error){result.error=error.stack;if(execute)result.rendered_text=await execute('document.body.innerText').catch(()=>null)}
  if(execute)await writeFile(join(out,'public-events.json'),JSON.stringify(await events(),null,2));await writeFile(join(out,'observation.json'),JSON.stringify(result,null,2));if(execute)await execute('window.myrk.quit()');else app.quit()
}
void run()
