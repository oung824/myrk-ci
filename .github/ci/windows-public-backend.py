"""Run an explicit public backend test group and publish counts from managed receipts."""

import argparse
import ast
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
GROUPS = {
    'records-and-context': (
        'test_conversation_format', 'test_context_task_goal', 'test_input_measurement_compatibility',
        'test_legacy_conversation_export', 'test_message_persistence_compatibility',
        'test_rendered_input_budget',
    ),
    'compaction-and-budget': (
        'test_auto_compaction', 'test_compaction_fact_contract', 'test_compaction_latest_requirement',
        'test_compaction_resume', 'test_independent_budget',
    ),
    'model-protocols': (
        'test_deepseek_output_limit', 'test_gemini_provider', 'test_model_controls', 'test_request_observation_replay',
    ),
    'desktop-service': (
        'test_connection_credentials', 'test_desktop_compaction', 'test_model_selection',
        'test_recovery_control_desktop',
    ),
    'check-resources': (
        'test_bare_check_paths', 'test_check_task_id', 'test_closeout', 'test_navigation',
        'test_process_receipt', 'test_project_paths', 'test_runtime_resources',
    ),
    'layout-context': ('test_layout_context',),
    'judge-fixtures': ('acceptance/test_judge_full_ledger', 'acceptance/test_judge_native_roundtrip'),
    'synthetic-acceptance': (
        'acceptance/test_actions', 'acceptance/test_compaction_replay', 'acceptance/test_recorded_response',
        'acceptance/test_reporting_response_decoding', 'acceptance/test_review_failure_matching',
        'acceptance/test_save_diagnostics',
    ),
    'synthetic-quality': (
        'acceptance/test_quality_governance_parcel_score', 'acceptance/test_quality_recovery_supplement',
        'acceptance/test_quality_diagnostic_review',
    ),
    'synthetic-judge-qualification': ('acceptance/test_judge_qualification',),
    'synthetic-trust-and-memory': ('acceptance/test_trust', 'acceptance/test_memory'),
    'synthetic-review-and-cancellation': (
        'acceptance/test_action_review', 'acceptance/test_merger_t02_cancel_boundary',
        'acceptance/test_public_cancellation_protocols', 'acceptance/test_task_continuation_snapshot_contract',
    ),
    'synthetic-local-scoring': ('test_formal_delivery_scoring',),
    'synthetic-question-review': ('acceptance/test_question_review',),
    'process-and-scheduling': ('test_parallel_checks', 'test_process_observation'),
    'root-git-hooks': ('test_closeout_file_lists', 'test_pre_commit', 'test_pre_commit_size'),
    'executor-and-network': (
        'test_action_reporting_facts', 'test_execution_identity', 'test_executor',
        'test_full_network', 'test_supervisor_origin',
    ),
    'serial-checks': ('test_check_commands_serial',),
    'action-approval': (
        'test_approval_instruction_contract', 'test_approval_modes', 'test_auto_review',
        'test_extension_review',
    ),
    'artifact-delivery': (
        'test_artifacts', 'test_extensions', 'test_independent_delivery',
    ),
    'conversation-and-delegation': (
        'test_context', 'test_conversation', 'test_delegation', 'test_delegation_verification_status',
        'test_independent_conversation', 'test_task_goal_context',
    ),
    'desktop-conversation': ('test_desktop_service', 'test_live_settings', 'test_window'),
    'recovery': ('test_compaction_recovery', 'test_recovery', 'test_recovery_ownership', 'test_step_window_recovery'),
    'retained-context': ('test_compaction_protocols', 'test_memory', 'test_persistent_layout', 'test_project_context'),
    'provider-transcripts': ('test_deepseek_protocol', 'test_gemini_observation', 'test_gemini_tools_and_resume'),
    'request-protocols': ('test_request_contracts', 'test_request_observation'),
    'review-evidence': (
        'test_independent_review', 'test_review_format_compatibility', 'test_review_tool_recovery',
        'test_review_tool_recovery_http',
    ),
}
PHASES = {'setup', 'call', 'teardown'}
RESULTS = ROOT / '.scratch/check-runs/results'
SYNTHETIC_COUNTS = {
    'synthetic-acceptance': 82,
    'synthetic-quality': 36,
    'synthetic-judge-qualification': 18,
    'synthetic-trust-and-memory': 42,
    'synthetic-review-and-cancellation': 22,
    'synthetic-local-scoring': 73,
    'synthetic-question-review': 76,
}
DESELECT = {
    'synthetic-judge-qualification': (
        'tests/acceptance/test_judge_qualification.py::test_native_judge_controls_keep_complete_input_and_zero_send_boundaries',
    ),
    'synthetic-trust-and-memory': (
        'tests/acceptance/test_memory.py::test_unsaved_notes_remain_absent_and_failed_batches_do_not_partially_apply[save-failure]',
    ),
    'synthetic-review-and-cancellation': (
        'tests/acceptance/test_action_review.py::test_independent_review_preserves_actual_decision_and_failures[stop]',
    ),
    'synthetic-local-scoring': (
        'tests/test_formal_delivery_scoring.py::test_local_delivery_does_not_replace_formal_domain_qualification',
    ),
    'synthetic-question-review': (
        'tests/acceptance/test_question_review.py::test_five_output_types_use_actual_sources_and_keep_domain_criteria',
        'tests/acceptance/test_question_review.py::test_formal_attachment_uses_the_same_read_only_product_settings',
    ),
}
DIAGNOSTIC_FILES = {
    'test_action_reporting_facts': 'executor-and-network',
    'test_recovery': 'recovery',
    'acceptance/test_actions': 'synthetic-acceptance',
}
DIAGNOSTIC_XML = 'public-diagnostic.xml'


