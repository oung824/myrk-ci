"""Run the frozen audit draw with the actual CPython 3.13.0 standard library."""
import json
import random
import sys


if __name__ == '__main__':
    if sys.implementation.name != 'cpython' or sys.version_info[:3] != (3, 13, 0):
        raise SystemExit('Audit sampling requires the frozen CPython 3.13.0 runtime; use human review.')
    request = json.load(sys.stdin)
    generator = random.Random(20261116)
    selected = []
    groups = {}
    for kind in ('main_answer', 'child_report', 'review_reason', 'memory', 'summary'):
        ids = [row['output_id'] for row in request if row['output_type'] == kind]
        count = min(len(ids), max(3, (len(ids) + 4) // 5))
        drawn = generator.sample(ids, count)
        groups[kind] = {'input_ids': ids, 'N': len(ids), 'base_sample_count': count, 'base_selected_ids': drawn}
        selected.extend(drawn)
    remaining = [row['output_id'] for row in request if row['output_id'] not in selected]
    topup = generator.sample(remaining, max(0, min(len(request), 10) - len(selected)))
    selected.extend(topup)
    print(json.dumps({'algorithm': 'CPython-3.13.0-random.Random.sample', 'seed': 20261116,
        'sampling_runtime': sys.version, 'input_ids': [row['output_id'] for row in request],
        'by_type': groups, 'topup_ids': topup, 'selected_ids': selected, 'actual_sample_count': len(selected)}))
