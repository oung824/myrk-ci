"""Local PDF/Office contracts at the real public Main, service and file boundary."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
import shutil
import subprocess
from zipfile import ZipFile
from uuid import uuid4
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry
from tests.support.agent_acceptance.formal import build, environment, provider_config, bindings as desktop_bindings
from tests.support.agent_acceptance.preparation import SOURCE

FIXTURES = SOURCE / 'desktop/scripts/office-preview-fixtures'
CRITERIA = SOURCE/'tests/acceptance/materials/document-preview/criteria.json'


async def content(context: CaseContext) -> CaseOutcome:
    criteria=json.loads(CRITERIA.read_bytes())
    for name,expected in criteria['sources'].items():
        if sha256((FIXTURES/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Independent preview source changed before execution: '+name)
    folder=context.root/'materials'
    folder.mkdir()
    outputs=[]
    selected = [('pdf-layout.pdf','report.pdf'),('word-layout.docx','report.docx'),('workbook-layout.xlsx','report.xlsx'),('slides-layout.pptx','report.pptx')]
    mode = 'refusal' if context.case_id.endswith('refusal') else 'stop' if context.case_id.endswith('stop') else 'content'
    if mode == 'stop':
        selected=[('word-layout.docx','report.docx')]
    external_hits: list[dict[str,str]] = []
    receiver = None
    if mode == 'refusal':
        generated = context.root/'controlled-materials'
        generated.mkdir()
        pdf_python = str(Path(shutil.which('pdftoppm') or '').parents[4]/'python/python.exe')
        material_result = await asyncio.to_thread(subprocess.run, ['C:/Program Files/PowerShell/7/pwsh.exe','-NoProfile','-NonInteractive','-File',str(SOURCE/'tests/acceptance/materials/document_legacy.ps1'),'-Directory',str(generated)],env=environment(context.root/'material-environment'),capture_output=True)
        (context.root/'material.stdout').write_bytes(material_result.stdout)
        (context.root/'material.stderr').write_bytes(material_result.stderr)
        if material_result.returncode:
            return CaseOutcome('inconclusive',{'material_setup_error':material_result.stderr.decode('utf-8',errors='replace')},{'material_setup_error':context.root/'material.stderr'})
        selected=[('password-protected.docx','encrypted.docx'),('corrupt-document.docx','corrupt.docx')]
        for name in ['legacy.doc','legacy.xls','legacy.ppt','macro-format.docm']:
            shutil.copyfile(generated/name,folder/name)
            selected.append((name,'delivered-'+name))
        with ZipFile(generated/'macro-format.docm') as archive:
            assert b'macroEnabled' in archive.read('[Content_Types].xml')
        shutil.copyfile(generated/'macro-format.docm',folder/'disguised.docx')
        selected.append(('disguised.docx','delivered-disguised.docx'))
        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received = await reader.read(8192)
            external_hits.append({'received_hex':received.hex()})
            writer.close()
            await writer.wait_closed()
        receiver = await asyncio.start_server(receive,'127.0.0.1',0)
        external_url = f'http://127.0.0.1:{receiver.sockets[0].getsockname()[1]}/never-fetch-preview'
        (context.root/'external-receiver.json').write_text(json.dumps({'url':external_url,'purpose':'Controlled negative material; no contact is authorized.'}),encoding='utf-8')
        with ZipFile(FIXTURES/'word-layout.docx') as archive, ZipFile(folder/'external.docx','w') as target_archive:
            for item in archive.infolist():
                data=archive.read(item.filename)
                if item.filename=='word/_rels/document.xml.rels':
                    data=data.replace(b'</Relationships>',f'<Relationship Id="rId_external_control" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{external_url}" TargetMode="Exter&#110;al"/></Relationships>'.encode())
                target_archive.writestr(item,data)
        selected.append(('external.docx','delivered-external.docx'))
        script = "from pypdf import PdfReader,PdfWriter;import sys;from pathlib import Path;p=Path(sys.argv[1]);r=PdfReader(sys.argv[2]);w=PdfWriter();w.add_page(r.pages[0]);w.encrypt('public-synthetic-preview-20261116');w.write(str(p/'encrypted.pdf'));(p/'corrupt.pdf').write_bytes(b'%PDF-1.7\\nactual-corrupt-no-cross-reference')"
        await asyncio.to_thread(subprocess.run,[pdf_python,'-B','-c',script,str(folder),str(FIXTURES/'pdf-layout.pdf')],check=True,env=environment(context.root/'pdf-material-environment'),capture_output=True)
        selected.extend([('encrypted.pdf','delivered-encrypted.pdf'),('corrupt.pdf','delivered-corrupt.pdf')])
    for source,target in selected:
        if (FIXTURES/source).is_file():
            shutil.copyfile(FIXTURES/source,folder/source)
        outputs.append([source,target])
    scope={'root':str(folder),'allowed_actions':['read','edit','check'],'provider':'deepseek','recipients':['deepseek']}
    request={'folder':str(folder),'outputs':outputs,'scope':scope,'mode':mode,'run_id':context.run_id,'case_id':context.case_id,'criteria':str(CRITERIA),'config':provider_config('deepseek').model_dump(mode='json')}
    (context.root/'scenario.json').write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
    build_root=Path(os.environ.get('MYRK_CHECK_RUN_DIRECTORY',str(context.root)))/('preview-'+uuid4().hex[:8])
    compiled_sources=await asyncio.to_thread(build,build_root)
    (context.root/'compiled-sources.json').write_text(json.dumps(compiled_sources,indent=2)+'\n',encoding='utf-8')
    launch={'root':str(context.root),'driver':str(SOURCE/'tests/support/agent_acceptance/desktop/document-preview.mjs'),'timeout':450000,
        'environment':{'MYRK_TEST_RUN_DIRECTORY':str(build_root),'MYRK_USER_DATA_DIRECTORY':str(context.root/'profile'),
            'MYRK_DATA_DIRECTORY':str(context.root/'records'),'MYRK_ACCEPTANCE_DOCUMENT_ROOT':str(context.root),
            'MYRK_SERVICE_MODULE':'tests.support.agent_acceptance.document_preview_service','MYRK_RUNTIME_ROOT':str(context.root/'runtime'),'MYRK_PREVIEW_PDF_PYTHON':str(Path(shutil.which('pdftoppm') or '').parents[4]/'python/python.exe')}}
    path=context.root/'launch.json'
    path.write_text(json.dumps(launch),encoding='utf-8')
    process=await asyncio.create_subprocess_exec('node',str(SOURCE/'tests/support/agent_acceptance/desktop/document-preview-runner.mjs'),str(path),cwd=SOURCE,env=environment(context.root/'environment'),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await process.communicate()
    (context.root/'runner.stdout').write_bytes(stdout)
    (context.root/'runner.stderr').write_bytes(stderr)
    if receiver is not None:
        receiver.close()
        await receiver.wait_closed()
        (context.root/'external-effects.json').write_text(json.dumps({'requests':external_hits,'listener_closed':True}),encoding='utf-8')
    processes=json.loads((context.root/'processes.json').read_bytes())
    result: dict[str, Any]=json.loads((context.root/'observations.json').read_bytes()) if (context.root/'observations.json').exists() else {'documents':[],'error':'Driver did not produce observations.'}
    exchange=json.loads((context.root/'exchange.json').read_bytes()) if (context.root/'exchange.json').exists() else {'exchanges':[]}
    facts={**result,'compiled_sources':compiled_sources,'processes':processes,'setup_http_attempts':len(exchange['exchanges']),'real_http_attempts':0,'normal_profile_accesses':0,'visual_review':'pending independent image inspection','user_acceptance':'pending','external_access_attempts':len(external_hits)}
    expected_rows={'content':4,'refusal':10,'stop':2}[mode]
    passed=len(result.get('documents',[]))==expected_rows and all(row['status']=='passed' for row in result['documents']) and not external_hits
    tool=process.returncode==0 and processes['complete'] and not result.get('error') and result.get('owned_process_closure') is True
    if processes['complete']:
        shutil.rmtree(build_root)
        shutil.rmtree(context.root/'profile',ignore_errors=False)
        shutil.rmtree(context.root/'environment',ignore_errors=False)
        for extra_root in ['material-environment','pdf-material-environment']:
            if (context.root/extra_root).exists():
                shutil.rmtree(context.root/extra_root)
    evidence={str(path.relative_to(context.root)):path for path in context.root.rglob('*') if path.is_file() and not path.is_relative_to(context.root/'profile') and not path.is_relative_to(context.root/'environment')}
    rows=result.get('documents',[])
    readonly=bool(rows) and all(row.get('source_unchanged') is True and row.get('foreign_office_unchanged') is True for row in rows)
    contracts={'K1':'failed' if external_hits or any(row.get('after_sha256') and row.get('after_sha256')!=row.get('before_sha256') for row in rows) else 'passed' if readonly else 'unknown',
        'K2':'not_applicable','K3':('passed' if passed and tool else 'failed' if tool else 'unknown') if mode=='stop' else 'not_applicable','K4':'passed' if tool else 'unknown','K5':'passed' if passed and tool else 'failed' if tool else 'unknown'}
    facts['critical_contract_applicability']={'K1':'Actual authorized creation and read-only preview sources, converter external effects and foreign processes are checked.',
        'K2':'Controlled creation uses saved full-access authorization; no separate approval/reviewer decision occurs.',
        'K3':'Actual timeout and UI cancel include precise owned Office PID/creation time and observed process/directory exit.' if mode=='stop' else 'This content/refusal case completes conversions before closing; timeout/cancel is a separate actual local case.',
        'K4':'Real delivered identities, source hashes and actual UI failure messages; user acceptance remains pending.',
        'K5':'Every required local format content/refusal row is checked separately.'}
    return CaseOutcome('passed' if passed else 'failed',facts,evidence,tool_status='passed' if tool else 'failed',critical_contracts=contracts)


def register(registry: CaseRegistry) -> None:
    for name in ['preview.document-content','preview.document-refusal','preview.document-stop']:
        registry.register(AcceptanceCase(name,'document-preview',('deepseek',),content,
        owner_ticket='19',group='document-preview',observation_layer='electron-main-service-sdk',
        bindings=(*desktop_bindings(),CRITERIA,Path(__file__).with_name('document_preview_service.py'),Path(__file__).parent/'desktop/document-preview.mjs',Path(__file__).parent/'desktop/document-preview-runner.mjs',SOURCE/'tests/acceptance/materials/document_pdf_text.py',SOURCE/'tests/acceptance/materials/document_legacy.ps1',SOURCE/'tests/acceptance/materials/document_pause_office.ps1',*FIXTURES.iterdir()),
        applicability='Local L14-L23 preview effects run once. Native SDK replay only creates real delivered files; all later preview actions send zero model HTTP.'))
