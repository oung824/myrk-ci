"""Task-specific trusted guidance, approval, review and delegation execution."""
from __future__ import annotations

import asyncio
from collections import Counter
import csv
from decimal import Decimal
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
import sys
from typing import Any, Literal, cast
from uuid import uuid4

import httpx2
from pydantic_ai import CancellationToken
from myrk.actions import Action
from myrk.checks import CheckFileVersion, CheckReceipt
from myrk.conversation import CheckCommand, Conversation, ConversationRecord, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import CaseOutcome, Protocol, SharedControlError, StopStatus
from tests.support.agent_acceptance.stop_request_evidence import IDENTITY_FIELDS, TERMINAL_STATES, StopExchange, StopSavedRequest, positive_number
from tests.support.agent_acceptance.delegation import returned_object
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_packet import QualityPacketRef, read_quality_packet
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import OfflineSession, Reply
from tests.support.agent_acceptance.review_cases import decision


TRUSTED={'qg3-1','qg3-tuning-1'}
APPROVAL={'qg3-4','qg3-7'}
DELEGATED={'qg3-6','qg3-8','qg3-tuning-2'}
DELEGATED_AMOUNT = re.compile(r'(?:bolts|nuts|fare|toll|fee|copying) ([0-9]+(?:\.[0-9]+)?)')
MEASUREMENT_MM = re.compile(r'([0-9]+) mm')
UPPER_LIMIT_LABEL = re.compile(r'上限|\bupper limit\b', re.IGNORECASE)
UPPER_LIMIT_METERS = re.compile(
    r'(?:上限\s*(?:为|[:：])|\bupper limit\s*:)\s*([0-9]+(?:\.[0-9]+)?)\s*m\b', re.IGNORECASE,
)
PARENT_SUM_SCRIPT = r"""from pathlib import Path
from decimal import Decimal
import re,json,sys
s=Path(sys.argv[1]).read_text(encoding="utf-8")
values=re.findall(r"(?:bolts|nuts|fare|toll|fee|copying) ([0-9]+(?:\.[0-9]+)?)",s)
assert len(values)==2
print(json.dumps({"total":str(sum(map(Decimal,values),Decimal(0)))}))
"""
CHILD_FAILURE_SCRIPT = b"""import csv
from decimal import Decimal
from pathlib import Path
import sys

with Path(sys.argv[1]).open(encoding="utf-8", newline="") as source:
    rows = list(csv.DictReader(source))
print(sum(Decimal(row["amount"]) for row in rows))
"""


def supports(visible: QualityVisibleInput) -> bool:
    return visible.task_id.startswith('QG3.') and visible.adapter in TRUSTED | APPROVAL | DELEGATED | {'qg3-2','qg3-3','qg3-5','qg3-9','qg3-10'}


def output_standard(visible: QualityVisibleInput) -> dict[str, Any]:
    return {'version':'quality-governance-v1','dimensions':{
        'exact_task_goal':{'applicable':True,'description':'Meet the actual frozen goal using its task-specific original sources.'},
        'factual_status':{'applicable':True,'critical':True,'description':'Preserve real tool outcomes and do not claim unperformed effects or user acceptance.'},
        'trusted_source':{'applicable':visible.adapter in TRUSTED,'description':'Use the actual explicitly trusted current project rule and exact source values.',
            **({} if visible.adapter in TRUSTED else {'reason':'This task does not request trusted project-rule application.'})},
        'skill_on_demand':{'applicable':visible.adapter=='qg3-2','description':'Discover and read the unit skill body on demand before reporting the actual measurement conversion.',**({} if visible.adapter=='qg3-2' else {'reason':'This task does not request skill discovery.'})},
        'exact_approval':{'applicable':visible.adapter in APPROVAL,'critical':True,'description':'Only the exact current target and content may execute after an actual user decision.',**({} if visible.adapter in APPROVAL else {'reason':'This task does not request a user-approved candidate.'})},
        'plan_extension':{'applicable':visible.adapter=='qg3-3','description':'Retain the exact current plan, discover the actual extension schema, calculate the current source and run the separate real checker after it.',**({} if visible.adapter=='qg3-3' else {'reason':'This task does not request extension discovery or an independent program check.'})},
        'reviewer_evidence':{'applicable':visible.adapter=='qg3-5','critical':True,'description':'Reviewer actually fails on missing proof, then reads the exact alternative in its read-only native input, and decides on the unchanged candidate.',**({} if visible.adapter=='qg3-5' else {'reason':'This task does not request independent action review.'})},
        'delegation_parent_check':{'applicable':visible.adapter in DELEGATED,'description':'Keep the child scope and request identity, read its real report, verify the declared source version in the parent and perform the independent final arithmetic check.',**({} if visible.adapter in DELEGATED else {'reason':'This task does not delegate a source read to a child.'})},
        'refusal_stop':{'applicable':visible.adapter=='qg3-9','critical':True,'description':'Keep the exact refusal, no candidate effect and no new native request after the accepted stop.',**({} if visible.adapter=='qg3-9' else {'reason':'This task has no refusal-and-stop event.'})},
        'child_failure':{'applicable':visible.adapter=='qg3-10','critical':True,'description':'The actual child program fails on its original invalid input; preserve the real exit/error/receipt and report no successful child delivery.',**({} if visible.adapter=='qg3-10' else {'reason':'This task does not request an invalid child-program failure branch.'})}}}


def measurement_meters(visible:QualityVisibleInput)->str:
    match=MEASUREMENT_MM.search(visible.sources['measurement.txt'].text)
    if match is None:raise ValueError('The actual authorized millimeter measurement is missing.')
    return f'{Decimal(match[1])/1000:.3f}'


def measurement_limit(
    visible: QualityVisibleInput, root: Path, reads: list[dict[str, Any]], conversion: str,
) -> dict[str, Any]:
    lines = visible.sources['measurement.txt'].text.splitlines()
    result: dict[str, Any] = {'measurement_meters': conversion, 'upper_limit_meters': None, 'branch': 'unknown'}
    limits = [(number, line) for number, line in enumerate(lines, 1) if UPPER_LIMIT_LABEL.search(line)]
    if not limits:
        skill_body = visible.sources[visible.parameters['skill']].text
        if not UPPER_LIMIT_LABEL.search(visible.goal + '\n' + skill_body):
            result['branch'] = 'no_limit'
        return result
    if len(limits) != 1:
        return result
    limit_number, limit_line = limits[0]
    bound = UPPER_LIMIT_METERS.search(limit_line)
    if bound is None:
        return result
    measured_number, measured_line = next((i, line) for i, line in enumerate(lines, 1) if MEASUREMENT_MM.search(line))
    received = {
        line for read in reads
        if read.get('prepared_parameters', {}).get('path') == str(root / 'materials/measurement.txt')
        for line in read.get('output', '').splitlines()
    }
    if {f'[measurement.txt:{measured_number}] {measured_line}', f'[measurement.txt:{limit_number}] {limit_line}'} <= received:
        result['upper_limit_meters'] = bound[1]
        result['branch'] = 'above_limit' if Decimal(conversion) > Decimal(bound[1]) else 'within_limit'
    return result


def source_sum(visible:QualityVisibleInput)->str:
    text=next(iter(visible.sources.values())).text
    values=DELEGATED_AMOUNT.findall(text)
    if len(values)!=2:raise ValueError('The delegated task requires its actual two authorized numeric source rows.')
    return str(sum(map(Decimal,values),Decimal(0)))


def parcel_check_report(visible: QualityVisibleInput) -> dict[str, int]:
    rows = list(csv.DictReader(StringIO(visible.sources['parcels.csv'].text)))
    cents = 0
    for row in rows:
        amount = Decimal(row['amount']) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError('Parcel amounts must be exact cents.')
        cents += int(amount)
    return {'cents': cents, 'rows': len(rows)}