class CheckError(Exception):
    pass


def publish(summary, message):
    print(message, flush=True)
    if summary:
        with Path(summary).open('a', encoding='utf-8') as output:
            output.write(message + '\n')


def run_pytest(summary, environment, phase, paths, *options):
    started = monotonic()
    result = subprocess.run(
        [sys.executable, '-B', 'scripts/run_checks.py', 'pytest', '-q',
         '-p', 'scripts.pytest_shards', *options, *paths],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding='utf-8', check=False,
    )
    publish(summary, f'- {phase} took {monotonic() - started:.2f} s. Exit code: {result.returncode}.')
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CheckError('The managed launcher did not return a receipt.') from None
    commands = receipt['commands']
    runtime_removed = int(receipt.get('runtime_removed') is True and not Path(receipt['runtime_directory']).exists())
    interpreter_exited = sum(command.get('interpreter_exit_confirmed') is True for command in commands)
    jobs_empty = sum(command.get('job_active_processes') == 0 for command in commands)
    publish(summary, f'- {phase} cleanup: {runtime_removed}/1 runtime removed, '
                     f'{interpreter_exited}/1 interpreter exited, {jobs_empty}/1 Windows process group empty.')
    if len(commands) != 1 or receipt['exit_code'] != result.returncode:
        raise CheckError('The launcher receipt does not match this invocation.')
    cleanup_complete = runtime_removed == interpreter_exited == jobs_empty == 1
    if result.returncode and environment.get('MYRK_TASK_ID') == 'github-public-backend-judge-fixtures':
        failure_text = commands[0].get('output_tail', '')
        for name in receipt.get('failure_logs', []):
            path = Path(name).resolve()
            if path.is_relative_to(RESULTS.resolve()) and path.is_file():
                with path.open('rb') as output:
                    output.seek(max(path.stat().st_size - 6000, 0))
                    failure_text += output.read().decode('utf-8', errors='replace')
        exception = re.findall(r'\b(?:FileNotFoundError|ImportError|ModuleNotFoundError|PermissionError|'
                               r'ValueError|TypeError|AttributeError|RuntimeError|AssertionError)\b', failure_text)
        frames = re.findall(r'\b(?:tests|myrk)[\\/][A-Za-z0-9_.\\/-]+\.py:\d+\b', failure_text)
        if exception:
            publish(summary, '- Judge exception classes: ' + ', '.join(dict.fromkeys(exception)) + '.')
        if frames:
            publish(summary, '- Judge failure frames: ' + ', '.join(list(dict.fromkeys(frames))[-6:]) + '.')
    return result.returncode, receipt, cleanup_complete


