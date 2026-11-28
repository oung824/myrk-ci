"""Score the originals for governance checks and delegated work."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from myrk.checks import CheckFileVersion, CheckReceipt
from myrk.conversation import CheckCommand
from tests.support.agent_acceptance.quality_governance_inputs import (
    CHILD_FAILURE_SCRIPT,
    DELEGATED_AMOUNT,
    MEASUREMENT_MM,
    PARENT_SUM_SCRIPT,
    child_failure_check,
    parent_sum_check,
    parcel_check_script,
    parcel_sum_check,
    source_sum,
)
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput


UPPER_LIMIT_LABEL = re.compile(r'上限|\bupper limit\b', re.IGNORECASE)
UPPER_LIMIT_METERS = re.compile(
    r'(?:上限\s*(?:为|[:：])|\bupper limit\s*:)\s*([0-9]+(?:\.[0-9]+)?)\s*m\b', re.IGNORECASE,
)


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


def delegation_evidence(
    visible: QualityVisibleInput,
    root: Path,
    turns: list[dict[str, Any]],
    main: list[dict[str, Any]],
    parent_target: Path | None,
    exchanges: list[dict[str, Any]],
) -> tuple[bool, bool, str | None, bytes | None, bool]:
    child_identity = False
    parent_verified = False
    parent_total = None
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
    return child_identity, parent_verified, parent_total, parent_bytes, goal


def child_failure_evidence(
    visible: QualityVisibleInput,
    root: Path,
    turns: list[dict[str, Any]],
    main: list[dict[str, Any]],
) -> tuple[bool, CheckReceipt | None, str | None]:
    child_receipt = None
    authorized_check_id = None
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
    return child_identity, child_receipt, authorized_check_id