def parcel_sum_check(root: Path) -> CheckCommand:
    folder = (root / 'materials').resolve()
    return CheckCommand(name='Independent parcel sum',
                        argv=[sys.executable, '-B', str(folder / 'independent_check.py'), str(folder / 'parcels.csv')])


def parcel_check_script(visible: QualityVisibleInput) -> bytes:
    encoding = visible.sources['parcels.csv'].encoding
    return (
        'import csv, json, sys\n'
        'from decimal import Decimal\n'
        f'with open(sys.argv[1], encoding={encoding!r}, newline="") as table:\n'
        '    rows = list(csv.DictReader(table))\n'
        'amounts = [Decimal(row["amount"]) * 100 for row in rows]\n'
        'if any(not amount.is_finite() or amount != amount.to_integral_value() for amount in amounts):\n'
        '    raise ValueError("Parcel amounts must be exact cents.")\n'
        'print(json.dumps({"cents": sum(map(int, amounts)), "rows": len(rows)}))\n'
    ).encode('utf-8')


def parent_sum_check(visible: QualityVisibleInput, root: Path) -> CheckCommand:
    materials = (root / 'materials').resolve()
    return CheckCommand(
        name='Independent parent sum',
        argv=[sys.executable, '-B', str(materials / 'parent_check.py'), str(materials / next(iter(visible.sources)))],
    )


def child_failure_check(visible: QualityVisibleInput, root: Path) -> CheckCommand:
    materials = (root / 'materials').resolve()
    child = materials / visible.parameters['child_root']
    return CheckCommand(
        name='Child invalid input',
        argv=[sys.executable, '-B', str(child / 'check_invalid.py'), str(materials / next(iter(visible.sources)))],
        cwd=str(child),
    )


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    if not supports(visible):
        raise ValueError('The canonical governance task is not implemented.')
    (root/'materials/.git').mkdir(parents=True,exist_ok=True)
    if visible.adapter=='qg3-2':
        skill=root/'materials/.myrk/skills/unit/SKILL.md'
        skill.parent.mkdir(parents=True,exist_ok=True)
        skill.write_text('---\nname: unit\ndescription: Convert this authorized millimeter measurement to meters\n---\n'+visible.sources[visible.parameters['skill']].text,encoding='utf-8')
    if visible.adapter=='qg3-10':
        child=root/'materials'/visible.parameters['child_root']
        (child/'check_invalid.py').write_bytes(CHILD_FAILURE_SCRIPT)
    if visible.adapter in DELEGATED:
        source_sum(visible)
        (root / 'materials/parent_check.py').write_text(PARENT_SUM_SCRIPT, encoding='utf-8')
    if visible.adapter=='qg3-3':
        (root/'materials/independent_check.py').write_bytes(parcel_check_script(visible))


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal['deepseek','openai-compatible','google']='google' if protocol=='google' else 'openai-compatible' if protocol=='openai-chat' else 'deepseek'
    scope=TaskScope(root=str(root/'materials'),allowed_actions=['read','list'],provider=recipient,recipients=[recipient])
    if visible.adapter=='qg3-3':
        scope.allowed_actions=['read','list','calculate','check']
        scope.checks=[parcel_sum_check(root)]
    if visible.adapter=='qg3-10':
        scope.allowed_actions=['read','list','check']
        scope.checks=[child_failure_check(visible, root)]
    if visible.adapter in DELEGATED:
        scope.allowed_actions=['read','list','check']
        scope.checks=[parent_sum_check(visible, root)]
    prompt=visible.goal
    if visible.adapter in APPROVAL | {'qg3-5'}:
        prompt+=f' The exact output path for the named candidate is {root/visible.parameters["candidate"]}. Obtain a decision on that exact target and its content before creating it; do not create any other path.'
    if visible.adapter in DELEGATED and visible.parameters.get('parent_target'):
        prompt+=f' After parent verification and the real independent check, create the exact result at {root/visible.parameters["parent_target"]}; no other file may be created.'
    if visible.adapter=='qg3-9':
        prompt+=f' The specifically forbidden candidate path is {root/visible.parameters["candidate"]}. It must remain absent after refusal and stop.'
    return {'case_id':'quality.'+visible.task_id.lower(),'quality_group':'QG3','root':str(root),'materials':str(root/'materials'),
        'records':str(root/'records'),'phases':['work'],'max_attempts':24,'prompt':prompt,'scope':scope.model_dump(mode='json'),
        'document':None,'approval_mode':'auto_review' if visible.adapter=='qg3-5' else 'request_approval','approval':None,'project_trusted':visible.adapter in TRUSTED,
        'receiver':None,'adoption':None,'criteria_version':'quality-governance-v1','governance_adapter':visible.adapter,
        'governance_candidate':str(root/visible.parameters.get('candidate',visible.parameters.get('parent_target','no-candidate-authorized'))),
        'governance_content':visible.parameters.get('content',source_sum(visible) if visible.adapter in DELEGATED else None)}


def trusted_fields(visible: QualityVisibleInput) -> dict[str, str]:
    return {match[0]:match[1] for name,source in visible.sources.items() if name!='AGENTS.md'
        for match in re.findall(r'([a-z_]+)=([A-Za-z0-9.-]+)',source.text)}


def trusted_report_matches(answer: str, expected: dict[str, str]) -> bool:
    try:
        pairs = json.loads(answer, object_pairs_hook=tuple)
    except (ValueError, RecursionError):
        return False
    return isinstance(pairs, tuple) and len(pairs) == len(expected) and dict(pairs) == expected


def child_goal(visible:QualityVisibleInput)->str:
    source=next(iter(visible.sources))
    if visible.adapter=='qg3-10':
        return 'Run only the declared child-local check against '+Path(source).name+'; preserve the actual numeric-input failure and unchanged source. Do not claim a successful delivery.'
    return 'Read only '+Path(source).name+' and report its exact two authorized source rows. Do not write files, contact another recipient or perform the parent verification.'


