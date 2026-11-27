"""Quality freezing and human gates through the common acceptance entry."""

import json
from pathlib import Path

import pytest

from tests.support.agent_acceptance.frozen import SOURCE

MATERIALS = Path(__file__).parent / 'materials/quality/synthetic.json'


@pytest.fixture
def quality_source(tmp_path):
    from uuid import uuid4
    import shutil

    root = SOURCE / '.scratch/retro-agent-live-acceptance-20261116/implementation/t20/tmp' / uuid4().hex
    root.mkdir(parents=True)
    from tests.support.agent_acceptance.quality import quality_usage_authority_path
    from tests.support.agent_acceptance import initialize_quality_usage_authority
    if not quality_usage_authority_path().parent.exists():
        initialize_quality_usage_authority()
    drafts = json.loads(MATERIALS.read_bytes())
    for task in drafts:
        task['objective'] += ' [SYNTHETIC experiment ' + root.name + ']'
        for kind in ('input', 'gold'):
            path = root / (task['task_id'] + '.' + kind + '.txt')
            path.write_text(task.pop(kind), encoding='utf-8')
            task[kind + '_path'] = path
    yield root, drafts
    assert root.resolve().is_relative_to(SOURCE / '.scratch/retro-agent-live-acceptance-20261116/implementation/t20/tmp')
    shutil.rmtree(root)


def test_freeze_binds_real_bytes_distinct_families_and_exact_quality_schedule(quality_source):
    from tests.support.agent_acceptance import freeze_quality, quality_material_report

    root, tasks = quality_source
    frozen = freeze_quality(tasks, destination=root / 'frozen.json', provenance='synthetic',
        source_version='synthetic-source-v1', environment={'platform': 'synthetic-local'},
        models={'deepseek': {'model': 'deepseek-flash', 'thinking': False},
                'google': {'model': 'gemini-2.5-flash', 'reasoning': 'low'}},
        criteria_version='quality-synthetic-v1', qualification_families=['judge-holdout-family'])
    report = quality_material_report(frozen)
    assert report['formal_tasks'] == 60
    assert report['tuning_tasks'] == 12
    assert report['distinct_material_families'] == 72
    assert report['proposal'] == {'deepseek': {'executions': 72, 'requests': 2160},
                                  'google': {'executions': 12, 'requests': 360}}
    assert report['different_tasks'] == 72
    assert report['synthetic'] is True
    assert report['real_materials_ready'] is False
    assert report['sending_authorized'] is False
    saved = json.loads(frozen.read_bytes())
    assert len(saved['files']) == 144
    assert saved['tasks'][5]['template'] == '中文约束解释'
    assert saved['tasks'][65]['template'] == '至少3份正常摘要后继续'
    assert all(task['input_path'] != task['gold_path'] for task in saved['tasks'])


def freeze_source(quality_source):
    from tests.support.agent_acceptance import freeze_quality
    root, tasks = quality_source
    return freeze_quality(tasks, destination=root / 'frozen.json', provenance='synthetic',
        source_version='synthetic-source-v1', environment={'platform': 'synthetic-local'},
        models={'deepseek': {'model': 'deepseek-flash', 'thinking': False},
                'google': {'model': 'gemini-2.5-flash', 'reasoning': 'low'}},
        criteria_version='quality-synthetic-v1', qualification_families=['judge-holdout-family'])


def synthetic_registry(frozen):
    from tests.support.agent_acceptance import AcceptanceCase, CaseRegistry
    from tests.support.agent_acceptance.quality import read_quality

    async def synthetic_original(context):
        raise AssertionError('This material scheduling test must not execute or send.')

    value = read_quality(frozen)
    registry = CaseRegistry()
    bindings = (frozen, *(SOURCE / name for name in value.files))
    registry.register(AcceptanceCase('quality.synthetic', 'request-input', ('deepseek', 'google'), synthetic_original,
        bindings=bindings, frozen_material_input=True, owner_ticket='20', output_standards={}))
    return registry, {task.task_id: 'quality.synthetic' for task in value.tasks}


def test_quality_plan_reuses_frozen_execution_parent_caps_and_evaluator_only_gold(quality_source, tmp_path):
    from tests.support.agent_acceptance import FrozenParentBudget, quality_plan

    frozen = freeze_source(quality_source)
    registry, adapters = synthetic_registry(frozen)
    parent = FrozenParentBudget(parent_id='synthetic-quality-envelope', accounting_root=tmp_path / 'prepared',
        request_cap=2520, phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    plan = quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent, mode='offline')
    assert len(plan.executions) == 84
    assert [row.scenario_id for row in plan.executions[:2]] == ['QG1.tuning.1', 'QG1.tuning.2']
    assert sum(row.request_cap for row in plan.executions if row.protocol == 'deepseek') == 2160
    assert sum(row.request_cap for row in plan.executions if row.protocol == 'google') == 360
    assert sum(row.cost_cap_microusd for row in plan.executions) == 39076920
    assert {row.scenario_id for row in plan.executions if row.protocol == 'google'} == {
        f'QG{group}.formal.{slot}' for group in range(1, 7) for slot in (3, 9)}
    assert len({row.execution_id for row in plan.executions}) == len({row.startpoint for row in plan.executions}) == 84
    assert plan.parent_budget is parent
    assert all(row.stage == 'quality' for row in plan.executions)
    registry.select(plan)
    registry.cases.clear()
    with pytest.raises(ValueError, match='adapter'):
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)


