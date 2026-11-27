"""Run an explicit public backend test group and publish counts from managed receipts."""

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic


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
    arguments = parser.parse_args()
    group = arguments.group
    if arguments.native_only and group != 'judge-fixtures':
        parser.error('--native-only requires the judge-fixtures group')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', MYRK_TASK_ID=f'github-public-backend-{group}')
    for name in ('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE',
                 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(name, None)
    publish(summary, f'### Public offline backend · {group}')
    publish(summary, 'Selected backend test files on a Windows hosted runner.')
    names = ('acceptance/test_judge_native_roundtrip',) if arguments.native_only else GROUPS[group]
    paths = tuple(f'tests/{name}.py' for name in names)
    for path in paths:
        publish(summary, f'- `{path}`')
    started = monotonic()
    stage = 'inventory'
    try:
        inventory = [name for names in GROUPS.values() for name in names]
        if len(inventory) != len(set(inventory)) or any(not (ROOT / path).is_file() for path in paths):
            raise CheckError('A test file is duplicated across groups or missing from this group.')
        stage = 'collection'
        status, receipt, cleaned = run_pytest(summary, environment, stage, paths, '--collect-only', '--myrk-shard=collect')
        if status or not cleaned:
            raise CheckError('Collection or its process cleanup failed.')
        manifest_path = report_file(receipt, 'collection.json')
        manifest = read_json(manifest_path)
        expected = manifest['nodeids']
        counts = Counter(node.partition('::')[0] for node in expected)
        publish(summary, f'- Collected {len(expected)} tests across {len(counts)}/{len(paths)} selected files.')
        if set(counts) != set(paths) or len(expected) != len(set(expected)):
            raise CheckError('The collected files or test identities differ from the selection.')
        stage = 'execution'
        status, receipt, cleaned = run_pytest(summary, environment, stage, paths, '--myrk-shard=other',
                                              f'--myrk-shard-manifest={manifest_path}')
        observation_path = report_file(receipt, 'observation.json')
        observation = read_json(observation_path)
        if status and arguments.native_only:
            publish_judge_diagnostic(summary, observation_path)
        outcomes, unexpected = node_outcomes(expected, observation)
        totals = Counter(outcomes.values())
        publish(summary, f'- Outcomes: {totals["passed"]} passed, {totals["failed"]} failed, '
                         f'{totals["skipped"]} skipped, {totals["incomplete"]} incomplete, {unexpected} unexpected.')
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
    except (KeyError, OSError, TypeError, ValueError) as error:
        publish(summary, f'- Failed at {stage}. Receipt or environment error: {type(error).__name__}.')
        return 1
    finally:
        publish(summary, f'- Collection, execution and cleanup took {monotonic() - started:.2f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