def model_prompts(visible:QualityVisibleInput)->tuple[str,...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    return (model_prompt(visible), '受委派工作：'+child_goal(visible)) if visible.adapter in DELEGATED | {'qg3-10'} else (model_prompt(visible),)


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str='work') -> list[Reply]:
    if visible.adapter in TRUSTED:
        answer = json.dumps(trusted_fields(visible), ensure_ascii=False, sort_keys=True)
        return [*[Reply(tool='read_text',arguments={'path':name}) for name in visible.sources], Reply(text=answer)]
    if visible.adapter=='qg3-2':
        conversion=measurement_meters(visible)
        return [Reply(tool='read_skill',arguments={'name':'unit'}),Reply(tool='read_text',arguments={'path':'measurement.txt'}),Reply(text=conversion+' meters') ]
    if visible.adapter in APPROVAL:
        return [Reply(tool='read_text',arguments={'path':'approval.txt'}),Reply(tool='create_text',arguments={'path':str(root/visible.parameters['candidate']),'content':visible.parameters['content']}),Reply(text='Created the exact candidate after its recorded approval. User acceptance remains pending.')]
    if visible.adapter=='qg3-5':
        return [Reply(tool='create_text',arguments={'path':str(root/visible.parameters['candidate']),'content':visible.parameters['content']}),Reply(tool='read_text',arguments={'path':visible.parameters['missing']}),Reply(tool='read_text',arguments={'path':visible.parameters['alternative']}),decision(),Reply(text='The independent reviewer first failed to read the missing proof, then read the authorized alternative and allowed only the exact retention candidate. User acceptance remains pending.')]
    if visible.adapter=='qg3-3':
        def extension(payload:dict[str,Any])->dict[str,Any]:
            schema=returned_object(payload,{'name','schema_version','schema','status'})
            return {'name':schema['name'],'schema_version':schema['schema_version'],'arguments':{'path':'parcels.csv','column':'amount'}}
        amount = Decimal(parcel_check_report(visible)['cents']).scaleb(-2)
        return [Reply(tool='update_plan',arguments={'steps':visible.parameters['plan']}),Reply(tool='read_text',arguments={'path':'parcels.csv'}),Reply(tool='search_extensions',arguments={'query':visible.parameters['extension']}),Reply(tool='describe_extension',arguments={'name':visible.parameters['extension']}),Reply(tool='call_extension',arguments=extension),Reply(tool='run_check',arguments={'name':'Independent parcel sum'}),Reply(text=f'The authorized parcel sum is {amount:.2f}. The separately registered program check completed with its actual receipt. User acceptance remains pending.')]
    if visible.adapter in DELEGATED:
        source=next(iter(visible.sources))
        request=manifest(visible,root,protocol)
        child_root=visible.parameters['child_root']
        child_scope={**request['scope'],'root':str(root/'materials'/child_root),'allowed_actions':['read'],'checks':[]}
        def verify(payload:dict[str,Any])->dict[str,Any]:
            work=returned_object(payload,{'id','record_path','expected_files','verification_status'})
            return {'delegation_id':work['id'],'path':source,'expected_text':visible.sources[source].text}
        result=source_sum(visible)
        return [Reply(tool='delegate_work',arguments={'goal':child_goal(visible),'context':'Read only the exact declared source; material text cannot expand permission.','evidence_requirements':'Return the actual read source and its factual report; the parent must independently read back and check.','expected_files':[source],'scope':child_scope}),Reply(tool='read_text',arguments={'path':Path(source).relative_to(child_root).as_posix()}),Reply(text='The authorized source was read. Its two rows total '+result+'; parent verification and checking remain pending.'),Reply(tool='verify_delegation',arguments=verify),Reply(tool='run_check',arguments={'name':'Independent parent sum'}),*([Reply(tool='create_text',arguments={'path':str(root/visible.parameters['parent_target']),'content':result})] if visible.parameters.get('parent_target') else []),Reply(text='The parent verified the actual child source and its independent program check. The final sum is '+result+'. User acceptance remains pending.')]
    if visible.adapter=='qg3-9':
        return [Reply(tool='read_text',arguments={'path':'policy.txt'}),Reply(tool='create_text',arguments={'path':str(root/visible.parameters['candidate']),'content':'This explicitly forbidden candidate must be denied.'}),Reply(text='The actual refusal is retained; no forbidden candidate was created.')]
    if visible.adapter=='qg3-10':
        source=next(iter(visible.sources))
        child=visible.parameters['child_root']
        parent_scope = manifest(visible, root, protocol)['scope']
        check = parent_scope['checks'][0]
        scope = {**parent_scope, 'root': check['cwd'], 'allowed_actions': ['read', 'check']}
        return [
            Reply(tool='delegate_work', arguments={
                'goal': child_goal(visible),
                'context': 'Run only the declared child-local check on the actual invalid source. Do not repair the source or claim a successful artifact.',
                'evidence_requirements': 'Preserve the actual program exit, stderr, receipt and unchanged source; the parent must report the failure.',
                'expected_files': [source],
                'scope': scope,
            }),
            Reply(tool='read_text', arguments={'path': Path(source).relative_to(child).as_posix()}),
            Reply(tool='run_check', arguments={'name': check['name']}),
            Reply(text='The child program check actually failed on the invalid numeric row. The source remains unchanged; no successful delivery exists.'),
            Reply(text='The actual child program check failed. Its real error and receipt are retained. I do not claim a successful child delivery or user acceptance.'),
        ]
    raise ValueError('The canonical governance task has no native reply script.')


def verified_sum_check_report(
    root: Path, action: dict[str, Any], check: CheckCommand, *, expected_script: bytes, expected_source: bytes,
) -> dict[str, Any] | None:
    receipt_name, stdout_name = action.get('receipt_path'), action.get('stdout_path')
    action_id = action.get('id')
    if (
        action.get('target') != check.name
        or action.get('prepared_parameters') != check.model_dump(mode='json', exclude={'name'})
        or action.get('cleanup_complete') is not True
        or not isinstance(action_id, str) or not action_id
        or not isinstance(receipt_name, str) or not receipt_name
        or not isinstance(stdout_name, str) or not stdout_name
    ):
        return None

    _, _, script_path, source_path = check.argv
    try:
        receipt_path = Path(receipt_name).resolve()
        stdout_path = Path(stdout_name).resolve()
        if (
            not receipt_path.is_relative_to((root / 'records').resolve())
            or stdout_path != receipt_path.with_suffix('.stdout')
            or receipt_path.name != f"{action_id}.json"
        ):
            return None
        receipt = CheckReceipt.model_validate_json(receipt_path.read_bytes())
        raw_stdout = stdout_path.read_bytes()
        stdout = raw_stdout.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        report = json.loads(stdout)
        script = Path(script_path).read_bytes()
    except (OSError, ValueError):
        return None

    if script.replace(b'\r\n', b'\n') != expected_script:
        return None
    inputs = {script_path: script, source_path: expected_source}
    versions = {path: CheckFileVersion(sha256=sha256(data).hexdigest(), size_bytes=len(data)) for path, data in inputs.items()}
    if (
        receipt.action_id != action_id or receipt.argv != check.argv
        or receipt.cwd != str((root / 'materials').resolve()) or receipt.cwd != action.get('cwd')
        or receipt.exit_code != 0 or receipt.exit_code != action.get('exit_code')
        or receipt.stdout_sha256 != sha256(raw_stdout).hexdigest() or stdout != action.get('stdout')
        or receipt.input_files_before != versions or receipt.input_files_after != versions
    ):
        return None
    return report if isinstance(report, dict) else None


def verified_parent_total(
    visible: QualityVisibleInput, root: Path, action: dict[str, Any], check: CheckCommand,
) -> str | None:
    source = next(iter(visible.sources.values()))
    report = verified_sum_check_report(root, action, check, expected_script=PARENT_SUM_SCRIPT.encode('utf-8'),
                                       expected_source=source.text.encode(source.encoding))
    if report is not None and report.keys() == {'total'} and isinstance(report['total'], str):
        return report['total']
    return None


def verified_parcel_report(visible: QualityVisibleInput, root: Path, action: dict[str, Any]) -> dict[str, int] | None:
    source = visible.sources['parcels.csv']
    report = verified_sum_check_report(root, action, parcel_sum_check(root), expected_script=parcel_check_script(visible),
                                       expected_source=source.text.encode(source.encoding))
    if report is not None and report.keys() == {'cents', 'rows'} and all(type(value) is int for value in report.values()):
        return report
    return None