def report_file(receipt, name):
    reports = receipt.get('retained_reports', {}).get('paths', [])
    paths = [Path(directory) / 'pytest-shards' / name for directory in reports]
    matches = [path for path in paths if path.is_file() and path.resolve().is_relative_to(RESULTS.resolve())]
    if len(matches) != 1:
        raise CheckError(f'The managed launcher did not retain one {name} report.')
    return matches[0]


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def publish_judge_diagnostic(summary, observation_path):
    reports = observation_path.parent.parent
    for result_path in reports.glob('ordinary-public/*/result.json'):
        if not result_path.resolve().is_relative_to(reports.resolve()):
            continue
        result = read_json(result_path)
        for item in result.get('cases', []):
            facts = item.get('facts', {})
            budget = facts.get('budget', {})
            numbers = [budget.get(field) for field in ('actual_http', 'reserved_requests', 'completed_responses')]
            if any(type(number) is not int or not 0 <= number <= 130 for number in numbers):
                continue
            status = item.get('product_status')
            if status not in ('passed', 'failed', 'inconclusive', 'not_executed'):
                status = 'unknown'
            if facts.get('reason') == 'dependency did not pass':
                category = 'prerequisite'
            elif facts.get('unfinished'):
                category = 'budget-stop'
            elif facts.get('error_type') in (
                'AssertionError', 'FileNotFoundError', 'PermissionError', 'OSError', 'RuntimeError',
                'ValueError', 'APIConnectionError', 'RequestError', 'TimeoutError',
            ):
                category = facts['error_type']
            elif facts.get('error') or facts.get('error_type'):
                category = 'other-error'
            else:
                category = 'none'
            publish(summary, f'- Synthetic judge outcome: {status}, {category}, '
                             f'{numbers[0]} HTTP, {numbers[1]} reserved, {numbers[2]} completed.')
            failure = item.get('evidence', {}).get('failure')
            if not isinstance(failure, str):
                continue
            trace = (result_path.parent / failure).resolve()
            if not trace.is_relative_to(result_path.parent) or trace.name != 'failure.txt' or not trace.is_file():
                continue
            frames = re.findall(r'^  File "([^"]+)", line ([0-9]+),', trace.read_text(encoding='utf-8'), re.MULTILINE)
            for filename, lineno in frames[-7:]:
                source = Path(filename).resolve()
                if source.is_relative_to(ROOT / 'myrk') or source.is_relative_to(ROOT / 'tests'):
                    relative = source.relative_to(ROOT).as_posix()
                    if re.fullmatch(r'[A-Za-z0-9_./-]+\.py', relative) and source.is_file():
                        publish(summary, f'- Synthetic judge source frame: {relative}:{int(lineno)}.')


