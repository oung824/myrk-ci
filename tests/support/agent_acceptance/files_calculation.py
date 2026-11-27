"""Version-bound local facts; no generated answer substitutes for actual effects."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Literal
from collections.abc import Callable

from pydantic_ai import CancellationToken

from myrk.checks import CheckReceipt
from myrk.conversation import CheckCommand, Conversation, Task, TaskScope
from tests.support.agent_acceptance.actions import record_outcome
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, SharedControlError
from tests.support.agent_acceptance.replay import Reply

MATERIALS = Path(__file__).resolve().parents[2] / 'acceptance/materials/files-calculation'
CRITERIA = 'files-calculation-v1'


def receipt_facts(action: dict[str, Any], source: Path, *, expected: bytes, argv: list[str]) -> dict[str, Any]:
    """Verify explicit file-argument observations, execution and independent output."""
    result: dict[str, Any] = {'verified':False, 'receipt_present': False, 'command_matches': False, 'input_version_matches': False,
        'output_matches': False, 'execution_succeeded': False, 'check_report_matches': False, 'checker_version_matches':False}
    path = action.get('receipt_path')
    if not path or not Path(path).is_file():
        return result
    receipt = CheckReceipt.model_validate_json(Path(path).read_bytes())
    result['receipt_present'] = True
    if receipt.argv is None or receipt.cwd is None or receipt.started_at is None or receipt.completed_at is None:
        return result
    result['command_matches'] = (receipt.action_id == action['id'] and receipt.argv == argv
        and receipt.cwd == str(source.parent) and action['cwd'] == receipt.cwd)
    original = receipt.input_files_before.get(str(source))
    final = receipt.input_files_after.get(str(source))
    digest = sha256(expected).hexdigest()
    result['input_version_matches'] = (original is not None and final is not None
        and original.sha256 == final.sha256 == digest and original.size_bytes == final.size_bytes == len(expected)
        and source.is_file() and source.read_bytes() == expected)
    result['output_matches'] = all(path is not None and getattr(receipt, stream + '_sha256') == sha256(Path(path).read_bytes() if Path(path).is_file() else b'').hexdigest()
        for stream in ('stdout', 'stderr') for path in [action[stream + '_path']])
    checker=Path(argv[2])
    frozen_checker=MATERIALS / ('check_version.py' if checker.name=='check.py' else checker.name)
    checked_before=receipt.input_files_before.get(str(checker))
    checked_after=receipt.input_files_after.get(str(checker))
    frozen_bytes=frozen_checker.read_bytes()
    result['checker_version_matches']=(checked_before is not None and checked_after is not None
        and checked_before.sha256==checked_after.sha256==sha256(frozen_bytes).hexdigest()
        and checked_before.size_bytes==checked_after.size_bytes==len(frozen_bytes)
        and checker.is_file() and checker.read_bytes()==frozen_bytes)
    result['execution_succeeded'] = (action['status'] == 'completed' and action['exit_code'] == receipt.exit_code == 0
        and action['cleanup_complete'] is True and receipt.started_at is not None and receipt.completed_at is not None
        and receipt.started_at <= receipt.completed_at)
    try:
        report = json.loads(action['stdout'])
    except (ValueError, TypeError):
        report = None
    result['check_report_matches'] = report == {'checked_sha256': digest, 'size_bytes': len(expected), 'result': 'passed'}
    result['receipt'] = receipt.model_dump(mode='json')
    result['verified'] = all(result[name] for name in ('receipt_present', 'command_matches', 'input_version_matches', 'output_matches', 'execution_succeeded', 'check_report_matches', 'checker_version_matches'))
    return result


def finish(context: CaseContext, outcome: CaseOutcome, domain: dict[str, Any], *, critical_false_claim: bool = False) -> CaseOutcome:
    outcome.facts['domain'] = domain
    outcome.facts['criteria_version'] = CRITERIA
    outcome.critical_contracts['K4'] = 'failed' if critical_false_claim else 'passed' if domain.get('claim') is not None or domain.get('no_final_claim') is True else 'unknown'
    outcome.evidence['domain_standard']=MATERIALS/'criteria.json'
    outcome.deterministic_status = 'passed' if outcome.product_status == 'passed' else 'failed'
    outcome.target_status = 'passed' if outcome.product_status == 'passed' else 'failed'
    review = context.root / 'files-calculation-domain-review.json'
    review.write_text(json.dumps({'criteria_version': CRITERIA, 'standards': str(MATERIALS / 'criteria.json'),
        'provenance': 'real local effects with fixed terminal model replies; semantics not measured',
        'execution': {'run_id': context.run_id, 'case_id': context.case_id, 'protocol': context.protocol},
        'critical_contracts': outcome.critical_contracts, 'goal': outcome.product_status,
        'dimensions': domain, 'originals': {name: {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
            for name, path in outcome.evidence.items() if path.is_file()}}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    outcome.evidence['domain_review'] = review
    return outcome


async def checked(context: CaseContext) -> CaseOutcome:
    folder = context.root / 'materials'
    folder.mkdir()
    source = folder / 'source.txt'
    original = b'independently-fixed-source-6193\r\n'
    source.write_bytes(original)
    script = folder / 'check.py'
    script.write_bytes((MATERIALS / 'check_version.py').read_bytes())
    argv = [sys.executable, '-B', str(script), str(source), sha256(original).hexdigest()]
    saved_receipt = context.root / 'original-receipt.json'
    def after_check(_payload: dict[str, Any]) -> dict[str, Any]:
        if conversation.turns[-1].actions and context.case_id.rsplit('.',1)[-1] in {'check-wrong-input', 'check-wrong-command', 'check-wrong-output', 'check-wrong-script', 'check-missing-receipt'}:
            recorded_receipt=conversation.turns[-1].actions[0].receipt_path
            if recorded_receipt is None:
                raise SharedControlError('The actual check did not record a receipt path; cannot perturb fictional evidence.')
            receipt_path = Path(recorded_receipt)
            saved_receipt.write_bytes(receipt_path.read_bytes())
            if context.case_id.endswith('missing-receipt'):
                receipt_path.unlink()
            else:
                value = json.loads(receipt_path.read_bytes())
                if context.case_id.endswith('wrong-input'):
                    value['input_files_before'][str(source)]['sha256'] = '0'*64
                elif context.case_id.endswith('wrong-script'):
                    value['input_files_before'][str(script)]['sha256']='0'*64
                elif context.case_id.endswith('wrong-command'):
                    value['argv'] = [sys.executable, '-c', 'pass']
                else:
                    value['stdout_sha256'] = '0'*64
                receipt_path.write_text(json.dumps(value), encoding='utf-8')
        if context.case_id.endswith('changed-after'):
            source.write_bytes(b'changed-after-the-check-7342\r\n')
        return {'path': 'source.txt'}
    session = context.session([*([Reply(tool='run_check', arguments={'name': 'Exact version'})] if not context.case_id.endswith('not-executed') else []),
        *([Reply(tool='read_text', arguments=after_check)] if not context.case_id.endswith('not-executed') else []), Reply(text=json.dumps({'goal': 'complete', 'checked_current_version': True}))])
    conversation = Conversation(context.root / 'conversation.json')
    recipient: Literal['deepseek','openai-compatible','google'] = 'google' if context.protocol == 'google' else 'openai-compatible' if context.protocol == 'openai-chat' else 'deepseek'
    task = Task(goal='Run the registered exact-version check before readback; report actual current verification.', provider_config=session.config,
        scope=TaskScope(root=str(folder), allowed_actions=['read','check'], provider=recipient, recipients=[recipient],
            checks=[CheckCommand(name='Exact version', argv=argv)]))
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
    domain = receipt_facts(turn.actions[0].model_dump(mode='json'), source, expected=original, argv=argv) if turn.actions and turn.actions[0].kind == 'check' else {'verified': False, 'receipt_present': False, 'input_version_matches': False, 'execution_succeeded': False}
    domain['claim']=json.loads(turn.answer)
    outcome = record_outcome(context, session, conversation, task, turn, bool(domain.get('verified')),
        evidence={'source': source, 'check_script': script, **({'original_receipt': saved_receipt} if saved_receipt.is_file() else {})})
    return finish(context, outcome, domain, critical_false_claim=not domain.get('verified', False))


def register(registry: CaseRegistry) -> None:
    for name in ('check-before-command','check-after-command-false'):
        registry.register(AcceptanceCase('files.'+name, 'process-effects', ('deepseek','openai-chat','google'), ordered, owner_ticket='06', bindings=tuple(MATERIALS.iterdir())))
    for name in ('calculation-invalid-recovery','calculation-wrong-answer'):
        registry.register(AcceptanceCase('files.'+name, 'calculation', ('deepseek','openai-chat','google'), calculation, owner_ticket='06', bindings=tuple(MATERIALS.iterdir())))
    for name in ('command-failure','command-timeout','command-stop','command-recovery'):
        registry.register(AcceptanceCase('files.'+name, 'process-effects', ('deepseek','openai-chat','google'), command_negative, owner_ticket='06', bindings=tuple(MATERIALS.iterdir())))
    for name in ('file-no-change','file-no-change-false','file-wrong-target','file-version-changed'):
        registry.register(AcceptanceCase('files.'+name, 'file-effects', ('deepseek','openai-chat','google'), file_negative, owner_ticket='06', bindings=tuple(MATERIALS.iterdir())))
    for name in ('check-version', 'check-changed-after', 'check-wrong-input', 'check-wrong-command', 'check-wrong-output', 'check-wrong-script', 'check-not-executed', 'check-missing-receipt'):
        registry.register(AcceptanceCase('files.'+name, 'process-effects', ('deepseek','openai-chat','google'), checked,
            owner_ticket='06', bindings=tuple(MATERIALS.iterdir())))


def command_facts(folder: Path, actions: list[dict[str, Any]]) -> dict[str, Any]:
    commands = [a for a in actions if a['kind'] == 'command']
    checks = [a for a in actions if a['kind'] == 'check']
    expected = b'actual-command-product-741\r\n'
    argv = [sys.executable, '-B', str(folder / 'check_version.py'), str(folder / 'product.txt'), sha256(expected).hexdigest()]
    checked = receipt_facts(checks[0], folder / 'product.txt', expected=expected, argv=argv) if len(checks) == 1 else {'verified': False}
    effect = (len(commands) == 1 and commands[0]['status'] == 'completed' and commands[0]['exit_code'] == 0
        and commands[0]['stdout'].strip() == 'actual-command-stdout-741' and commands[0]['stderr'].strip() == 'actual-command-stderr-741'
        and commands[0]['prepared_parameters'].get('program') == sys.executable
        and commands[0]['prepared_parameters'].get('arguments') == ['-B', str(folder / 'command.py'), str(folder / 'product.txt')]
        and (folder / 'product.txt').read_bytes() == expected)
    ordered = bool(commands and checks and commands[0]['finished_at'] <= checks[0]['started_at']
        and commands[0]['prepared_request'] < checks[0]['prepared_request'])
    return {'command_effect_verified': effect, 'check_after_command': ordered, 'check': checked,
        'passed': bool(effect and ordered and checked['verified'])}


def calculation_facts(folder: Path, actions: list[dict[str, Any]], sdk: list[dict[str, Any]]) -> dict[str, Any]:
    expected = b'item,amount\napple,0.10\npear,0.20\nrefund,-0.05\n'
    calculates = [a for a in actions if a['kind'] == 'calculate']
    checks = [a for a in actions if a['kind'] == 'check']
    expected_output = '按amount列逐行相加，共3行，合计：0.25。\n[amounts.csv:2] amount=0.10\n[amounts.csv:3] amount=0.20\n[amounts.csv:4] amount=-0.05'
    outputs = [part for item in sdk for message in item['messages'] for part in message['parts'] if part['part_kind'] == 'tool-return']
    descriptions = [part['content'] for part in outputs if part['tool_name'] == 'describe_extension' and isinstance(part['content'], dict)]
    searched = any(part['tool_name']=='search_extensions' and isinstance(part['content'],dict) and part['content'].get('status')=='ok'
        and any(tool.get('name')=='calculate_table' for tool in part['content'].get('tools',[])) for part in outputs)
    discovered = bool(descriptions and calculates and descriptions[-1].get('name') == 'calculate_table'
        and descriptions[-1].get('schema_version') == calculates[0]['tool_schema_version']
        and set(descriptions[-1]['schema']['required']) == {'path','column'}
        and calculates[0]['tool_name'] == 'calculate_table' and calculates[0]['tool_source'] == 'extension'
        and calculates[0]['tool_arguments'] == {'path':'amounts.csv','column':'amount'})
    checked = receipt_facts(checks[0], folder / 'amounts.csv', expected=expected,
        argv=[sys.executable, '-B', str(folder / 'check_cents.py'), str(folder / 'amounts.csv')]) if len(checks) == 1 else {'verified': False}
    exact = len(calculates) == 1 and calculates[0]['output'] == expected_output
    ordered = bool(calculates and checks and calculates[0]['finished_at'] <= checks[0]['started_at'])
    return {'independent_decimal_expected':'0.25', 'gold_derivation':'10 + 20 - 5 = 25 integer cents',
        'exact_tool_output':exact, 'actual_discovery_and_current_version': searched and discovered,
        'check':checked, 'calculation_before_check': ordered,
        'passed':bool(exact and searched and discovered and ordered and checked['verified'])}


async def file_negative(context: CaseContext) -> CaseOutcome:
    folder = context.root / 'materials'
    folder.mkdir()
    source = folder / 'source.txt'
    original = b'existing-original\r\n'
    source.write_bytes(original)
    before = context.root / 'original.bin'
    before.write_bytes(original)
    unchanged = 'no-change' in context.case_id
    changed = context.case_id.endswith('version-changed')
    def edit_arguments(_payload: dict[str, Any]) -> dict[str, Any]:
        if changed:
            source.write_bytes(b'external-current-version\r\n')
        return {'path': 'source.txt', **({'content': original.decode('utf-8')} if unchanged else
            {'old_text':'existing-original' if changed else 'missing-target-text', 'new_text':'requested-revision'})}
    false_claim = context.case_id.endswith('false')
    session = context.session([Reply(tool='read_text', arguments={'path':'source.txt'}),
        Reply(tool='write_text' if unchanged else 'edit_text', arguments=edit_arguments),
        Reply(text=json.dumps({'goal':'complete' if false_claim else 'failed', 'new_artifact':false_claim, 'delivered':false_claim}))])
    recipient: Literal['deepseek','openai-compatible','google'] = 'google' if context.protocol == 'google' else 'openai-compatible' if context.protocol == 'openai-chat' else 'deepseek'
    conversation = Conversation(context.root / 'conversation.json')
    task = Task(goal='Change the reviewed original to requested-revision and deliver only a real new change; preserve stale versions.',
        provider_config=session.config, scope=TaskScope(root=str(folder), allowed_actions=['read','edit'], provider=recipient, recipients=[recipient]))
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
    changes = [change.model_dump(mode='json') for action in turn.actions for change in action.file_changes]
    expected = b'external-current-version\r\n' if changed else original
    domain = {'new_file_changes':changes, 'original_sha256':sha256(original).hexdigest(),
        'current_sha256':sha256(source.read_bytes()).hexdigest(), 'preserved_expected_version':source.read_bytes()==expected,
        'action_errors':[a.error for a in turn.actions if a.error], 'actual_artifacts':[a.model_dump(mode='json') for a in turn.artifacts],
        'claim':json.loads(turn.answer)}
    outcome = record_outcome(context, session, conversation, task, turn, False, evidence={'source':source,'original':before})
    return finish(context, outcome, domain, critical_false_claim=false_claim and not changes)


async def command_negative(context: CaseContext) -> CaseOutcome:
    folder = context.root / 'materials'
    folder.mkdir()
    for name in ('partial_command.py','command.py','check_version.py'):
        (folder / name).write_bytes((MATERIALS / name).read_bytes())
    stopped = context.case_id.endswith('stop')
    timeout = context.case_id.endswith('timeout')
    recovered = context.case_id.endswith('recovery')
    partial = folder / 'partial.txt'
    product = folder / 'product.txt'
    expected = b'actual-command-product-741\r\n'
    check_argv = [sys.executable,'-B',str(folder / 'check_version.py'),str(product),sha256(expected).hexdigest()]
    first = Reply(tool='run_command',arguments={'program':sys.executable,
        'arguments':['-B',str(folder / 'partial_command.py'),str(partial),'wait' if stopped or timeout else 'fail'],
        'timeout_seconds':1 if timeout else 30})
    recovery = [Reply(tool='run_command',arguments={'program':sys.executable,'arguments':['-B',str(folder / 'command.py'),str(product)]}),
        Reply(tool='run_check',arguments={'name':'Recovered product'})] if recovered else []
    session = context.session([first,*recovery,Reply(text=json.dumps({'goal':'complete' if recovered else 'failed',
        'partial_effects':['partial.txt'],'check_passed':recovered,'recovery_used':recovered}))])
    recipient: Literal['deepseek','openai-compatible','google'] = 'google' if context.protocol == 'google' else 'openai-compatible' if context.protocol == 'openai-chat' else 'deepseek'
    conversation = Conversation(context.root / 'conversation.json')
    task = Task(goal='Create the requested product, then verify it. If partial_command.py fails, keep its partial.txt and original error; command.py is the authorized recovery. Report any incomplete goal honestly.',
        provider_config=session.config,scope=TaskScope(root=str(folder),allowed_actions=['read','edit','check'],provider=recipient,recipients=[recipient],
            checks=[CheckCommand(name='Recovered product',argv=check_argv)]))
    token = CancellationToken()
    stop_count = None
    async def stop_on_partial() -> None:
        nonlocal stop_count
        while not partial.is_file():
            await asyncio.sleep(.01)
        stop_count = len(session.exchanges)
        token.cancel()
    async with session.model() as model:
        stopper = asyncio.create_task(stop_on_partial()) if stopped else None
        try:
            turn = await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model,cancellation_token=token)
        finally:
            if stopper is not None:
                stopper.cancel()
                await asyncio.gather(stopper,return_exceptions=True)
    first_action = turn.actions[0]
    check = receipt_facts(turn.actions[-1].model_dump(mode='json'),product,expected=expected,argv=check_argv) if recovered and turn.actions[-1].kind == 'check' else {'verified':False}
    actual_order = all(a.finished_at is not None and a.finished_at <= b.started_at for a,b in zip(turn.actions,turn.actions[1:]))
    domain = {'partial_effect_verified':partial.is_file() and partial.read_bytes()==b'partial-before-error-4713\r\n',
        'original_error':first_action.error or turn.error, 'original_action_status':first_action.status,
        'original_stdout':first_action.stdout,'original_stderr':first_action.stderr,
        'recovery_check':check,'actual_order_preserved':actual_order,
        'alternative_basis':'Original task explicitly permits command.py after failure; original partial bytes and error retained.',
        'stop_requested':stopped,'no_new_request_after_stop':stopped and stop_count==len(session.exchanges),
        'claim':json.loads(turn.answer) if turn.answer else None,'no_final_claim':not turn.answer}
    passed = bool(recovered and domain['partial_effect_verified'] and first_action.status=='failed' and actual_order and check['verified'])
    outcome = record_outcome(context,session,conversation,task,turn,passed,
        evidence={'partial_effect':partial,**({'recovered_product':product} if product.is_file() else {}),
            'partial_command':folder / 'partial_command.py','recovery_command':folder / 'command.py'})
    return finish(context,outcome,domain)


async def calculation(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance.delegation import returned_object

    folder=context.root / 'materials'
    folder.mkdir()
    source=folder / 'amounts.csv'
    source.write_bytes(b'item,amount\napple,0.10\npear,0.20\nrefund,-0.05\n')
    bad=folder / 'bad.csv'
    bad.write_bytes(b'item,amount\ninvalid,not-a-decimal\n')
    script=folder / 'check_cents.py'
    script.write_bytes((MATERIALS / 'check_cents.py').read_bytes())
    recovered=context.case_id.endswith('recovery')
    def call_for(path: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def arguments(payload: dict[str, Any]) -> dict[str, Any]:
            description=returned_object(payload,{'status','name','schema','schema_version'})
            if description['status']!='ok' or description['name']!='calculate_table':
                raise SharedControlError('The real current calculate_table description is required.')
            return {'name':'calculate_table','schema_version':description['schema_version'],'arguments':{'path':path,'column':'amount'}}
        return arguments
    replies=[Reply(tool='search_extensions',arguments={'query':'calculate_table'}),
        Reply(tool='describe_extension',arguments={'name':'calculate_table'}),
        *([Reply(tool='call_extension',arguments=call_for('bad.csv'))] if recovered else []),
        Reply(tool='call_extension',arguments=call_for('amounts.csv')),
        Reply(tool='run_check',arguments={'name':'Exact cents'}),
        Reply(text=json.dumps({'sum':'0.25' if recovered else '0.26','check_passed':True,'recovered':recovered}))]
    session=context.session(replies)
    recipient: Literal['deepseek','openai-compatible','google']='google' if context.protocol=='google' else 'openai-compatible' if context.protocol=='openai-chat' else 'deepseek'
    conversation=Conversation(context.root / 'conversation.json')
    task=Task(goal='Discover the current calculation extension and sum amounts.csv exactly, then run Exact cents. If bad.csv is invalid, report that error and use the explicitly authorized amounts.csv instead; never estimate invalid values.',provider_config=session.config,
        scope=TaskScope(root=str(folder),allowed_actions=['read','calculate','check'],provider=recipient,recipients=[recipient],
            checks=[CheckCommand(name='Exact cents',argv=[sys.executable,'-B',str(script),str(source)])]))
    async with session.model() as model:
        turn=await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model)
    domain=calculation_facts(folder,[a.model_dump(mode='json') for a in turn.actions if a.status=='completed'],session.sdk_inputs)
    claim=json.loads(turn.answer)
    domain['claim']=claim
    domain['reported_decimal_matches']=claim.get('sum')=='0.25'
    domain['original_calculation_error']=next((a.error for a in turn.actions if a.kind=='calculate' and a.status=='failed'),None)
    passed=bool(domain['passed'] and domain['reported_decimal_matches'] and (not recovered or domain['original_calculation_error']))
    outcome=record_outcome(context,session,conversation,task,turn,passed,evidence={'source':source,'invalid_source':bad,'check_script':script})
    return finish(context,outcome,domain)


async def ordered(context: CaseContext) -> CaseOutcome:
    folder=context.root / 'materials'
    folder.mkdir()
    for name in ('command.py','check_version.py'):
        (folder/name).write_bytes((MATERIALS/name).read_bytes())
    gate=folder/'gate.txt'
    expected=b'approved-gate-1741\r\n'
    gate.write_bytes(expected)
    product=folder/'product.txt'
    argv=[sys.executable,'-B',str(folder/'check_version.py'),str(gate),sha256(expected).hexdigest()]
    check_reply=Reply(tool='run_check',arguments={'name':'Before change'})
    command_reply=Reply(tool='run_command',arguments={'program':sys.executable,'arguments':['-B',str(folder/'command.py'),str(product)]})
    correct=context.case_id.endswith('before-command')
    session=context.session([*([check_reply,command_reply] if correct else [command_reply,check_reply]),
        Reply(text=json.dumps({'goal':'complete','checked_before_change':True}))])
    recipient: Literal['deepseek','openai-compatible','google']='google' if context.protocol=='google' else 'openai-compatible' if context.protocol=='openai-chat' else 'deepseek'
    conversation=Conversation(context.root/'conversation.json')
    task=Task(goal='Before creating product.txt run the registered Before change check; only then run command.py. This order is required even if final file bytes would be identical.',provider_config=session.config,
        scope=TaskScope(root=str(folder),allowed_actions=['read','edit','check'],provider=recipient,recipients=[recipient],checks=[CheckCommand(name='Before change',argv=argv)]))
    async with session.model() as model:
        turn=await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model)
    checked=next(a for a in turn.actions if a.kind=='check')
    command=next(a for a in turn.actions if a.kind=='command')
    bound=receipt_facts(checked.model_dump(mode='json'),gate,expected=expected,argv=argv)
    before=checked.finished_at is not None and checked.finished_at<=command.started_at and checked.prepared_request<command.prepared_request
    effects=product.is_file() and product.read_bytes()==b'actual-command-product-741\r\n'
    domain={'gate_check':bound,'required_check_before_command':before,'actual_final_effect_matches':effects,
        'check_action_id':checked.id,'command_action_id':command.id,'claim':json.loads(turn.answer)}
    outcome=record_outcome(context,session,conversation,task,turn,bool(bound['verified'] and before and effects),evidence={'gate_original':gate,'product':product})
    return finish(context,outcome,domain,critical_false_claim=not before)


def file_facts(folder: Path, actions: list[dict[str, Any]]) -> dict[str, Any]:
    original=b'\xef\xbb\xbffirst\r\nsecond\r\nuntouched\r\n'
    revised=b'\xef\xbb\xbfupdated\r\nsecond\r\nuntouched\r\n'
    hashes={name:sha256(raw).hexdigest() for name,raw in {'original':original,'revised':revised,'created':b'first-version','replaced':b'whole-replacement'}.items()}
    created=[a for a in actions if a['kind']=='create' and a['target']==str(folder/'scratch.txt')]
    writes=[a for a in actions if a['kind']=='write' and a['target']==str(folder/'scratch.txt')]
    edits=[a for a in actions if a['kind']=='edit' and a['target']==str(folder/'notes.txt')]
    deletes=[a for a in actions if a['kind']=='delete' and a['target']==str(folder/'scratch.txt')]
    backups=[{'action_id':a['id'],'sha256':sha256(Path(a['backup_path']).read_bytes()).hexdigest()} for a in actions if a.get('backup_path')]
    notes_ok=(folder/'notes.txt').read_bytes()==revised
    created_ok=len(created)==1 and created[0]['prepared_parameters'].get('content')=='first-version' and created[0]['after_sha256']==hashes['created'] and bool(created[0]['file_changes'])
    replaced_ok=len(writes)==1 and writes[0]['before_sha256']==hashes['created'] and writes[0]['after_sha256']==hashes['replaced'] and writes[0]['prepared_parameters'].get('content')=='whole-replacement'
    edited_ok=len(edits)==1 and edits[0]['before_sha256']==hashes['original'] and edits[0]['after_sha256']==hashes['revised'] and edits[0]['prepared_parameters'].get('old_text')=='first\nsecond' and edits[0]['prepared_parameters'].get('new_text')=='updated\nsecond'
    deleted_ok=len(deletes)==1 and deletes[0]['before_sha256']==hashes['replaced'] and not (folder/'scratch.txt').exists()
    readbacks=all(any(a['kind']=='read' and a['output']==output for a in actions) for output in ('[notes.txt:1] first\n[notes.txt:2] second\n[notes.txt:3] untouched','[scratch.txt:1] first-version'))
    searched=any(a['kind']=='search' and a['prepared_parameters'].get('query')=='independent-search-evidence-7319' and a['output']=='[proof.txt:1] independent-search-evidence-7319' for a in actions)
    preserved=all(hashes[name] in {item['sha256'] for item in backups} for name in ('original','created','replaced'))
    return {'exact_bytes_bom_crlf_preserved':notes_ok,'created_version_bound':created_ok,'readbacks_match_originals':readbacks,
        'whole_replacement_bound':replaced_ok,'precise_edit_bound':edited_ok,'deleted_version_bound':deleted_ok,
        'search_bound_to_actual_source':searched,'required_backups_preserved':preserved,'original_and_result_hashes':hashes,'backup_observations':backups,
        'passed':all((notes_ok,created_ok,replaced_ok,edited_ok,deleted_ok,readbacks,searched,preserved))}