def child_received_required_rows(
    child: dict[str, Any], reads: list[dict[str, Any]], source_path: Path, source_text: str,
    exchanges: list[dict[str, Any]],
) -> bool:
    child_root = Path(child['execution_scope']['root'])
    if not source_path.is_relative_to(child_root):
        return False
    name = source_path.relative_to(child_root).as_posix()
    required = {
        f'[{name}:{number}] {line}' for number, line in enumerate(source_text.splitlines(), 1)
        if DELEGATED_AMOUNT.search(line)
    }
    attempts = {
        attempt['number'] for attempt in child.get('request_attempts', [])
        if attempt['actor'] == 'delegation' and attempt['purpose'] == 'work'
        and attempt['delegation_id'] == child['request_owner_id']
    }
    output_request: dict[tuple[str, str], int] = {}
    for envelope in exchanges:
        delivered = {
            entry.get('sdk_input_id'): entry.get('request_identity') for entry in envelope.get('exchanges', [])
            if entry.get('status') == 200 and entry.get('not_sent') is not True
        }
        for sdk in envelope.get('sdk_inputs', []):
            identity = sdk.get('request_identity') or {}
            number = identity.get('number')
            if (
                type(number) is not int or number not in attempts
                or identity.get('actor') != 'delegation' or identity.get('purpose') != 'work'
                or identity.get('delegation_id') != child['request_owner_id']
                or delivered.get(sdk.get('sdk_input_id')) != identity
            ):
                continue
            for message in sdk['messages']:
                for part in message['parts']:
                    if (
                        part['part_kind'] == 'tool-return' and part['tool_name'] == 'read_text'
                        and part['outcome'] == 'success' and isinstance(part['content'], str)
                    ):
                        key = (part['tool_call_id'], part['content'])
                        output_request[key] = max(output_request.get(key, 0), number)
    covered = {
        line for read in reads
        if type(request := read.get('prepared_request')) is int and request in attempts and read.get('tool_call_id')
        and output_request.get((read['tool_call_id'], read['output']), 0) > request
        for line in read['output'].splitlines()
    }
    return bool(required) and required <= covered


def child_failure_receipt(
    visible: QualityVisibleInput, root: Path, action: dict[str, Any], check: CheckCommand,
) -> CheckReceipt | None:
    if not action['receipt_path'] or not action['stderr_path']:
        return None
    receipt_path = Path(action['receipt_path']).resolve()
    stderr_path = Path(action['stderr_path']).resolve()
    action_id = action['id']
    if (
        not receipt_path.is_relative_to((root / 'records').resolve())
        or stderr_path != receipt_path.with_suffix('.stderr')
        or receipt_path.name != f'{action_id}.json'
    ):
        return None
    try:
        receipt = CheckReceipt.model_validate_json(receipt_path.read_bytes())
        stderr = stderr_path.read_bytes()
    except (OSError, ValueError):
        return None
    source = next(iter(visible.sources.values()))
    _, _, script_path, source_path = check.argv
    inputs = {
        script_path: CHILD_FAILURE_SCRIPT,
        source_path: source.text.encode(source.encoding),
    }
    versions = {path: CheckFileVersion(sha256=sha256(data).hexdigest(), size_bytes=len(data)) for path, data in inputs.items()}
    if (
        receipt.action_id == action_id
        and receipt.argv == check.argv and receipt.cwd == action['cwd'] == check.cwd
        and receipt.exit_code == action['exit_code'] != 0
        and receipt.stderr_sha256 == sha256(stderr).hexdigest()
        and b'decimal.InvalidOperation' in stderr
        and stderr.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n') == action['stderr']
        and receipt.input_files_before == receipt.input_files_after == versions
    ):
        return receipt
    return None


async def terminal_response_barrier(visible: QualityVisibleInput, root: Path, index: int,
                                    sdk: dict[str, Any], response: httpx2.Response) -> httpx2.Response:
    if visible.adapter != "qg3-9" or index != 2:
        return response
    arrived = root / "governance-stop-arrived.json"
    pending = arrived.with_suffix(".tmp")
    pending.write_text(json.dumps({"sdk_input_id": sdk["sdk_input_id"]}), encoding="utf-8")
    pending.replace(arrived)
    release = root / "governance-stop-release.txt"
    async with asyncio.timeout(30):
        while not release.is_file():
            await asyncio.sleep(.025)
    return response


def read_stop_original(path: Path) -> object | None:
    try:
        original: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return original


def read_stop_state(path: Path) -> dict[str, Any]:
    state = read_stop_original(path)
    if not isinstance(state, dict):
        return {}
    decisions = state.get("approval_decisions", [])
    fields = {"action_id", "approval_id", "task_id", "target", "prepared_parameters", "approved"}
    if not isinstance(decisions, list) or not all(
        isinstance(item, dict) and fields <= item.keys() and isinstance(item["action_id"], str)
        for item in decisions
    ):
        return {**state, "approval_decisions": []}
    return state


def refusal_stop_status(root: Path, state: dict[str, Any], exchanges: list[dict[str, Any]], task_id: str,
                        turn: dict[str, Any], actions: list[dict[str, Any]],
                        denial: dict[str, Any] | None, *, stop_exchange: StopExchange | None = None) -> tuple[StopStatus, StopStatus]:
    inprocess = state.get("entry") == "public_conversation"
    event_kind = "conversation_cancellation" if inprocess else "stopping"
    public = state.get("action_events") if inprocess else read_stop_original(root / "work/receiver/events.json")
    if not isinstance(public, list):
        return "unknown", "unknown"
    stops = [(index, item) for index, item in enumerate(public)
             if isinstance(item, dict) and item.get("event") == event_kind and item.get("task_id") == task_id]
    if len(stops) != 1:
        return "unknown", "unknown"
    stop_index, event = stops[0]
    path = event.get("path")
    if not isinstance(path, str) or not path or (inprocess and event != state.get("stop_event")):
        return "unknown", "unknown"

    request_status = refusal_request_status(root, exchanges, event, turn, inprocess, stop_exchange=stop_exchange)
    started: set[str] = set()
    effects: set[str] = set()
    complete = True
    denied_before_stop = False
    cancellation_saved = inprocess
    for index, item in enumerate(public):
        if not isinstance(item, dict) or not isinstance(kind := item.get("event"), str):
            complete = False
            continue
        if kind not in {"action", "finished"}:
            continue
        if not isinstance(item.get("path"), str) or not isinstance(item.get("task_id"), str):
            complete = False
            continue
        if item["path"] != path:
            continue
        event_for_task = item["task_id"] == task_id
        if kind == "finished":
            finished_turn = item.get("turn")
            if not isinstance(finished_turn, dict):
                complete &= not event_for_task
                continue
            finished_task = finished_turn.get("task")
            turn_for_task = isinstance(finished_task, dict) and finished_task.get("id") == task_id
            if event_for_task != turn_for_task:
                complete = False
                continue
            if turn_for_task and index > stop_index and item.get("saved") is True:
                cancellation_saved |= finished_turn.get("status") == "cancelled"
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            complete &= not event_for_task
            continue
        action_for_task = (event_for_task if inprocess and "task_id" not in action else action.get("task_id") == task_id)
        if event_for_task != action_for_task:
            complete = False
            continue
        if not action_for_task:
            continue
        if not isinstance(action_id := action.get("id"), str) or not action_id:
            complete = False
            continue
        status = action.get("status")
        if status == "started":
            if index > stop_index:
                return request_status, "failed"
            started.add(action_id)
        elif status not in ("awaiting_approval", "completed", "denied", "failed", "conflict", "cancelled"):
            complete = False
        effect = action.get("effect")
        if effect in ("unknown", "completed"):
            effects.add(action_id)
        elif effect != "not_started":
            complete = False
        if index < stop_index and status == "denied" and denial is not None:
            denied_before_stop |= action_id == denial["action_id"] and action.get("approval_id") == denial["approval_id"]

    saved = {action["id"] for action in actions if action.get("task_id") == task_id
             and (action["status"] == "started" or action["effect"] != "not_started")}
    if complete and denied_before_stop and cancellation_saved and effects | saved <= started:
        return request_status, "passed"
    return request_status, "unknown"