def publish_test_diagnostic(summary, xml_path, selected_path, outcomes):
    if not xml_path.is_file() or not xml_path.resolve().is_relative_to(RESULTS.resolve()):
        raise CheckError('The managed launcher did not retain the diagnostic report.')
    source = ast.parse((ROOT / selected_path).read_bytes())
    functions = {node.name for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    labels = {node.value for node in ast.walk(source) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    published = set(subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py'], cwd=ROOT, encoding='utf-8').split('\0'))
    tree = ElementTree.parse(xml_path)
    error_names = {'AssertionError', 'FileNotFoundError', 'PermissionError', 'OSError', 'RuntimeError', 'ValueError',
                   'TypeError', 'KeyError', 'AttributeError', 'TimeoutError', 'ConnectionError', 'ExceptionGroup',
                   'Failed', 'ModuleNotFoundError', 'ImportError', 'UnicodeDecodeError', 'ValidationError'}
    for case in tree.iter('testcase'):
        name = case.get('name', '')
        if case.get('file', '').replace('\\', '/') != selected_path:
            continue
        if outcomes.get(f'{selected_path}::{name}') != 'failed':
            continue
        node = re.fullmatch(r'(test_[A-Za-z0-9_]+)(?:\[([A-Za-z0-9_-]+)\])?', name)
        if node is None or node[1] not in functions:
            continue
        label = name if node[2] is None or node[2] in labels else node[1]
        publish(summary, f'- Failed case: {selected_path}::{label}.')
        for failure in (*case.findall('failure'), *case.findall('error')):
            frames = re.findall(r'^([^\r\n]+\.py):([0-9]+):\s*([A-Za-z][A-Za-z0-9_]*)',
                                failure.text or '', re.MULTILINE)
            for file, line, category in frames[-6:]:
                path = (ROOT / file.strip()).resolve()
                if path.is_relative_to(ROOT):
                    relative = path.relative_to(ROOT).as_posix()
                    if relative in published and re.fullmatch(r'[A-Za-z0-9_./-]+\.py', relative):
                        status = category if category in error_names else 'test-failure'
                        publish(summary, f'- Failure frame: {relative}:{int(line)} ({status}).')


def node_outcomes(expected, observation):
    reports_by_node = defaultdict(list)
    for report in observation['reports']:
        reports_by_node[report['nodeid']].append((report['when'], report['outcome']))
    outcomes = {}
    for node in expected:
        reports = reports_by_node[node]
        phases = Counter(phase for phase, _ in reports)
        results = {result for _, result in reports}
        if 'failed' in results:
            outcomes[node] = 'failed'
        elif 'skipped' in results:
            outcomes[node] = 'skipped'
        elif phases == dict.fromkeys(PHASES, 1) and results == {'passed'}:
            outcomes[node] = 'passed'
        else:
            outcomes[node] = 'incomplete'
    unexpected = len(reports_by_node.keys() - outcomes.keys())
    return outcomes, unexpected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group', choices=GROUPS)
    parser.add_argument('--native-only', action='store_true')
    parser.add_argument('--diagnostic-file', choices=DIAGNOSTIC_FILES)
    arguments = parser.parse_args()
    group = arguments.group
    if arguments.native_only and group != 'judge-fixtures':
        parser.error('--native-only requires the judge-fixtures group')
    if arguments.diagnostic_file and (group != DIAGNOSTIC_FILES[arguments.diagnostic_file] or arguments.native_only):
        parser.error('--diagnostic-file must match its registered group')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', MYRK_TASK_ID=f'github-public-backend-{group}')
    for name in ('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE',
                 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(name, None)
    publish(summary, f'### Public offline backend · {group}')
    publish(summary, 'Selected backend test files on a Windows hosted runner.')
    names = ('acceptance/test_judge_native_roundtrip',) if arguments.native_only else GROUPS[group]
    if arguments.diagnostic_file:
        names = (arguments.diagnostic_file,)
    paths = tuple(f'tests/{name}.py' for name in names)
    exclusions = DESELECT.get(group, ())
    deselection = tuple(f'--deselect={node}' for node in exclusions)
    for path in paths:
        publish(summary, f'- `{path}`')
    started = monotonic()
    stage = 'inventory'
    try:
        inventory = [name for names in GROUPS.values() for name in names]
        if len(inventory) != len(set(inventory)) or any(not (ROOT / path).is_file() for path in paths):
            raise CheckError('A test file is duplicated across groups or missing from this group.')
        stage = 'collection'
        status, receipt, cleaned = run_pytest(summary, environment, stage, paths, '--collect-only', '--myrk-shard=collect',
                                              *deselection)
        if status or not cleaned:
            raise CheckError('Collection or its process cleanup failed.')
        manifest_path = report_file(receipt, 'collection.json')
        manifest = read_json(manifest_path)
        expected = manifest['nodeids']
        counts = Counter(node.partition('::')[0] for node in expected)
        publish(summary, f'- Collected {len(expected)} tests across {len(counts)}/{len(paths)} selected files.')
        if set(counts) != set(paths) or len(expected) != len(set(expected)):
            raise CheckError('The collected files or test identities differ from the selection.')
        if not arguments.diagnostic_file and group in SYNTHETIC_COUNTS and (len(expected) != SYNTHETIC_COUNTS[group]
                or any(node.startswith(exclusions) for node in expected)):
            raise CheckError('The synthetic case selection differs from the reviewed scope.')
        stage = 'execution'
        diagnostic_options = ('--tb=short', '-o', 'junit_family=xunit1', f'--junitxml={DIAGNOSTIC_XML}') if arguments.diagnostic_file else ()
        status, receipt, cleaned = run_pytest(summary, environment, stage, paths, '--myrk-shard=other',
                                              f'--myrk-shard-manifest={manifest_path}', *deselection, *diagnostic_options)
        observation_path = report_file(receipt, 'observation.json')
        observation = read_json(observation_path)
        if status and arguments.native_only:
            publish_judge_diagnostic(summary, observation_path)
        outcomes, unexpected = node_outcomes(expected, observation)
        totals = Counter(outcomes.values())
        publish(summary, f'- Outcomes: {totals["passed"]} passed, {totals["failed"]} failed, '
                         f'{totals["skipped"]} skipped, {totals["incomplete"]} incomplete, {unexpected} unexpected.')
        if status and arguments.diagnostic_file:
            publish_test_diagnostic(summary, observation_path.parent.parent / DIAGNOSTIC_XML, paths[0], outcomes)
        for path in paths:
            counts = Counter(outcome for node, outcome in outcomes.items() if node.partition('::')[0] == path)
            publish(summary, f'- `{path}`: {sum(counts.values())} collected, {counts["passed"]} passed, '
                             f'{counts["failed"]} failed, {counts["skipped"]} skipped, {counts["incomplete"]} incomplete.')
        if (status or not cleaned or unexpected or totals['passed'] != len(expected)
                or observation['collected_nodeids'] != expected or observation['selected_nodeids'] != expected
                or observation['exitstatus'] != status or not observation['session_finished']
                or not observation['collection_completed'] or observation['interrupted']
                or observation['collection_errors'] or observation['internal_errors']):
            raise CheckError('The test outcomes, coverage, or process cleanup failed verification.')
    except CheckError as error:
        publish(summary, f'- Failed at {stage}. {error}')
        return 1
    except (ElementTree.ParseError, KeyError, OSError, TypeError, ValueError) as error:
        publish(summary, f'- Failed at {stage}. Receipt or environment error: {type(error).__name__}.')
        return 1
    finally:
        publish(summary, f'- Collection, execution and cleanup took {monotonic() - started:.2f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