@pytest.fixture
def quality_run(quality_source, tmp_path, request):
    import asyncio
    from dataclasses import replace
    from myrk.conversation import Conversation, ConversationRecord, Task
    from tests.support.agent_acceptance import (CaseOutcome, FrozenParentBudget, Reply, export_quality_review, quality_plan, run_plan)
    from tests.support.agent_acceptance.quality import read_quality

    for task in quality_source[1]:
        task.update(getattr(request, 'param', {}).get(task['task_id'], {}))
    frozen = freeze_source(quality_source)
    registry, adapters = synthetic_registry(frozen)
    if '__reference_models__' in getattr(request, 'param', {}):
        from tests.support.agent_acceptance import freeze_quality
        current = read_quality(frozen)
        older_models = json.loads(json.dumps(current.models))
        if request.param['__reference_models__'] == 'different':
            older_models['deepseek']['model'] = 'synthetic-other-model'
        alternate = freeze_quality(quality_source[1], destination=frozen.parent / 'alternate-freeze.json', provenance='synthetic',
            source_version='synthetic-older-source', environment=current.environment, models=older_models,
            criteria_version=current.criteria_version, qualification_families=[])
        registry.cases['quality.synthetic'] = replace(registry.cases['quality.synthetic'], bindings=(*registry.cases['quality.synthetic'].bindings, alternate))

    async def original(context):
        expected = 'SYNTHETIC workflow output for ' + context.execution.scenario_id
        session = context.session([Reply(text=expected)])
        conversation = Conversation(context.root / 'conversation.json')
        task = Task(goal=context.material_input, scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        exchange = session.save(context.root / 'exchange.json', attempts=[a.model_dump(mode='json') for t in record.turns for a in t.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        facts = {'quality_compatibility': {'protocol': 'passed', 'settings': 'passed'}}
        domain = getattr(request, 'param', {}).get('__domain_actual__', getattr(request, 'param', {}).get('__domain_standards__', {})).get(context.execution.scenario_id)
        if domain is not None:
            facts['output_standard'] = domain
            facts['output_domain_criteria'] = {'version': domain['version'], 'criteria': domain, 'provenance': 'synthetic'}
        return CaseOutcome('passed' if turn.answer == expected else 'failed', facts,
            {'conversation': conversation.path, 'exchange': exchange}, target_status='passed', deterministic_status='passed')

    registry.cases['quality.synthetic'] = replace(registry.cases['quality.synthetic'], run=original)
    for task_id, standard in getattr(request, 'param', {}).get('__domain_standards__', {}).items():
        case = replace(registry.cases['quality.synthetic'], case_id='quality.synthetic-domain-' + task_id.lower(), output_standards={'main_answer': standard})
        registry.register(case)
        adapters[task_id] = case.case_id
    parent = FrozenParentBudget(parent_id='synthetic-quality-run', accounting_root=tmp_path / 'prepared',
        request_cap=2520, phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    plan = quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent, mode='offline')
    import os
    proof_root = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY']) / 'reports/quality-workflow' if request.node.name == 'test_synthetic_manual_forms_import_all_originals_without_creating_human_reviews' else tmp_path
    if getattr(request, 'param', {}).get('__forget_declarations__'):
        registry.cases = {name: replace(case, output_standards=None) for name, case in registry.cases.items()}
    result = asyncio.run(run_plan(plan, root=proof_root / 'run', registry=registry))
    assert len(result.cases) == 84 and all(case.product_status == 'passed' for case in result.cases)
    package = None if getattr(request, 'param', {}).get('__defer_export__') else export_quality_review(frozen, result.result_path, proof_root / 'review')
    yield frozen, package, result


def synthetic_forms(package, *, failed=(), unknown=()):
    from tests.support.agent_acceptance import SyntheticQualityReview
    value = json.loads(package.read_bytes())
    forms = []
    for output in value['outputs']:
        identity = output['execution_id']
        verdict = 'failed' if identity in failed else 'unknown' if identity in unknown else 'passed'
        dimensions = {name: 'passed' for name in output['standard']['dimensions']}
        dimensions['sufficiency'] = verdict
        forms.append(SyntheticQualityReview(review_id='synthetic-' + str(len(forms)), output_id=output['output_id'],
            authorship='synthetic', package_sha256=value['binding'], criteria_version=output['standard']['version'],
            output_sha256=output['sha256'], reviewed_originals=output['required_originals'], conclusions=dimensions,
            reasons={} if verdict == 'passed' else {'sufficiency': 'Explicit synthetic boundary verdict.'}))
    return forms


def test_synthetic_manual_forms_import_all_originals_without_creating_human_reviews(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, result = quality_run
    original = result.result_path.read_bytes()
    forms = synthetic_forms(package)
    imported = import_quality_reviews(frozen, package, forms, synthetic_destination=package.parent / 'synthetic-import.json')
    report = quality_report(frozen, package, synthetic_reviews=imported)
    assert report['synthetic_validation']['deepseek']['passed'] == 60
    assert report['synthetic_validation']['google']['passed'] == 12
    assert report['synthetic_validation']['qualified'] is True
    (package.parent / 'quality-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert report['actual_human_reviews'] == 0
    assert report['real_qualification'] == 'not_measured'
    assert report['actual_baseline'] == 'not_established'
    assert report['user_acceptance'] == 'pending'
    assert not list((package.parent / 'reviews').glob('*.json'))
    assert result.result_path.read_bytes() == original
    assert all(case.semantic_status == 'not_measured' for case in result.cases)
    wire = result.cases[12].evidence['exchange'].read_text(encoding='utf-8')
    assert 'SYNTHETIC GOLD' not in wire
    assert 'SYNTHETIC ORIGINAL' in wire


def test_quality_threshold_boundaries_keep_failures_unknowns_and_gemini_contracts_separate(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, _ = quality_run
    scenarios = [
        ('ds57-one-group7', [f'quality:deepseek:QG1.formal.{n}:1' for n in (1, 2, 3)], [], 57, False, True),
        ('ds54-balanced', [f'quality:deepseek:QG{g}.formal.1:1' for g in range(1, 7)], [], 54, True, True),
        ('ds54-required-unknown', [f'quality:deepseek:QG{g}.formal.1:1' for g in range(1, 6)],
         ['quality:deepseek:QG6.formal.2:1'], 54, False, False),
        ('gem10-selected-failures', ['quality:google:QG1.formal.3:1', 'quality:google:QG2.formal.9:1'], [], 60, True, True),
        ('gem10-one-group0', ['quality:google:QG1.formal.3:1', 'quality:google:QG1.formal.9:1'], [], 60, False, True),
    ]
    for name, failed, unknown, ds_passed, qualifies, complete in scenarios:
        forms = synthetic_forms(package, failed=failed, unknown=unknown)
        imported = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / (name + '.json'))
        report = quality_report(frozen, package, synthetic_reviews=imported)['synthetic_validation']
        assert report['deepseek']['planned'] == 60
        assert report['deepseek']['passed'] == ds_passed
        assert report['deepseek']['complete'] is complete
        assert report['qualified'] is qualifies, name
        assert report['google']['compatibility_passed'] is True
        assert report['deepseek']['planned'] == sum(report['deepseek'][status] for status in ('passed', 'failed', 'unknown', 'invalid', 'not_executed'))


def test_quality_reference_is_immutable_matches_materials_and_uses_integer_regression_limits(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report, save_quality_reference
    frozen, package, _ = quality_run
    baseline_forms = synthetic_forms(package, failed=[f'quality:deepseek:QG{group}.formal.1:1' for group in (4, 5, 6)])
    baseline_reviews = import_quality_reviews(frozen, package, baseline_forms, synthetic_destination=tmp_path / 'baseline-forms.json')
    reference = save_quality_reference(frozen, package, destination=tmp_path / 'synthetic-reference.json', synthetic_reviews=baseline_reviews)
    before = reference.read_bytes()
    forms = synthetic_forms(package, failed=[f'quality:deepseek:QG{group}.formal.1:1' for group in range(1, 7)] +
        ['quality:google:QG1.formal.3:1', 'quality:google:QG2.formal.9:1'])
    reviews = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / 'current-forms.json')
    report = quality_report(frozen, package, synthetic_reviews=reviews, baseline=reference)
    assert report['comparison']['status'] == 'matched'
    assert report['comparison']['deepseek']['net_change'] == -3
    assert report['comparison']['deepseek']['within_limits'] is True
    assert report['comparison']['google']['net_change'] == -2
    assert report['comparison']['google']['regression_blocking_rule'] is False
    assert report['synthetic_validation']['qualified'] is True
    assert len(report['comparison']['google']['item_changes']) == 12
    group_failures = ['quality:deepseek:QG1.formal.1:1', 'quality:deepseek:QG1.formal.2:1',
        'quality:deepseek:QG4.formal.1:1', 'quality:deepseek:QG4.formal.2:1',
        'quality:deepseek:QG5.formal.1:1', 'quality:deepseek:QG6.formal.1:1']
    group_reviews = import_quality_reviews(frozen, package, synthetic_forms(package, failed=group_failures), synthetic_destination=tmp_path / 'group-drop.json')
    group_report = quality_report(frozen, package, synthetic_reviews=group_reviews, baseline=reference)
    assert group_report['synthetic_validation']['deepseek']['passed'] == 54
    assert group_report['synthetic_validation']['deepseek']['absolute_passed'] is True
    assert group_report['comparison']['deepseek']['net_change'] == -3
    assert group_report['comparison']['deepseek']['group_changes']['QG1'] == -2
    assert group_report['synthetic_validation']['qualified'] is False
    all_reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'all-pass.json')
    all_reference = save_quality_reference(frozen, package, destination=tmp_path / 'all-pass-reference.json', synthetic_reviews=all_reviews)
    net_reviews = import_quality_reviews(frozen, package, synthetic_forms(package, failed=[f'quality:deepseek:QG{g}.formal.1:1' for g in (1, 2, 3, 4)]), synthetic_destination=tmp_path / 'net-drop.json')
    net_report = quality_report(frozen, package, synthetic_reviews=net_reviews, baseline=all_reference)
    assert net_report['synthetic_validation']['deepseek']['passed'] == 56
    assert net_report['synthetic_validation']['deepseek']['absolute_passed'] is True
    assert net_report['comparison']['deepseek']['net_change'] == -4
    assert net_report['synthetic_validation']['qualified'] is False
    assert reference.read_bytes() == before
    with pytest.raises(FileExistsError):
        save_quality_reference(frozen, package, destination=reference, synthetic_reviews=reviews)
    with pytest.raises(ValueError, match='actual'):
        save_quality_reference(frozen, package, destination=tmp_path / 'forged-actual.json')


def test_formal_material_used_for_a_fix_becomes_regression_without_erasing_original_results(quality_run, tmp_path):
    from tests.support.agent_acceptance import (import_quality_reviews, quality_material_report, quality_report,
        record_quality_adjustment, quality_plan, FrozenParentBudget)
    frozen, package, _ = quality_run
    forms = synthetic_forms(package)
    reviews = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / 'forms.json')
    evidence = frozen.parent / 'fix-decision.txt'
    evidence.write_text('SYNTHETIC: QG2 formal 4 was used to change a parser implementation.', encoding='utf-8')
    original_freeze = frozen.read_bytes()
    record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='implementation', evidence=evidence)
    material = quality_material_report(frozen)
    assert material['regression_tasks'] == ['QG2.formal.4']
    assert material['formal_unseen_eligible'] is False
    report = quality_report(frozen, package, synthetic_reviews=reviews)
    assert report['synthetic_validation']['deepseek']['passed'] == 60
    assert report['synthetic_validation']['qualified'] is False
    assert frozen.read_bytes() == original_freeze
    registry, adapters = synthetic_registry(frozen)
    parent = FrozenParentBudget(parent_id='synthetic-no-reuse', accounting_root=tmp_path / 'prepared', request_cap=2520,
        phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    with pytest.raises(ValueError, match='regression'):
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)


def test_quality_export_includes_frozen_gold_authorization_and_scoring_bytes_only_for_review(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_quality_review
    frozen, _, result = quality_run
    package = export_quality_review(frozen, result.result_path, tmp_path / 'quality-export')
    data = json.loads(package.read_bytes())
    frozen_data = json.loads(frozen.read_bytes())
    assert len(data['outputs']) == 84
    for output in data['outputs']:
        labels = output['required_originals']
        assert any(label.endswith('/quality.gold') for label in labels)
        assert any(label.endswith('/quality.freeze') for label in labels)
        assert output['standard'] == frozen_data['standards'][output['type']]
        gold = next(data['originals'][label] for label in labels if label.endswith('/quality.gold'))
        assert 'SYNTHETIC' in (package.parent / gold['path']).read_text(encoding='utf-8')
        assert 'GOLD' not in json.dumps(output['actual_input'])


def test_shared_screening_is_one_preplanned_generation_with_stricter_cap_and_original_quality_denominator(quality_source, tmp_path):
    from dataclasses import replace
    from hashlib import sha256
    from tests.support.agent_acceptance import FrozenExecution, FrozenParentBudget, SharedCoverage, quality_plan
    from tests.support.agent_acceptance.quality import read_quality
    frozen = freeze_source(quality_source)
    registry, adapters = synthetic_registry(frozen)
    registry.cases['quality.synthetic'] = replace(registry.cases['quality.synthetic'],
        screening={'deepseek': {'scenario_id': 'D-synthetic', 'request_cap': 4}})
    value = read_quality(frozen)
    task = value.tasks[0]
    alias = SharedCoverage(scenario_id=task.task_id, material_family_id=task.material_family_id,
        material_sha256=value.files[task.input_path.as_posix()], request_cap=24, criteria_path=frozen,
        criteria_sha256=sha256(frozen.read_bytes()).hexdigest(), never_tuned=True,
        applicability_reason='Preplanned same source version, same untouched original and complete ordinary screening branch.')
    shared = FrozenExecution(execution_id='one-generation-two-coverages', case_id='quality.synthetic', protocol='deepseek',
        scenario_id='D-synthetic', material_family_id=task.material_family_id, material_variant='baseline',
        material_path=task.input_path, material_sha256=value.files[task.input_path.as_posix()], startpoint='shared-original',
        order=1, stage='screening', request_cap=4, cost_cap_microusd=58984, shared_with=(alias,))
    parent = FrozenParentBudget(parent_id='synthetic-sharing', accounting_root=tmp_path / 'prepared', request_cap=2520,
        phase_microusd={'screening': 58984, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    plan = quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent, mode='offline', screening=(shared,))
    assert len(plan.executions) == 84
    assert sum(row.request_cap for row in plan.executions) == 2500
    assert sum(row.scenario_id == 'QG1.formal.1' for row in plan.executions) == 0
    assert sum(len(row.shared_with) for row in plan.executions) == 1
    assert plan.executions[0].request_cap == 4
    assert sum(row.stage == 'quality' for row in plan.executions) + sum(len(row.shared_with) for row in plan.executions) == 84
    assert len({row.execution_id for row in plan.executions}) == 84
    bad = shared.model_copy(update={'shared_with': (alias.model_copy(update={'scenario_id': 'QG1.tuning.1'}),)})
    with pytest.raises(ValueError, match='formal'):
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent, screening=(bad,))


def test_missing_review_gold_and_wrong_execution_mapping_are_rejected_before_import(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_review, quality_report, import_quality_reviews
    from tests.support.agent_acceptance.output_review import ReviewOriginal
    from tests.support.agent_acceptance.quality import read_quality
    frozen, _, result = quality_run
    value = read_quality(frozen)
    missing = export_review(result.result_path, tmp_path / 'missing-context', standards=value.standards)
    with pytest.raises(ValueError, match='gold|review-only'):
        quality_report(frozen, missing)
    with pytest.raises(ValueError, match='gold|review-only'):
        import_quality_reviews(frozen, missing, synthetic_forms(missing), synthetic_destination=tmp_path / 'must-not-exist.json')
    assert not (tmp_path / 'must-not-exist.json').exists()
    with pytest.raises(ValueError, match='execution'):
        export_review(result.result_path, tmp_path / 'wrong-execution', review_originals={
            'not-in-original-plan': {'gold': ReviewOriginal(path=frozen, sha256='0' * 64)}})
    assert not (tmp_path / 'wrong-execution').exists()
    with pytest.raises(ValueError, match='changed'):
        export_review(result.result_path, tmp_path / 'wrong-sha', review_originals={
            result.cases[0].execution_id: {'gold': ReviewOriginal(path=frozen, sha256='0' * 64)}})
    assert not (tmp_path / 'wrong-sha').exists()


def test_candidate_source_drift_requires_same_parent_stage_refreeze_and_preserves_closed_original(prepared_root, quality_source):
    from dataclasses import replace
    from tests.support.agent_acceptance import (FrozenParentBudget, prepare_live, quality_plan, quality_candidate_status)
    frozen = freeze_source(quality_source)
    registry, adapters = synthetic_registry(frozen)
    prompt = frozen.parent / 'candidate-prompt.txt'
    prompt.write_text('SYNTHETIC pre-tuning candidate prompt.', encoding='utf-8')
    registry.cases['quality.synthetic'] = replace(registry.cases['quality.synthetic'],
        bindings=(*registry.cases['quality.synthetic'].bindings, prompt))
    parent = FrozenParentBudget(parent_id='synthetic-candidate-' + prepared_root.name, accounting_root=prepared_root,
        request_cap=2520, phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    plan = quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)
    prepare_live(plan, root=prepared_root, registry=registry)
    old_bundle = (prepared_root / 'prepared.json').read_bytes()
    old_ledger = (prepared_root / 'ledger.json').read_bytes()
    assert quality_candidate_status(prepared_root, registry=registry)['state'] == 'frozen'
    prompt.write_text('SYNTHETIC candidate prompt adjusted after independent tuning.', encoding='utf-8')
    state = quality_candidate_status(prepared_root, registry=registry)
    assert state['state'] == 'candidate_refreeze_required'
    assert state['sending_authorized'] is False
    assert state['stage_transition_implemented'] is True
    assert state['owner_ticket'] == '24'
    assert (prepared_root / 'prepared.json').read_bytes() == old_bundle
    assert (prepared_root / 'ledger.json').read_bytes() == old_ledger
    assert json.loads(old_bundle)['plan']['parent_budget']['parent_id'] == parent.parent_id


@pytest.mark.parametrize('quality_run', [{'QG6.formal.6': {'required_outputs': {'main_answer': 1, 'summary': 3}}}], indirect=True)
def test_missing_required_summaries_cannot_be_hidden_by_a_passing_main_answer(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, _ = quality_run
    forms = synthetic_forms(package)
    reviews = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / 'forms.json')
    report = quality_report(frozen, package, synthetic_reviews=reviews)['synthetic_validation']
    assert report['deepseek']['passed'] == 59
    assert report['deepseek']['absolute_passed'] is True
    assert report['deepseek']['unknown'] == 1
    assert report['deepseek']['complete'] is False
    assert report['qualified'] is False
    row = next(row for row in report['executions'] if row['task_id'] == 'QG6.formal.6')
    assert 'missing_required_output_type:summary:3' in row['gaps']

@pytest.mark.parametrize('quality_run', [{'QG2.formal.3': {'compatibility_contracts': ['protocol', 'settings', 'tool_roundtrip']}}], indirect=True)
def test_applicable_gemini_tool_roundtrip_is_required_even_when_general_quality_hits_the_line(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, _ = quality_run
    reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'forms.json')
    report = quality_report(frozen, package, synthetic_reviews=reviews)['synthetic_validation']
    assert report['google']['passed'] == 11
    assert report['google']['absolute_passed'] is True
    assert report['google']['compatibility_passed'] is False
    assert report['google']['complete'] is False
    assert report['qualified'] is False


def test_formal_plan_rejects_declared_models_or_environment_without_the_frozen_native_route_binding(quality_source, tmp_path):
    from tests.support.agent_acceptance import freeze_quality, quality_plan, FrozenParentBudget
    root, tasks = quality_source
    frozen = freeze_quality(tasks, destination=root / 'formal-candidate.json', provenance='formal',
        source_version='candidate-never-executed', environment={'platform': 'unverified-environment-label'},
        models={'deepseek': {'model': 'wrong-deepseek-alias'}, 'google': {'model': 'gemini-2.5-flash'}},
        criteria_version='quality-test-v1', qualification_families=[])
    registry, adapters = synthetic_registry(frozen)
    parent = FrozenParentBudget(parent_id='synthetic-reject-models', accounting_root=tmp_path / 'prepared', request_cap=2520,
        phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    with pytest.raises(ValueError, match='model|environment'):
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)
    assert not (tmp_path / 'prepared').exists()


def test_material_readiness_requires_the_public_execution_adapters_and_keeps_synthetic_status(quality_source, tmp_path):
    from tests.support.agent_acceptance import FrozenParentBudget, quality_plan, quality_material_report
    frozen = freeze_source(quality_source)
    registry, adapters = synthetic_registry(frozen)
    parent = FrozenParentBudget(parent_id='synthetic-ready', accounting_root=tmp_path / 'prepared', request_cap=2520,
        phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    assert quality_material_report(frozen)['execution_adapter_status'] == 'not_verified'
    plan = quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)
    report = quality_material_report(frozen, plan=plan, registry=registry)
    assert report['execution_adapter_status'] == 'verified'
    assert report['synthetic_materials_ready'] is True
    assert report['real_materials_ready'] is False
    assert report['sending_authorized'] is False

@pytest.mark.parametrize('defect', ['family', 'renamed-input', 'template', 'used-formal', 'judge-family', 'shapes', 'missing', 'gold-in-input'])
def test_material_freeze_rejects_missing_reused_or_nonindependent_originals(quality_source, defect):
    root, tasks = quality_source
    if defect == 'family': tasks[1]['material_family_id'] = tasks[0]['material_family_id']
    if defect == 'renamed-input': tasks[1]['input_path'].write_bytes(tasks[0]['input_path'].read_bytes())
    if defect == 'template': tasks[0]['template'] = 'Renamed unrelated template'
    if defect == 'used-formal': tasks[0]['prior_uses'] = ['implementation']
    if defect == 'judge-family': tasks[0]['material_family_id'] = 'judge-holdout-family'
    if defect == 'shapes':
        for task in tasks:
            if task['group'] == 'QG1': task['shape'] = 'single-renamed-shape'
    if defect == 'missing': tasks[0]['input_path'].unlink()
    if defect == 'gold-in-input': tasks[0]['input_path'].write_bytes(tasks[0]['gold_path'].read_bytes())
    with pytest.raises((ValueError, OSError)):
        freeze_source((root, tasks))
    assert not (root / 'frozen.json').exists()


def test_prior_read_only_quality_review_does_not_become_tuning_or_forge_historical_applicability(quality_source):
    from tests.support.agent_acceptance import quality_material_report
    root, tasks = quality_source
    tasks[0]['prior_uses'] = ['quality_execution', 'read_only_review', 'historical_reuse']
    frozen = freeze_source((root, tasks))
    report = quality_material_report(frozen)
    assert report['formal_unseen_eligible'] is True
    assert report['real_materials_ready'] is False
    assert report['sending_authorized'] is False



def test_critical_failure_is_not_averaged_into_general_quality(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, _ = quality_run
    forms = synthetic_forms(package)
    selected = next(index for index, form in enumerate(forms) if ':QG1.formal.1:' in form.output_id)
    form = forms[selected]
    forms[selected] = form.model_copy(update={'conclusions': {**form.conclusions, 'K1': 'failed'},
        'reasons': {'K1': 'SYNTHETIC critical authorization failure; no actual unauthorized action occurred.'}})
    reviews = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / 'critical.json')
    report = quality_report(frozen, package, synthetic_reviews=reviews)['synthetic_validation']
    assert report['deepseek']['passed'] == 59
    assert report['deepseek']['absolute_passed'] is True
    assert report['deepseek']['critical_passed'] is False
    assert report['qualified'] is False


def test_relabeling_a_synthetic_reference_cannot_establish_an_actual_human_baseline(quality_run, tmp_path):
    from hashlib import sha256
    from tests.support.agent_acceptance import import_quality_reviews, quality_report, save_quality_reference
    frozen, package, _ = quality_run
    reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'forms.json')
    reference = save_quality_reference(frozen, package, destination=tmp_path / 'reference.json', synthetic_reviews=reviews)
    body = json.loads(reference.read_bytes())
    body['kind'] = 'actual'
    body.pop('binding')
    body['binding'] = sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    forged = tmp_path / 'relabelled-reference.json'
    forged.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='actual|human'):
        quality_report(frozen, package, synthetic_reviews=reviews, baseline=forged)


@pytest.mark.parametrize('quality_run', [{'__reference_models__': 'same'}, {'__reference_models__': 'different'}], indirect=True)
def test_code_version_can_change_but_unmatched_models_require_separate_old_version_costs(quality_run, tmp_path, request):
    from tests.support.agent_acceptance import export_quality_review, import_quality_reviews, save_quality_reference, quality_report
    frozen, package, result = quality_run
    alternate = frozen.parent / 'alternate-freeze.json'
    older_package = export_quality_review(alternate, result.result_path, tmp_path / 'older-review')
    older_reviews = import_quality_reviews(alternate, older_package, synthetic_forms(older_package), synthetic_destination=tmp_path / 'older-forms.json')
    reference = save_quality_reference(alternate, older_package, destination=tmp_path / 'older-reference.json', synthetic_reviews=older_reviews)
    current_reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'current-forms.json')
    report = quality_report(frozen, package, synthetic_reviews=current_reviews, baseline=reference)
    assert report['comparison']['baseline_source_version'] == 'synthetic-older-source'
    assert report['comparison']['current_source_version'] == 'synthetic-source-v1'
    different = request.node.callspec.params['quality_run']['__reference_models__'] == 'different'
    assert report['comparison']['status'] == ('unmatched' if different else 'matched')
    if different:
        proposal = report['comparison']['old_version_execution_proposal']
        assert proposal == {'executions': 60, 'protocol': 'deepseek', 'requests': 1800, 'cost_cap_microusd': 26542800,
                            'included_in_current_budget': False, 'sending_authorized': False}
    else:
        assert report['comparison']['status'] == 'matched'
        assert report['comparison']['deepseek']['net_change'] == 0
    assert report['real_qualification'] == 'not_measured'


def test_a_caller_only_registry_cannot_claim_real_formal_material_readiness(quality_source, tmp_path):
    from dataclasses import replace
    from tests.support.agent_acceptance import freeze_quality, quality_plan, quality_model_settings, FrozenParentBudget
    from tests.support.agent_acceptance.reporting import environment_facts
    root, tasks = quality_source
    frozen = freeze_quality(tasks, destination=root / 'formal-bridge.json', provenance='formal',
        source_version='SYNTHETIC metadata boundary fixture; never a formal execution', environment=environment_facts(),
        models=quality_model_settings(), criteria_version='quality-test-v1', qualification_families=[])
    registry, adapters = synthetic_registry(frozen)
    registry.cases['quality.synthetic'] = replace(registry.cases['quality.synthetic'], formal_live=True)
    parent = FrozenParentBudget(parent_id='synthetic-no-formal-rebuild', accounting_root=tmp_path / 'prepared', request_cap=2520,
        phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
    with pytest.raises(ValueError, match='formal|rebuild|adapter'):
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)
    assert not (tmp_path / 'prepared').exists()

@pytest.mark.parametrize('quality_run', [{'__reference_models__': 'same'}], indirect=True)
@pytest.mark.parametrize('usage_state', ['unused', 'adjusted', 'unavailable'])
def test_review_and_reference_survive_retiring_source_materials_but_copies_cannot_execute(quality_run, tmp_path, usage_state):
    from tests.support.agent_acceptance import (export_quality_review, import_quality_reviews, save_quality_reference,
        quality_report, quality_material_report)
    frozen, package, result = quality_run
    alternate = frozen.parent / 'alternate-freeze.json'
    old_package = export_quality_review(alternate, result.result_path, tmp_path / 'old-review')
    old_forms = import_quality_reviews(alternate, old_package, synthetic_forms(old_package), synthetic_destination=tmp_path / 'old-forms.json')
    reference = save_quality_reference(alternate, old_package, destination=tmp_path / 'old-reference.json', synthetic_reviews=old_forms)
    current_forms = synthetic_forms(package)
    data = json.loads(package.read_bytes())
    context = next(iter(data['review_originals'].values()))['quality.freeze']['original_key']
    packaged_freeze = package.parent / data['originals'][context]['path']
    reference_bytes = reference.read_bytes()
    if usage_state == 'adjusted':
        from tests.support.agent_acceptance import record_quality_adjustment
        decision = frozen.parent / 'later-adjustment.txt'
        decision.write_text('SYNTHETIC: use the original QG2 formal 4 input and goal after accepting the reference.', encoding='utf-8')
        record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='threshold', evidence=decision)
        decision.unlink()
    source_files = json.loads(frozen.read_bytes())['files']
    for name in source_files:
        source = SOURCE / name
        assert source.resolve().is_relative_to(frozen.parent.resolve())
        source.unlink()
    alternate.unlink()
    frozen.unlink()
    imported = import_quality_reviews(packaged_freeze, package, current_forms, synthetic_destination=tmp_path / 'after-retirement.json')
    from tests.support.agent_acceptance.quality import quality_usage_authority_path
    index = quality_usage_authority_path().parent / 'index.json'
    held_index = index.with_name('index-portability-test.held')
    if usage_state == 'unavailable':
        index.rename(held_index)
    try:
        report = quality_report(packaged_freeze, package, synthetic_reviews=imported, baseline=reference)
        if usage_state == 'unavailable':
            with pytest.raises(ValueError, match='actual baseline'):
                save_quality_reference(packaged_freeze, package, destination=tmp_path / 'unknown-reference.json', synthetic_reviews=imported)
    finally:
        if usage_state == 'unavailable':
            held_index.rename(index)
    assert report['comparison']['status'] == 'matched'
    assert report['comparison']['baseline_usage_scope'] == 'historical_as_of'
    assert reference.read_bytes() == reference_bytes
    assert json.loads(reference_bytes)['results']['qualified'] is True
    assert report['synthetic_validation']['qualified'] is (usage_state == 'unused')
    assert report['synthetic_validation']['deepseek']['passed'] == 60
    assert report['synthetic_validation']['deepseek']['planned'] == 60
    assert report['materials']['regression_tasks'] == (['QG2.formal.4'] if usage_state == 'adjusted' else [])
    if usage_state == 'unavailable':
        assert report['material_usage']['status'] == 'unknown'
        assert report['materials']['formal_unseen_eligible'] is None
    assert report['actual_human_reviews'] == 0
    assert report['real_qualification'] == 'not_measured'
    assert report['materials']['source_materials_verified'] is False
    assert report['materials']['real_materials_ready'] is False
    with pytest.raises((ValueError, OSError)):
        quality_material_report(packaged_freeze)


