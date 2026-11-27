import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'
installNormalExit(app)
app.disableHardwareAcceleration()
app.setPath('userData',process.env.MYRK_USER_DATA_DIRECTORY)
const root=process.env.MYRK_ACCEPTANCE_DOCUMENT_ROOT
const request=JSON.parse(await readFile(join(root,'scenario.json'),'utf8'))
const documents=[], responses=[], rendererErrors=[], externalRequests=[], reveals=[], ownedSnapshots=[], conversionWorkers=[]
let window, error, setupRequests, pausedControl
const sha=bytes=>createHash('sha256').update(bytes).digest('hex')
const execute=code=>window.webContents.executeJavaScript(code)
const wait=async(predicate,label,ms=115000)=>{const deadline=Date.now()+ms;while(!await predicate()){assert.ok(Date.now()<deadline,label);await pause(25)}}
const command=value=>execute(`window.myrk.command(${JSON.stringify(value)})`)
const click=label=>execute(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===${JSON.stringify(label)}||b.textContent===${JSON.stringify(label)});if(!b||b.disabled)throw Error('Missing enabled '+${JSON.stringify(label)});b.click()})()`)
const office=()=>JSON.parse(execFileSync('C:/Program Files/PowerShell/7/pwsh.exe',['-NoProfile','-NonInteractive','-Command',"ConvertTo-Json -Compress -InputObject @(Get-Process WINWORD,EXCEL,POWERPNT -ErrorAction SilentlyContinue | Sort-Object Id | Select-Object Id,ProcessName,@{n='creation_file_time';e={$_.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()}})"],{encoding:'utf8',windowsHide:true}))
const previews=async()=> (await readdir(process.env.MYRK_RUNTIME_ROOT).catch(e=>{if(e.code==='ENOENT')return [];throw e})).filter(n=>n.startsWith('document-preview-'))
const nativeHandle=ipcMain.handle.bind(ipcMain)
ipcMain.handle=(channel,listener)=>nativeHandle(channel,async(...args)=>{const value=await listener(...args);if(channel==='myrk:open-artifact-preview')responses.push({request:args[1],response:value});return value})
const body=()=>execute('document.body.innerText')
const pageReady=()=>execute(`!!document.querySelector('.document-preview canvas[data-rendered="true"]')`)
shell.showItemInFolder=path=>{reveals.push(path)}
let sampling=true
const ownershipSampling=(async()=>{while(sampling){for(const name of await previews()){const path=join(process.env.MYRK_RUNTIME_ROOT,name);const snapshot=await readFile(join(path,'owner.json'),'utf8').then(JSON.parse).catch(()=>null);const worker=await readFile(join(path,'conversion/owner.json'),'utf8').then(JSON.parse).catch(()=>null);if(worker&&!conversionWorkers.some(item=>item.worker.pid===worker.pid&&item.worker.process_started_at===worker.process_started_at))conversionWorkers.push({path,snapshot,worker});const owner=await readFile(join(path,'conversion/office-owner.json'),'utf8').then(JSON.parse).catch(()=>null);if(owner&&!ownedSnapshots.some(item=>item.owner.pid===owner.pid&&item.owner.creation_file_time===owner.creation_file_time))ownedSnapshots.push({path,snapshot,owner})}await pause(25)}})()
const normalized=text=>text.replace(/\s/g,'')
const sectionCriteria=JSON.parse(await readFile(request.criteria,'utf8')).expected
async function capture(name){
 const canvas=await execute(`document.querySelector('.document-preview canvas').toDataURL('image/png').split(',')[1]`)
 const bytes=Buffer.from(canvas,'base64');await writeFile(join(root,`${name}-canvas.png`),bytes)
 await writeFile(join(root,`${name}-view.png`),(await window.webContents.capturePage()).toPNG())
 return {canvas_sha256:sha(bytes),canvas_image:`${name}-canvas.png`,view_image:`${name}-view.png`}
}
async function run(){
 dialog.showOpenDialog=async()=>({canceled:false,filePaths:[request.folder]})
 await import(builtMain());await app.whenReady()
 await wait(()=>{window=BrowserWindow.getAllWindows()[0];return window&&!window.webContents.isLoading()},'Real Main window loaded')
 window.webContents.on('console-message',details=>{if(details.level==='error')rendererErrors.push(details.message)})
 window.webContents.session.webRequest.onBeforeRequest((details,callback)=>{if(/^https?:/.test(details.url))externalRequests.push(details.url);callback({cancel:false})})
 await execute('window.previewEvents=[];window.myrk.subscribe(e=>window.previewEvents.push(e));window.myrk.command({action:"snapshot"})')
 await wait(()=>execute('window.previewEvents.some(e=>e.event==="snapshot")'),'Public service snapshot')
 await execute('window.myrk.selectWorkspace()')
 await command({action:'set_project_trust',root:request.folder,trusted:true})
 await command({action:'configure',key:'synthetic-preview-only',provider_config:request.config})
 await wait(()=>execute('window.previewEvents.some(e=>e.event==="configured")'),'Configured finite offline SDK')
 await command({action:'send',prompt:'Create and explicitly deliver the controlled local document copies for read-only preview.',approval_mode:'full_access',scope:request.scope})
 await wait(()=>execute('window.previewEvents.some(e=>e.event==="finished")'),'Real command-created delivery')
 const finished=await execute('window.previewEvents.find(e=>e.event==="finished")')
 await writeFile(join(root,'delivery.json'),JSON.stringify(finished,null,2))
 assert.equal(finished.turn.status,'completed',JSON.stringify(finished))
 assert.equal(finished.turn.artifacts.length,request.outputs.length)
 setupRequests=JSON.parse(await readFile(join(root,'exchange.json'),'utf8')).exchanges.length
 assert.equal(setupRequests,5)
 if(request.mode==='stop'){
  const name='report.docx',source=join(request.folder,name)
  for(const scenario of ['L22','L23']){
   const beforeHash=sha(await readFile(source)),beforeOffice=office(),seen=ownedSnapshots.length
   const row={scenario,name,status:'unknown',before_sha256:beforeHash,office_before:beforeOffice,source_unchanged:false,resources_released:false,foreign_office_unchanged:false,exact_owned_identity_exited:false,actual_cancel_flag_seen:false}
   documents.push(row)
   const started=Date.now(),responseCount=responses.length
   await click('查看会话成果');await click(`预览 ${name}`)
   await wait(()=>ownedSnapshots.length>seen,'Exact owned Office process observed before fault',20000)
   const owned=ownedSnapshots[seen],ownerPath=join(owned.path,'conversion/office-owner.json')
   row.owned_identity=owned.owner
   const control=mode=>JSON.parse(execFileSync('C:/Program Files/PowerShell/7/pwsh.exe',['-NoProfile','-NonInteractive','-File',join(dirname(fileURLToPath(import.meta.url)),'../../../acceptance/materials/document_pause_office.ps1'),'-OwnerPath',ownerPath,'-Mode',mode],{encoding:'utf8',windowsHide:true,env:process.env}))
   row.suspend=control('Suspend');pausedControl={owner:owned.owner,resume:()=>control('Resume')}
   await writeFile(join(root,`${scenario}-pending.png`),(await window.webContents.capturePage()).toPNG())
   if(scenario==='L23'){
    await click('返回聊天')
    await wait(()=>readFile(join(owned.path,'conversion/cancel.flag')).then(()=>true,()=>false),'Actual Main cancellation reached converter')
    row.actual_cancel_flag_seen=true
    row.resume=control('Resume');pausedControl=undefined
   }
   await wait(()=>responses.length>responseCount,'Actual conversion stop response',120000)
   const response=responses.at(-1).response
   pausedControl=undefined;row.elapsed_ms=Date.now()-started;row.public_status=response.status;row.message=response.message
   assert.equal(response.status,'unreadable')
   assert.match(response.message,scenario==='L22'?/超时/:/取消|关闭/)
   if(scenario==='L22'){
    assert.ok(row.elapsed_ms>=90000,'Production 90-second conversion timeout must actually elapse')
    await wait(()=>execute(`document.querySelector('.artifact-preview')?.innerText.includes('超时')`),'Actual timeout UI explanation')
    await writeFile(join(root,`${scenario}-message.png`),(await window.webContents.capturePage()).toPNG())
    await click('返回聊天')
   }
   await wait(()=>execute(`!document.querySelector('.artifact-workspace')`),'Stopped UI preview is closed')
   await wait(async()=>(await previews()).length===0,'Stopped own snapshot resources are absent')
   row.resources_released=true;row.after_sha256=sha(await readFile(source));row.source_unchanged=row.after_sha256===beforeHash
   row.office_after=office();row.foreign_office_unchanged=JSON.stringify(row.office_after)===JSON.stringify(beforeOffice)
   row.exact_owned_identity_exited=!row.office_after.some(item=>item.Id===owned.owner.pid&&item.creation_file_time===owned.owner.creation_file_time)
   assert.ok(row.source_unchanged&&row.foreign_office_unchanged&&row.exact_owned_identity_exited)
   row.status='passed'
   await writeFile(join(root,'partial-observations.json'),JSON.stringify({documents},null,2))
  }
  return
 }
 if(request.mode==='refusal'){
  for(const [,name]of request.outputs){
   const scenario=name.includes('encrypted')?'L18':name.includes('corrupt')?'L19':/disguised|external/.test(name)?'L21':'L20'
   const source=join(request.folder,name),beforeHash=sha(await readFile(source)),beforeOffice=office()
   const row={scenario,name,status:'unknown',before_sha256:beforeHash,office_before:beforeOffice,source_unchanged:false,resources_released:false,foreign_office_unchanged:false,located:false,message:null}
   documents.push(row)
   await click('查看会话成果');await click(`预览 ${name}`)
   await wait(()=>responses.some(r=>r.response.name===name),'Actual refusal public response')
   const response=responses.findLast(r=>r.response.name===name).response
   row.public_status=response.status;row.public_message=response.message??null
   await wait(()=>execute(`(()=>{const p=document.querySelector('.artifact-preview');return !!p&&!p.textContent.includes('正在读取文件快照')&&!p.textContent.includes('正在打开文档页面')&&!!(p.querySelector('[role="alert"]')||p.querySelector('[role="status"]')&&!p.textContent.includes('正在显示页面'))})()`),'Actual UI refusal explanation')
   row.message=await execute(`document.querySelector('.artifact-preview').innerText`)
   assert.match(row.message,/密码|损坏|不支持|宏|外部/)
   await writeFile(join(root,`${name}-refusal.png`),(await window.webContents.capturePage()).toPNG())
   await click('在文件夹中定位成果');await wait(()=>reveals.includes(source),'Public original-file location')
   row.located=true
   await click('返回聊天');await wait(()=>execute(`!document.querySelector('.artifact-workspace')`),'Refused preview closed')
   await wait(async()=>(await previews()).length===0,'Refused snapshot resources closed')
   row.resources_released=true;row.after_sha256=sha(await readFile(source));row.source_unchanged=row.after_sha256===beforeHash
   row.office_after=office();row.foreign_office_unchanged=JSON.stringify(row.office_after)===JSON.stringify(beforeOffice)
   assert.ok(row.source_unchanged&&row.foreign_office_unchanged)
   row.status='passed'
   await writeFile(join(root,'partial-observations.json'),JSON.stringify({documents},null,2))
  }
  return
 }
 for(const [index,[,name]] of request.outputs.entries()){
  const format=name.split('.').at(-1), criteria=sectionCriteria[format]
  const beforeOffice=office(),source=join(request.folder,name), beforeHash=sha(await readFile(source))
  const row={scenario:`L${14+index}`,name,status:'unknown',pages:[],before_sha256:beforeHash,office_before:beforeOffice,original_error:null,source_unchanged:false,resources_released:false,all_pages_observed:false,independent_content_matches:false,zoom_observed:false}
  documents.push(row)
  try{
   await click('查看会话成果');await click(`预览 ${name}`)
   await wait(()=>responses.some(r=>r.response.name===name),'Public preview response')
   const response=responses.findLast(r=>r.response.name===name).response
   row.public_status=response.status
   if(response.status!=='ready')throw Error(response.message)
   row.snapshot_id=response.snapshotId;row.delivered_sha256=response.sha256
   assert.equal(response.sha256,beforeHash)
   assert.equal(response.content.sections.length,criteria.length)
   for(const [sectionIndex,section] of response.content.sections.entries()){
    if(criteria[sectionIndex].label)assert.equal(section.label,criteria[sectionIndex].label)
    const path=join(root,`${format}-section-${sectionIndex+1}.pdf`)
    await writeFile(path,Buffer.from(section.pdfBase64,'base64'))
    const parsed=JSON.parse(execFileSync(process.env.MYRK_PREVIEW_PDF_PYTHON,['-B',join(dirname(fileURLToPath(import.meta.url)),'../../../acceptance/materials/document_pdf_text.py'),path],{encoding:'utf8',windowsHide:true,env:{...process.env,PYTHONIOENCODING:'utf-8'}}))
    assert.equal(parsed.sha256,sha(Buffer.from(section.pdfBase64,'base64')))
    assert.equal(parsed.pages.length,criteria[sectionIndex].pages.length,'Every expected source page must be present')
    if(sectionIndex){const label=format==='xlsx'?'工作表':'幻灯片';await execute(`(()=>{const s=document.querySelector('[aria-label="${label}"]');s.selectedIndex=${sectionIndex};s.dispatchEvent(new Event('change',{bubbles:true}))})()`)}
    for(const [pageIndex,text]of parsed.pages.entries()){
     if(pageIndex)await click('下一页')
     await wait(async()=>await pageReady()&&await execute(`document.querySelector('.document-preview canvas').getAttribute('aria-label')==='第 ${pageIndex+1} 页'`),'Actual expected page rendered')
     for(const marker of criteria[sectionIndex].pages[pageIndex])assert.ok(normalized(text).includes(normalized(marker)),`${name} section${sectionIndex+1} page${pageIndex+1} missing source marker ${marker}`)
     row.pages.push({section:sectionIndex+1,label:section.label,page:pageIndex+1,text,pdf_sha256:parsed.sha256,...await capture(`${format}-${sectionIndex+1}-${pageIndex+1}`)})
    }
   }
   const width=await execute('document.querySelector(".document-preview canvas").width')
   await click('放大');await wait(async()=>await pageReady()&&await execute(`document.querySelector('.document-preview canvas').width>${width}`),'Real page zoom rendered')
   row.zoom_observed=true
   await click('缩小');await wait(pageReady,'Zoom out rendered')
   if(format==='pdf'||format==='docx'){await click('上一页');await wait(async()=>await pageReady()&&await execute('document.querySelector(".document-preview canvas").getAttribute("aria-label")==="第 4 页"'),'Previous page navigation')}
   assert.equal(new Set(row.pages.map(item=>item.canvas_sha256)).size,row.pages.length,'Each expected source page/section must have its own actual rendered content')
   row.all_pages_observed=true;row.independent_content_matches=true;row.status='passed'
  }catch(failure){row.status='failed';row.original_error=String(failure);row.body=await body();await writeFile(join(root,`${format}-failure.png`),(await window.webContents.capturePage()).toPNG())}
  finally{
   if(await execute(`!!document.querySelector('.artifact-workspace')`))await click('返回聊天')
   await wait(()=>execute(`!document.querySelector('.artifact-workspace')`),'Previous preview unmounted')
   await wait(async()=>(await previews()).length===0,'Real snapshot directories closed')
   row.resources_released=(await previews()).length===0
   row.after_sha256=sha(await readFile(source));row.source_unchanged=beforeHash===row.after_sha256
   await writeFile(join(root,'partial-observations.json'),JSON.stringify({documents},null,2));row.office_after=office();row.foreign_office_unchanged=JSON.stringify(row.office_after)===JSON.stringify(beforeOffice)
   row.owned_identities=ownedSnapshots.filter(item=>item.snapshot?.snapshot_id===row.snapshot_id).map(item=>item.owner)
   row.exact_owned_identity_exited=row.owned_identities.every(owner=>!row.office_after.some(item=>item.Id===owner.pid&&item.creation_file_time===owner.creation_file_time))
   if(format!=='pdf'&&row.status==='passed'&&row.owned_identities.length!==1){row.status='unknown';row.original_error='Exact owned Office identity was not observed'}
   if(!row.source_unchanged||!row.foreign_office_unchanged){row.status='failed';row.original_error??='Read-only/process contract failed'}
  }
 }
}
run().catch(failure=>{error=String(failure);if(pausedControl&&office().some(item=>item.Id===pausedControl.owner.pid&&item.creation_file_time===pausedControl.owner.creation_file_time)){try{pausedControl.resume()}catch(cleanupError){error+='; exact owned resume failed: '+String(cleanupError)}}}).finally(async()=>{
 sampling=false;await ownershipSampling
 const identities=conversionWorkers.length?JSON.parse(execFileSync('C:/Program Files/PowerShell/7/pwsh.exe',['-NoProfile','-NonInteractive','-Command',`ConvertTo-Json -Compress -InputObject @(Get-Process -Id ${conversionWorkers.map(item=>item.worker.pid).join(',')} -ErrorAction SilentlyContinue | Select-Object Id,@{n='created_at';e={$_.StartTime.ToUniversalTime().ToString('o')}})`],{encoding:'utf8',windowsHide:true})):[]
 for(const item of conversionWorkers)item.exited=!identities.some(actual=>actual.Id===item.worker.pid&&actual.created_at===item.worker.process_started_at)
 const owned_process_closure=conversionWorkers.length===({content:3,refusal:4,stop:2}[request.mode])&&conversionWorkers.every(item=>item.exited)
 const after=JSON.parse(await readFile(join(root,'exchange.json'),'utf8').catch(()=>'{"exchanges":[]}')).exchanges.length
 await writeFile(join(root,'observations.json'),JSON.stringify({documents,error,setup_http_attempts:setupRequests,preview_http_attempts:setupRequests===undefined?null:after-setupRequests,rendererErrors,externalRequests,reveals,ownedSnapshots,conversionWorkers,owned_process_closure},null,2))
 if(window&&!window.isDestroyed())await execute('window.myrk.quit()');else app.quit()
})
