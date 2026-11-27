"""Run an explicit public backend test group and publish counts from managed receipts."""

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
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
    group = parser.parse_args().group
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', MYRK_TASK_ID=f'github-public-backend-{group}')
    for name in ('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE',
                 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(name, None)
    publish(summary, f'### Public offline backend · {group}')
    publish(summary, 'Selected backend test files on a Windows hosted runner.')
    paths = tuple(f'tests/{name}.py' for name in GROUPS[group])
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
        observation = read_json(report_file(receipt, 'observation.json'))
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