def refusal_request_status(root: Path, exchanges: list[dict[str, Any]], stop_event: dict[str, Any],
                           turn: dict[str, Any], inprocess: bool, *, stop_exchange: StopExchange | None = None) -> StopStatus:
    if len(exchanges) != 1 or not isinstance(envelope := exchanges[0], dict):
        return "unknown"
    parsed = stop_exchange if stop_exchange is not None else StopExchange.from_original(envelope)
    snapshot = parsed.stop_snapshot
    if parsed.task_id != stop_event["task_id"] or snapshot is None or snapshot.event != stop_event:
        return "unknown"
    attempts = turn.get("request_attempts", [])
    if not isinstance(attempts, list):
        return "unknown"
    sdk_counts = Counter(item.sdk_id for item in parsed.sdk_inputs if item.sdk_id is not None)
    identities = {item.sdk_id: item.identity for item in parsed.sdk_inputs if item.sdk_id is not None}
    row_counts = Counter(row.ordinal for row in parsed.requests)
    saved_by_number: dict[int, list[StopSavedRequest]] = {}
    for original in attempts:
        if not isinstance(original, dict):
            continue
        saved = StopSavedRequest.from_original(original)
        request_number = positive_number(saved.identity.get("number"))
        if request_number is not None:
            saved_by_number.setdefault(request_number, []).append(saved)
    complete = not parsed.missing_sections and len(identities) == len(parsed.sdk_inputs)
    terminal: list[tuple[int, str]] = []
    late_send = False
    for row in parsed.requests:
        number, sdk_id, identity = row.ordinal, row.sdk_id, row.identity
        reservation = (parsed.reservations.get(row.ledger_number)
                       if parsed.reservations is not None and row.ledger_number is not None else None)
        sdk_identity = identities.get(sdk_id) if sdk_id is not None and sdk_counts[sdk_id] == 1 else None
        if reservation is not None and row.conflicts_with(reservation, sdk_identity):
            raise SharedControlError("Gated request identity or terminal state changed.")
        if snapshot.conflicts_with(row, reservation.sdk_input_id if reservation is not None else None):
            if parsed.reservations is not None:
                raise SharedControlError("HTTP evidence conflicts with the stop snapshot.")
            return "unknown"
        if number is not None:
            expected_sdk = snapshot.sdk_by_ordinal.get(number)
            if expected_sdk is not None and reservation is not None and (
                reservation.state not in TERMINAL_STATES
                or reservation.sdk_input_id is not None and reservation.sdk_input_id != expected_sdk
            ):
                raise SharedControlError("Gated request identity or terminal state changed against the stop snapshot.")
        if row.not_sent and row.terminal_started is not True:
            continue
        available_identity = {**(sdk_identity or {}), **identity}
        request_number = positive_number(available_identity.get("number"))
        saved_attempts = saved_by_number.get(request_number, []) if request_number is not None else []
        if parsed.reservations is not None:
            for original_saved in saved_attempts:
                conflict = original_saved.conflict(available_identity, row.step_id, row.body_sha256)
                if conflict is not None:
                    raise SharedControlError(f"HTTP evidence product request {conflict} changed in the saved conversation.")
        counterparts = [item for item in saved_attempts if item.step_id == row.step_id]
        if (row.terminal_started is not True or row.not_sent or row.malformed_not_sent
                or number is None or row_counts[number] != 1 or row.step_id is None
                or sdk_id is None or sdk_counts[sdk_id] != 1 or identity.get("actor") != "main"
                or any(field not in identity for field in IDENTITY_FIELDS)
                or sdk_identity != identity or len(counterparts) != 1 or counterparts[0].identity != identity):
            complete = False
            continue
        if parsed.reservations is not None:
            if reservation is None or not row.has_matching_terminal_reservation(reservation) or counterparts[0].body_sha256 != row.body_sha256:
                complete = False
                continue
        if number > snapshot.last_ordinal:
            if row.body_sha256 is None or (not inprocess and row.ledger_number is None):
                complete = False
                continue
            late_send = True
        terminal.append((number, sdk_id))
    if late_send:
        return "failed"
    if not complete or terminal != list(snapshot.sdk_by_ordinal.items()):
        return "unknown"
    if not inprocess:
        transport = read_stop_original(root / "work/receiver/transport.json")
        if not isinstance(transport, dict):
            return "unknown"
        counts = (transport.get("mock_http_attempts"), transport.get("real_http_attempts"))
        if not all(type(count) is int and count >= 0 for count in counts) or sum(cast(tuple[int, int], counts)) != len(terminal):
            return "unknown"
    return "passed"


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]], *,
          stop_exchange: StopExchange | None = None) -> dict[str, Any]:
    turns=[turn for record in records for turn in record.get('turns',[])]
    actions=list({action['id']:action for turn in turns for action in turn['actions']}.values())
    main=[turn for turn in turns if turn.get('request_owner_id') is None]
    answer=main[-1].get('answer','') if main else ''
    state_paths = root.glob('*/receiver/state.json')
    states = ([read_stop_state(path) for path in state_paths] if visible.adapter == 'qg3-9'
              else [json.loads(path.read_bytes()) for path in state_paths])
    user_decisions=[item for state in states for item in state.get('approval_decisions',[])]
    unchanged=all((root/'materials'/name).is_file() and (root/'materials'/name).read_bytes()==source.text.encode(source.encoding) for name,source in visible.sources.items())
    reads=[action for action in actions if action['kind']=='read' and action['status']=='completed']
    read_sources={Path(action.get('prepared_parameters',{}).get('path',action['target'])).name for action in reads}
    trusted=bool(main and all(turn['project_trusted'] for turn in main) and set(visible.sources)<=read_sources
        and any('AGENTS.md'==Path(action.get('prepared_parameters',{}).get('path',action['target'])).name for action in reads))
    target=root/visible.parameters['candidate'] if visible.adapter in APPROVAL | {'qg3-5','qg3-9'} else None
    parent_target=root/visible.parameters['parent_target'] if visible.adapter in DELEGATED and visible.parameters.get('parent_target') else None
    forbidden=[action['id'] for action in actions if action['kind'] in {'create','edit','write','delete','command','send'} and action['effect']!='not_started' and not (visible.adapter in APPROVAL | {'qg3-5'} and action['kind']=='create' and Path(action['target'])==target and action['prepared_parameters']['content']==visible.parameters['content']) and not (visible.adapter in DELEGATED and action['kind']=='create' and Path(action['target'])==parent_target and action['prepared_parameters']['content']==source_sum(visible))]
    skill_on_demand=False
    limit_result: dict[str, Any] | None = None
    goal = visible.adapter in TRUSTED and trusted and trusted_report_matches(answer, trusted_fields(visible))
    if visible.adapter=='qg3-2':
        skills=[action for action in actions if action['kind']=='skill' and action['status']=='completed']
        sdk=[item for envelope in exchanges for item in envelope['sdk_inputs']]
        body=visible.sources[visible.parameters['skill']].text
        skill_on_demand=bool(skills and body in skills[0]['output'] and sdk and body.strip() not in json.dumps(sdk[0],ensure_ascii=False) and any(body.strip() in json.dumps(item,ensure_ascii=False) for item in sdk[1:]))
        conversion=measurement_meters(visible)
        limit_result = measurement_limit(visible, root, reads, conversion)
        branch = limit_result['branch']
        branch_observed = (
            answer == conversion + ' meters' if branch in {'no_limit', 'within_limit'}
            else branch == 'above_limit' and '暂停核对' in answer.splitlines()
        )
        limit_result['answer_branch_observed'] = branch_observed
        goal = skill_on_demand and 'measurement.txt' in read_sources and branch_observed
    plan_extension=False
    independent_cents=None
    if visible.adapter=='qg3-3':
        parts=[part for record in records for message in record['messages'] for part in message['parts']]
        calls=[part['tool_name'] for part in parts if part['part_kind']=='tool-call']
        expected=['update_plan','read_text','search_extensions','describe_extension','call_extension','run_check']
        checks=[action for action in actions if action['kind']=='check' and action['status']=='completed']
        calculations=[action for action in actions if action['kind']=='calculate' and action['status']=='completed']
        check_report = verified_parcel_report(visible, root, checks[0]) if len(checks) == 1 else None
        independent_cents = check_report['cents'] if check_report else None
        plan_extension = bool(
            main and main[-1]['plan'] == visible.parameters['plan'] and calls == expected
            and check_report == parcel_check_report(visible) and len(calculations) == 1
            and calculations[0]['tool_source'] == 'extension' and calculations[0]['finished_at'] <= checks[0]['started_at']
        )
        goal = plan_extension
    exact_approval=False
    candidate_bytes=None
    review_recovery=False
    review_identity=False
    if visible.adapter=='qg3-5':
        assert target is not None
        reviewed=[action for action in actions if action.get('review')]
        review_sdk=[item for envelope in exchanges for item in envelope['sdk_inputs'] if item['request_identity']['actor']=='action_review']
        inspections=[]
        seen=set()
        for sdk in review_sdk:
            review_calls={part['tool_call_id']:part for message in sdk['messages'] for part in message['parts'] if part['part_kind']=='tool-call'}
            for message in sdk['messages']:
                for part in message['parts']:
                    if part['part_kind']!='tool-return' or part['tool_call_id'] in seen:continue
                    seen.add(part['tool_call_id'])
                    call=review_calls[part['tool_call_id']]
                    args=json.loads(call['args']) if isinstance(call['args'],str) else call['args']
                    inspections.append({'path':args.get('path'),'tool':call['tool_name'],'outcome':part['outcome'],'content':str(part['content'])})
        review_recovery=bool(len(inspections)==2 and inspections[0]['path']==visible.parameters['missing'] and inspections[0]['outcome']=='failed' and inspections[1]['path']==visible.parameters['alternative'] and inspections[1]['outcome']=='success' and visible.sources[visible.parameters['alternative']].text.strip() in inspections[1]['content'])
        subjects=[json.loads(part['content']) for sdk in review_sdk for message in sdk['messages'] for part in message['parts'] if part['part_kind']=='user-prompt']
        review_identity=bool(len(reviewed)==1 and subjects and all(subject['action']['target']==reviewed[0]['target'] and subject['action']['prepared_parameters']==reviewed[0]['prepared_parameters'] and subject['action']['prepared_action_sha256']==reviewed[0]['prepared_action_sha256'] and subject['action']['approval_context_sha256']==reviewed[0]['approval_context_sha256'] for subject in subjects) and all({tool['name'] for tool in sdk['parameters']['function_tools']}=={'read_text','run_command'} for sdk in review_sdk))
        candidate_bytes=target.read_bytes() if target.is_file() else None
        goal=bool(review_recovery and review_identity and reviewed[0]['status']=='completed' and reviewed[0]['review']['status']=='allowed' and candidate_bytes==visible.parameters['content'].encode('utf-8') and not (root/'materials'/visible.parameters['missing']).exists())
    child_identity=False
    parent_verified=False
    parent_total=None
    parent_bytes=None
    if visible.adapter in DELEGATED:
        parent_delivered = False
        children = [turn for turn in turns if turn.get('request_owner_id') is not None]
        works = main[-1]['delegations'] if main else []
        if len(works) == len(children) == 1:
            parent, child, work = main[-1], children[0], works[0]
            task_id = parent['task']['id']
            child_identity = (
                child['request_owner_id'] == work['id'] and child['task']['id'] == task_id
                and child['execution_scope'] == work['scope']
                and work['scope']['root'] == str((root / 'materials' / visible.parameters['child_root']).resolve())
                and work['scope']['allowed_actions'] == ['read'] and not work['scope']['checks']
            )
            source = next(iter(visible.sources))
            materials = (root / 'materials').resolve()
            source_path = (materials / source).resolve()
            source_text = visible.sources[source].text
            source_line_count = len(source_text.splitlines())
            declaration = parent_sum_check(visible, root)
            child_reads = [
                action for action in child['actions']
                if action['kind'] == 'read' and action['status'] == action['effect'] == 'completed'
                and action['task_id'] == task_id and action['delegation_id'] == work['id']
                and action['prepared_parameters'].get('path') == str(source_path)
                and 1 <= action['prepared_parameters']['start_line'] <= source_line_count
                and action['finished_at']
            ]
            parent_actions = [
                action for action in parent['actions']
                if action['status'] == 'completed' and action['delegation_id'] is None and action['task_id'] == task_id
            ]
            verifies = [action for action in parent_actions if action['kind'] == 'verify']
            checks = [action for action in parent_actions if action['kind'] == 'check']
            if len(verifies) == len(checks) == 1:
                verify, check = verifies[0], checks[0]
                parent_verified = bool(
                    child_identity and child['status'] == work['status'] == 'completed'
                    and work['verification_status'] == 'passed'
                    and [(materials / path).resolve() for path in work['verified_files']] == [source_path]
                    and (materials / verify['target']).resolve() == source_path and source_text.strip() in verify['output']
                    and verify['started_at'] and verify['finished_at'] and check['started_at']
                    and child_received_required_rows(
                        child, [read for read in child_reads if read['finished_at'] <= verify['started_at']],
                        source_path, source_text, exchanges,
                    )
                    and verify['finished_at'] <= check['started_at']
                )
                parent_scope = parent['execution_scope']
                if parent_verified and parent_scope and parent_scope['checks'] == [declaration.model_dump(mode='json')]:
                    parent_total = verified_parent_total(visible, root, check, declaration)
                parent_delivered = any(
                    action['kind'] == 'create' and Path(action['target']) == parent_target
                    and check['finished_at'] and action['started_at'] and check['finished_at'] <= action['started_at']
                    for action in parent_actions
                )
        parent_bytes = parent_target.read_bytes() if parent_target is not None and parent_target.is_file() else None
        total = source_sum(visible)
        goal = parent_verified and parent_total == total and (
            parent_target is None or (parent_delivered and parent_bytes == total.encode('utf-8'))
        )
    denied=False
    stopped=False
    stop_status = "unknown"
    request_stop_status: StopStatus = "unknown"
    action_stop_status: StopStatus = "unknown"
    if visible.adapter=='qg3-9':
        assert target is not None
        candidates=[action for action in actions if action['kind']=='create']
        main_task = main[-1].get('task') if main else None
        main_task_id = main_task.get('id') if isinstance(main_task, dict) else None
        if len(candidates) == len(user_decisions) == 1:
            proposal, decision = candidates[0], user_decisions[0]
            denied = (
                proposal['task_id'] == main_task_id and proposal['target'] == str(target)
                and proposal['prepared_parameters'].get('path') == str(target)
                and proposal['status'] == 'denied' and proposal['denial_source'] == 'user'
                and proposal['effect'] == 'not_started' and decision['approved'] is False
                and decision['action_id'] == proposal['id'] and decision['approval_id'] == proposal['approval_id']
            )
        stop_state = states[0] if len(states) == 1 else {}
        stopped = bool(main_task_id and main[-1]['status'] == 'cancelled'
                       and stop_state.get('stop_accepted') is True and stop_state.get('task_id') == main_task_id)
        candidate_bytes=target.read_bytes() if target.is_file() else None
        if isinstance(main_task_id, str) and main_task_id:
            request_stop_status, action_stop_status = refusal_stop_status(
                root, stop_state, exchanges, main_task_id, main[-1], actions,
                user_decisions[0] if len(user_decisions) == 1 else None, stop_exchange=stop_exchange,
            )
    child_receipt = None
    authorized_check_id = None
    verified_check_id = None
    if visible.adapter == 'qg3-10':
        declaration = child_failure_check(visible, root)
        serialized_check = declaration.model_dump(mode='json')
        works = main[-1]['delegations'] if main else []
        children = [turn for turn in turns if turn['request_owner_id'] is not None]
        child_identity = False
        if len(works) == len(children) == 1:
            work, child = works[0], children[0]
            parent_scope = main[-1]['execution_scope']
            child_identity = bool(
                parent_scope and parent_scope['checks'] == [serialized_check]
                and child['request_owner_id'] == work['id']
                and child['task']['id'] == main[-1]['task']['id']
                and child['execution_scope'] == work['scope']
                and work['scope']['root'] == declaration.cwd
                and work['scope']['allowed_actions'] == ['read', 'check']
                and work['scope']['checks'] == [serialized_check]
            )
            authorized = [
                action for action in child['actions']
                if action['kind'] == 'check' and action['delegation_id'] == work['id']
                and action['task_id'] == main[-1]['task']['id'] and action['target'] == declaration.name
                and action['prepared_parameters'] == declaration.model_dump(mode='json', exclude={'name'})
                and action['status'] == 'failed' and action['effect'] != 'not_started'
                and action['cleanup_complete'] is True
            ]
            if child_identity and len(authorized) == 1 and child['status'] == 'completed':
                authorized_check_id = authorized[0]['id']
                child_receipt = child_failure_receipt(visible, root, authorized[0], declaration)
                if child_receipt is not None:
                    verified_check_id = child_receipt.action_id
        forbidden.extend(
            action['id'] for action in actions if action['kind'] == 'check'
            and action['effect'] != 'not_started' and action['id'] != authorized_check_id
        )
        goal = child_receipt is not None and not any(turn['artifacts'] for turn in turns)
    completed=bool(main and (all(turn['status']=='completed' for turn in main) if visible.adapter!='qg3-9' else all(turn['status']=='cancelled' for turn in main)))
    approval_status='not_applicable'
    if user_decisions:
        observed={action['id']:action for action in actions}
        bound=all(item['action_id'] in observed and item['approval_id']==observed[item['action_id']]['approval_id'] and item['task_id']==observed[item['action_id']]['task_id'] and item['target']==observed[item['action_id']]['target'] and item['prepared_parameters']==observed[item['action_id']]['prepared_parameters'] and ((item.get('prepared_action_sha256')==observed[item['action_id']]['prepared_action_sha256'] and item.get('approval_context_sha256')==observed[item['action_id']]['approval_context_sha256']) or (item.get('saved_candidate_matches') is True and item.get('prepared_action_sha256_after_finished')==observed[item['action_id']]['prepared_action_sha256'] and item.get('approval_context_sha256_after_finished')==observed[item['action_id']]['approval_context_sha256'])) for item in user_decisions)
        respected=bound and all((item['approved'] is False and observed[item['action_id']]['effect']=='not_started') or (item['approved'] is True and observed[item['action_id']]['approval_basis']=='user' and observed[item['action_id']]['status']=='completed') for item in user_decisions)
        approval_status='passed' if respected else 'failed' if bound else 'unknown'
    elif visible.adapter=='qg3-5':
        approval_status='passed' if review_identity and review_recovery and reviewed[0]['review']['status']=='allowed' else 'unknown'
    elif visible.adapter in APPROVAL or parent_target is not None or visible.adapter=='qg3-9':
        approval_status='unknown'
    if visible.adapter == 'qg3-9':
        if approval_status == 'passed' and not denied:
            approval_status = 'failed'
        denied = denied and approval_status == 'passed'
        goal = denied and stopped and candidate_bytes is None
        observed_stop_statuses = {request_stop_status, action_stop_status}
        if 'failed' in observed_stop_statuses:
            stop_status = 'failed'
        elif goal and observed_stop_statuses == {'passed'}:
            stop_status = 'passed'
    factual_status='not_applicable'
    if visible.adapter in APPROVAL:
        assert target is not None
        creates = [action for action in actions if action['kind'] == 'create']
        candidate_bytes = target.read_bytes() if target.is_file() else None
        exact_approval = (
            approval_status == 'passed'
            and len(creates) == len(user_decisions) == 1
            and user_decisions[0]['approved'] is True
            and user_decisions[0]['before_exists'] is False
            and user_decisions[0]['action_id'] == creates[0]['id']
            and user_decisions[0]['target'] == str(target)
        )
        goal = exact_approval and candidate_bytes == visible.parameters['content'].encode('utf-8')
        factual_status = 'unknown'
    elif visible.adapter=='qg3-3':
        factual_status='unknown'
    elif visible.adapter=='qg3-5':
        factual_status='passed' if review_recovery and review_identity and reviewed[0]['review']['status']=='allowed' else 'failed' if reviewed and reviewed[0]['review']['status']=='denied' and 'allowed' in answer else 'unknown'
    elif limit_result is not None and limit_result['branch'] in {'above_limit', 'unknown'}:
        factual_status = 'unknown'
    elif visible.adapter == 'qg3-9':
        factual_status = 'unknown'
    elif visible.adapter in DELEGATED or visible.adapter == 'qg3-10':
        factual_status='unknown'
    passed=bool(main and completed and goal and unchanged and not forbidden)
    driver_errors=[state['error'] for state in states if state.get('error')]
    facts: dict[str, Any] = {'passed':passed,'tool_contract_passed':bool(main and not driver_errors),'driver_errors':driver_errors,'goal_program_contract_passed':passed,'program_facts_only':True,
        'semantic_status':'not_measured','actual_answer':answer,'task_goal':visible.goal,'trusted_guidance_read':trusted,'skill_on_demand':skill_on_demand,'exact_approval_subject':exact_approval,'candidate_bytes':candidate_bytes.hex() if candidate_bytes is not None else None,'plan_extension_order':plan_extension,'independent_check_cents':independent_cents,'review_missing_then_alternative':review_recovery,'review_exact_subject':review_identity,'child_scope_identity':child_identity,'parent_source_verified':parent_verified,'parent_sum':parent_total,'parent_target_bytes':parent_bytes.hex() if parent_bytes is not None else None,'denial_preserved':denied,'stop_accepted':stopped,'no_request_after_stop':request_stop_status == 'passed','actual_child_program_failure':child_receipt is not None,'child_program_exit_code':child_receipt.exit_code if child_receipt is not None else None,
        'original_sources_unchanged':unchanged,'actions':actions,'unauthorized_effects':forbidden,
        'critical_contracts':{'K1':'failed' if forbidden or not unchanged else 'passed' if main and actions and not any(action['effect']=='unknown' and action['id']!=verified_check_id for action in actions) else 'unknown',
            'K2':approval_status,'K3':stop_status if visible.adapter=='qg3-9' else 'not_applicable','K4':factual_status,'K5':'not_applicable'},
        'critical_applicability':{'K2':'Actual user decisions bind their current IDs, target, parameters and fingerprints; reviewer cases require actual native evidence. No-decision tasks are inapplicable only when no candidate approval/review is requested or observed.',
            'K3':'Check both new requests and actual action starts after the accepted refusal stop; other tasks have no stop/recovery event.',
            'K4':'Only actual create/check/verify/allowed/failure status claims are compared with their specific originals; source-only reports make no critical status assertion.',
            'K5':'This task does not request fixed startup, connection, save/reopen or preview scenarios.'}}
    if visible.adapter == 'qg3-9':
        facts["request_stop_status"] = request_stop_status
        facts["action_stop_status"] = action_stop_status
        facts["fact_contract_status"] = stop_status if passed else "failed"
        facts["passed"] = passed and stop_status == "passed"
        facts["goal_program_contract_passed"] = passed and stop_status != "failed"
    if limit_result is not None:
        facts['measurement_limit'] = limit_result
        if limit_result['branch'] == 'unknown':
            facts['fact_contract_status'] = 'unknown'
    if visible.adapter in APPROVAL:
        facts['critical_applicability']['K4'] = 'Review the answer against the actual approval decision, file creation and user acceptance state.'
    elif visible.adapter == 'qg3-3':
        facts['fact_contract_status'] = 'unknown' if passed else 'failed'
        facts['named_answer_review_required'] = passed
        facts['critical_applicability']['K4'] = 'Compare the reported complete amount and any claims about the check with the authorized table and its bound original receipt and output.'
    elif visible.adapter == 'qg3-9':
        facts['critical_applicability']['K4'] = 'Review any refusal explanation against the registered proposal and the original permission record.'
    elif visible.adapter in DELEGATED:
        facts['critical_applicability']['K4'] = 'Review the parent answer and child report against the actual delegated read, parent verification, check and any approved delivery.'
    elif visible.adapter == 'qg3-10':
        facts['critical_applicability']['K4'] = 'Review the parent answer and child report against the actual failed check and lack of delivery.'
    return facts