DOMAIN_STANDARD = {'version': 'SYNTHETIC-readonly-domain-v1', 'dimensions': {
    'readonly_boundary': {'description': 'SYNTHETIC read-only review boundary; no actual review action is simulated as human.',
                          'applicable': True, 'critical': True}}}


@pytest.mark.parametrize('quality_run', [{'QG3.formal.5': {'domain_standards': {'main_answer': DOMAIN_STANDARD}},
    '__domain_standards__': {'QG3.formal.5': DOMAIN_STANDARD}}], indirect=True)
def test_per_task_frozen_domain_standard_preserves_critical_dimensions_in_human_forms(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report
    frozen, package, _ = quality_run
    data = json.loads(package.read_bytes())
    domain_output = next(row for row in data['outputs'] if ':QG3.formal.5:' in row['execution_id'])
    ordinary_output = next(row for row in data['outputs'] if ':QG3.formal.4:' in row['execution_id'])
    definition = domain_output['standard']['dimensions']['readonly_boundary']
    assert definition == DOMAIN_STANDARD['dimensions']['readonly_boundary']
    assert domain_output['standard']['domain_version'] == DOMAIN_STANDARD['version']
    assert 'readonly_boundary' not in ordinary_output['standard']['dimensions']
    assert domain_output['domain_criteria']['version'] == DOMAIN_STANDARD['version']
    forms = synthetic_forms(package)
    index = next(index for index, form in enumerate(forms) if form.output_id == domain_output['output_id'])
    forms[index] = forms[index].model_copy(update={'conclusions': {**forms[index].conclusions, 'readonly_boundary': 'failed'},
        'reasons': {'readonly_boundary': 'SYNTHETIC critical domain verdict; no actual violation occurred.'}})
    reviews = import_quality_reviews(frozen, package, forms, synthetic_destination=tmp_path / 'domain-forms.json')
    report = quality_report(frozen, package, synthetic_reviews=reviews)['synthetic_validation']
    assert report['deepseek']['passed'] == 59
    assert report['deepseek']['absolute_passed'] is True
    assert report['deepseek']['critical_passed'] is False
    assert report['qualified'] is False


@pytest.mark.parametrize('defect', ['unknown', 'missing', 'version', 'critical', 'base-critical'])
def test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending(quality_source, tmp_path, defect):
    from dataclasses import replace
    from tests.support.agent_acceptance import FrozenParentBudget, quality_plan
    root, tasks = quality_source
    frozen_domain = json.loads(json.dumps(DOMAIN_STANDARD))
    if defect == 'version': frozen_domain['version'] = 'different-domain-v0'
    if defect == 'critical': frozen_domain['dimensions']['readonly_boundary']['critical'] = False
    if defect == 'base-critical': frozen_domain['dimensions']['K2'] = {'description': 'Invalid downgrade', 'applicable': True, 'critical': False}
    task = next(task for task in tasks if task['task_id'] == 'QG3.formal.5')
    if defect not in {'unknown', 'missing'}:
        task['domain_standards'] = {'main_answer': frozen_domain}
    with pytest.raises(ValueError, match='standard|critical|declared'):
        frozen = freeze_source((root, tasks))
        registry, adapters = synthetic_registry(frozen)
        registry.register(replace(registry.cases['quality.synthetic'], case_id='quality.synthetic-domain',
            output_standards=None if defect == 'unknown' else {'main_answer': DOMAIN_STANDARD}))
        adapters['QG3.formal.5'] = 'quality.synthetic-domain'
        parent = FrozenParentBudget(parent_id='synthetic-domain-reject', accounting_root=tmp_path / 'never-prepared', request_cap=2520,
            phase_microusd={'screening': 0, 'quality': 39076920, 'judge': 0}, reserve_microusd=0, profile='synthetic')
        quality_plan(frozen, registry=registry, adapters=adapters, parent_budget=parent)
    assert not (tmp_path / 'never-prepared').exists()


@pytest.mark.parametrize('quality_run', [{'QG3.formal.5': {'domain_standards': {'main_answer': DOMAIN_STANDARD}},
    '__domain_standards__': {'QG3.formal.5': DOMAIN_STANDARD},
    '__domain_actual__': {'QG3.formal.5': {**DOMAIN_STANDARD, 'version': 'unexpected-runtime-version'}}, '__defer_export__': True}], indirect=True)
def test_actual_domain_standard_drift_does_not_create_a_ready_review_package(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_quality_review
    frozen, _, result = quality_run
    with pytest.raises(ValueError, match='drifted|conflicting|Unfrozen'):
        export_quality_review(frozen, result.result_path, tmp_path / 'must-not-exist')
    assert not (tmp_path / 'must-not-exist').exists()


@pytest.mark.parametrize('quality_run', [{'QG3.formal.5': {'domain_standards': {'main_answer': DOMAIN_STANDARD}},
    '__domain_standards__': {'QG3.formal.5': DOMAIN_STANDARD}}], indirect=True)
def test_domain_dimensions_cannot_be_added_after_observing_the_original_output(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_quality_review, freeze_quality
    from tests.support.agent_acceptance.quality import read_quality
    frozen, _, result = quality_run
    value = read_quality(frozen)
    tasks = [task.model_dump(mode='python') for task in value.tasks]
    task = next(task for task in tasks if task['task_id'] == 'QG3.formal.5')
    task['domain_standards']['main_answer']['dimensions']['post_seen_extra'] = {'description': 'SYNTHETIC late criterion', 'applicable': True}
    late = freeze_quality(tasks, destination=frozen.parent / 'late-standard.json', provenance='synthetic', source_version=value.source_version,
        environment=value.environment, models=value.models, criteria_version=value.criteria_version, qualification_families=[])
    with pytest.raises(ValueError, match='before|generation|frozen'):
        export_quality_review(late, result.result_path, tmp_path / 'late-must-not-exist')
    assert not (tmp_path / 'late-must-not-exist').exists()


@pytest.mark.parametrize('quality_run', [{'__defer_export__': True, '__forget_declarations__': True}], indirect=True)
def test_legacy_undeclared_adapter_cannot_be_relabelled_common_only_after_generation(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_quality_review
    frozen, _, result = quality_run
    with pytest.raises(ValueError, match='declared|standard|unknown'):
        export_quality_review(frozen, result.result_path, tmp_path / 'unknown-must-not-exist')
    assert not (tmp_path / 'unknown-must-not-exist').exists()


def test_packaged_freeze_cannot_restore_unseen_status_after_formal_material_adjustment(quality_run, tmp_path):
    from tests.support.agent_acceptance import import_quality_reviews, quality_report, record_quality_adjustment

    frozen, package, _ = quality_run
    reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'forms.json')
    decision = frozen.parent / 'implementation-adjustment.txt'
    decision.write_text('SYNTHETIC: formal QG2.4 was used for an implementation adjustment.', encoding='utf-8')
    record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='implementation', evidence=decision)
    original = quality_report(frozen, package, synthetic_reviews=reviews)
    assert original['materials']['regression_tasks'] == ['QG2.formal.4']
    assert original['synthetic_validation']['qualified'] is False
    data = json.loads(package.read_bytes())
    key = next(iter(data['review_originals'].values()))['quality.freeze']['original_key']
    portable_freeze = package.parent / data['originals'][key]['path']
    portable = quality_report(portable_freeze, package, synthetic_reviews=reviews)
    assert portable['synthetic_validation']['qualified'] is False, 'A copied freeze must not erase the observed formal-material adjustment.'


@pytest.mark.parametrize('quality_run', [{'__defer_export__': True}], indirect=True)
def test_adjustment_before_export_cannot_be_forgotten_by_packaged_freeze(quality_run, tmp_path):
    from tests.support.agent_acceptance import export_quality_review, import_quality_reviews, quality_report, record_quality_adjustment

    frozen, _, result = quality_run
    decision = frozen.parent / 'before-export-adjustment.txt'
    decision.write_text('SYNTHETIC: formal QG2.4 was already used before this review export.', encoding='utf-8')
    record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='implementation', evidence=decision)
    package = export_quality_review(frozen, result.result_path, tmp_path / 'after-adjustment-review')
    reviews = import_quality_reviews(frozen, package, synthetic_forms(package), synthetic_destination=tmp_path / 'forms.json')
    original = quality_report(frozen, package, synthetic_reviews=reviews)
    assert original['materials']['regression_tasks'] == ['QG2.formal.4']
    assert original['synthetic_validation']['qualified'] is False
    data = json.loads(package.read_bytes())
    key = next(iter(data['review_originals'].values()))['quality.freeze']['original_key']
    portable_freeze = package.parent / data['originals'][key]['path']
    portable = quality_report(portable_freeze, package, synthetic_reviews=reviews)
    assert portable['synthetic_validation']['qualified'] is False, 'Pre-export observed material use must remain regression in the portable report.'


def test_copying_and_refreezing_the_same_input_and_goal_does_not_reset_observed_use(quality_source):
    from tests.support.agent_acceptance import freeze_quality, quality_material_report, record_quality_adjustment
    root, tasks = quality_source
    frozen = freeze_source(quality_source)
    original = json.loads(frozen.read_bytes())
    decision = root / 'observed-adjustment.txt'
    decision.write_text('SYNTHETIC: implementation used QG2 formal 4.', encoding='utf-8')
    record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='implementation', evidence=decision)
    copies = root / 'copied-inputs'
    copies.mkdir()
    moved_tasks = [{**task} for task in tasks]
    for task in moved_tasks:
        for kind in ('input', 'gold'):
            source = task[kind + '_path']
            destination = copies / source.name
            destination.write_bytes(source.read_bytes())
            task[kind + '_path'] = destination
    options = {key: original[key] for key in ('provenance', 'environment', 'models', 'criteria_version', 'qualification_families')}
    refrozen = freeze_quality(moved_tasks, destination=copies / 'different-candidate.json', source_version='SYNTHETIC-different-code-version', **options)
    frozen.unlink()
    decision.unlink()
    for task in tasks:
        for kind in ('input', 'gold'):
            task[kind + '_path'].unlink()
    report = quality_material_report(refrozen)
    assert report['regression_tasks'] == ['QG2.formal.4']
    assert report['formal_unseen_eligible'] is False
    assert report['different_tasks'] == 72
    for task in moved_tasks:
        if task['task_id'] == 'QG2.formal.4':
            task['objective'] = 'SYNTHETIC independent objective for the same attachment: explain its file encoding.'
    independent = freeze_quality(moved_tasks, destination=copies / 'different-objective.json', source_version='SYNTHETIC-third-code-version', **options)
    assert quality_material_report(independent)['regression_tasks'] == []


@pytest.mark.parametrize('defect', ['missing_record', 'changed_record', 'outside_evidence'])
def test_usage_authority_refuses_reinitialization_and_truncated_journal(quality_source, defect):
    from tests.support.agent_acceptance import initialize_quality_usage_authority, quality_material_report, record_quality_adjustment
    frozen = freeze_source(quality_source)
    with pytest.raises(FileExistsError):
        initialize_quality_usage_authority()
    decision = frozen.parent / 'usage-decision.txt'
    decision.write_text('SYNTHETIC: fixed parser after reading this formal task.', encoding='utf-8')
    record = record_quality_adjustment(frozen, task_ids=['QG2.formal.4'], purpose='implementation', evidence=decision)
    original = record.read_bytes()
    held = record.with_suffix('.held')
    if defect == 'missing_record':
        record.rename(held)
    else:
        data = json.loads(original)
        if defect == 'changed_record':
            data['purpose'] = 'prompt'
        else:
            data['evidence_sha256'] = '../../foreign-evidence'
        record.write_text(json.dumps(data), encoding='utf-8')
    try:
        with pytest.raises(ValueError, match='truncated|changed|invalid identity'):
            quality_material_report(frozen)
    finally:
        if defect == 'missing_record':
            held.rename(record)
        else:
            record.write_bytes(original)