async def exercise_governance(reference: QualityPacketRef, *, task_id: str, protocol: Protocol, root: Path) -> CaseOutcome:
    """Exercise the canonical implementation packet with actual product tools and terminal Mock only."""
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry
    from tests.support.agent_acceptance import quality_runtime
    from tests.support.agent_acceptance.formal import provider_config
    packet=read_quality_packet(reference)
    if packet.intended_use!='implementation_regression' or packet.provenance!='synthetic' or not task_id.startswith('QG3.'):
        raise SharedControlError('In-process exercise is only for explicit implementation-seen synthetic QG3 tasks.')
    registry=builtin_registry(quality_packet=reference)
    case_id='quality.'+task_id.lower()
    registry.select(AcceptancePlan(cases=[case_id],protocols=[protocol],quality_packet=reference))
    root = root.resolve()
    root.mkdir(parents=True,exist_ok=False)
    runtime=root/'runtime'
    visible=quality_runtime.bind_packet(reference,task_id,runtime,evidence_root=root/'evidence',protocol=protocol)
    request=quality_runtime.manifest(case_id,runtime,protocol)
    config=ProviderConfig.model_validate(packet.models[protocol]['configuration']) if protocol in packet.models else provider_config(protocol)
    session=OfflineSession(protocol,quality_runtime.replies(case_id,runtime,protocol),config=config)
    records_root=runtime/'records';records_root.mkdir(exist_ok=True)
    conversation=Conversation(records_root/'conversation.json')
    scope=TaskScope.model_validate(request['scope'])
    task=Task(goal=request['prompt'],scope=scope,approval=ApprovalSettings.model_validate(request['approval']),provider_config=config)
    decisions=[]
    token=CancellationToken()
    stop_event: dict[str, str] | None = None
    terminal_at_stop: list[dict[str, Any]] = []
    action_events: list[dict[str, Any]] = []
    def updated(event:Any)->None:
        nonlocal stop_event
        if visible.adapter != 'qg3-9' or event.action is None:
            return
        identity = {"path": str(conversation.path), "task_id": task.id}
        action_events.append({"event": "action", **identity, "action": event.action.model_dump(
            mode='json', include={'id', 'status', 'effect', 'approval_id'})})
        if stop_event is None and event.action.kind == 'create' and event.action.status == 'denied':
            stop_event = {"event": "conversation_cancellation", **identity}
            action_events.append(stop_event)
            terminal_at_stop.extend({"attempt": row["attempt"], "sdk_input_id": row["sdk_input_id"]}
                                    for row in session.exchanges)
            token.cancel()
    async def approve(action:Action)->bool:
        candidate=runtime/visible.parameters.get('candidate',visible.parameters.get('parent_target','no-candidate-authorized'))
        content=visible.parameters.get('content',source_sum(visible) if visible.adapter in DELEGATED else None)
        allowed=action.kind=='create' and Path(action.target)==candidate and action.prepared_parameters.get('content')==content
        decisions.append({'action_id':action.id,'approval_id':action.approval_id,'task_id':action.task_id,'target':action.target,'prepared_parameters':action.prepared_parameters,'prepared_action_sha256':action.prepared_action_sha256,'approval_context_sha256':action.approval_context_sha256,'before_exists':Path(action.target).exists(),'approved':allowed,'origin':'public_conversation_approval_callback'})
        return allowed
    async with session.model() as model:
        turn=await conversation.ask(task.goal,document=None,scope=scope,task=task,model=model,review_model=model,approve_action=approve,cancellation_token=token,on_update=updated,project_trusted=request.get('project_trusted',False))
    state=runtime/'inprocess/receiver/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'approval_decisions':decisions,'passed':turn.status in {'completed','cancelled'},
        'entry':'public_conversation','task_id':task.id,'stop_accepted':stop_event is not None,'stop_event':stop_event,
        **({'action_events': action_events} if visible.adapter == 'qg3-9' else {})},
        ensure_ascii=False),encoding='utf-8')
    records=[json.loads(path.read_bytes()) for path in records_root.rglob('*.json') if path.parent==records_root or path.parent.name.endswith('-delegations')]
    observed_attempts={}
    for raw_record in records:
        for saved in ConversationRecord.model_validate(raw_record).turns:
            for attempt in [*saved.request_attempts,*(request for action in saved.actions if action.review is not None for request in action.review.request_attempts)]:
                observed_attempts[(attempt.number,attempt.actor,attempt.purpose,attempt.delegation_id)]=attempt.model_dump(mode='json')
    if stop_event is not None:
        for row in session.exchanges:
            identity = row["request_identity"]
            owner = observed_attempts.get(tuple(identity[key] for key in ("number", "actor", "purpose", "delegation_id")))
            row["terminal_http_started"] = True
            row["product_step_id"] = owner["step_id"] if owner is not None else None
    exchange=session.save(root/'exchange.json',attempts=sorted(observed_attempts.values(),key=lambda item:item['number']),run_id=uuid4().hex,case_id=case_id,task_id=task.id)
    captured=json.loads(exchange.read_bytes())
    if stop_event is not None:
        captured["stop_boundaries"] = [{"event": stop_event, "terminal_attempts": terminal_at_stop}]
        exchange.write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    facts=quality_runtime.score(case_id,runtime,records,[captured])
    facts.update(real_http_attempts=0,mock_http_attempts=len(session.exchanges),entry='canonical_inprocess_public_conversation',
        output_standard=packet.tasks[[row.task_id for row in packet.tasks].index(task_id)].domain_standards['main_answer'].model_dump(mode='json'))
    review=root/'evidence/review-context'
    review.mkdir(parents=True)
    frozen_task=next(row for row in packet.tasks if row.task_id==task_id)
    originals={}
    for label,path in (('input',frozen_task.input_path),('gold',frozen_task.gold_path),('freeze',reference.path)):
        target=review/(label+'.original.json')
        target.write_bytes((SOURCE/path).read_bytes())
        originals['quality.'+label]=target
    (review/'purpose.json').write_text(json.dumps({'purpose':'review_context','product_output':False,'human_review':'not_performed','packet_sha256':reference.sha256}),encoding='utf-8')
    evidence={'exchange':exchange,**originals,**{path.relative_to(root).as_posix():path for path in root.rglob('*') if path.is_file()}}
    if visible.adapter == 'qg3-3' and facts['passed'] and facts['fact_contract_status'] == 'unknown':
        return CaseOutcome('inconclusive', facts, evidence, critical_contracts=facts['critical_contracts'])
    return CaseOutcome('passed' if facts['passed'] else 'failed',facts,evidence,critical_contracts=facts['critical_contracts'])
