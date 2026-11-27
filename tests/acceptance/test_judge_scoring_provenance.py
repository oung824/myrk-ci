"""Independent merger red: fixture judgments never complete actual output review."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.acceptance.test_judge_scoring_audits import qualified_fixture
from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, export_review, run_plan
from tests.support.agent_acceptance.judge_scoring import (
    FixtureAudit, ScoringOutput, freeze_judge_scoring, scoring_report,
)


@pytest.mark.parametrize('original_binding', [False, True], ids=['self-declared-source', 'bound-local-originals'])
def test_fixture_audit_cannot_supply_actual_final(acceptance_public_root, original_binding):
    tmp_path = acceptance_public_root
    tmp_path.mkdir(parents=True)
    product = asyncio.run(run_plan(AcceptancePlan(cases=['ordinary.question']),
        root=tmp_path/'product', registry=builtin_registry()))
    package_path = export_review(product.result_path, tmp_path/'review')
    package = json.loads(package_path.read_bytes())
    exported = package['outputs'][0]
    source = tmp_path/'actual-input.json'
    source.write_text(json.dumps(exported['actual_input'], ensure_ascii=False), encoding='utf-8')
    output = tmp_path/'actual-output.txt'
    output.write_text(exported['text'], encoding='utf-8')
    row = ScoringOutput(output_id=exported['output_id'], output_type=exported['type'],
        source_task_id=exported['execution_id'], family_id='independent-local-originals',
        input_path=source, output_path=output,
        original_paths=tuple(package_path.parent/package['originals'][key]['path']
            for key in exported['required_originals']), ordinary_dimensions=('sufficiency',),
        source_kind='actual_execution', language='zh', domain='synthetic', deterministic_status='passed',
        review_package_path=package_path if original_binding else None)
    study, candidate, predictions = qualified_fixture(tmp_path/'qualification')
    scoring = freeze_judge_scoring(tmp_path/'scoring', outputs=[row], candidate=candidate,
        qualification_study=study, synthetic_predictions=predictions, mode='synthetic_test',
        sampling_python=Path('E:/Myrk/.scratch/judge-sampling-python-3.13.0/runtime/python.exe'))
    fixture = FixtureAudit(output_id=row.output_id, ordinary_conclusions={'sufficiency':'passed'},
        critical_conclusions={name:'passed' for name in row.mandatory_human_dimensions},
        reason='Explicit synthetic audit; no actual person reviewed any original.')
    report = scoring_report(scoring, fixture_reviews=[fixture])
    (tmp_path/'observed-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['actual_human_reviews'] == []
    assert report['actual_qualification'] == 'not-started'
    assert len(report['fixture_reviews']) == 1
    assert report['outputs'][0]['fixture_final'] == 'passed'
    assert report['outputs'][0]['final'] == 'unknown'
    assert report['unknown'] == report['planned_outputs'] == 1


def local_scoring(root, *, mode='synthetic_test', wrong_answer=False):
    from tests.support.agent_acceptance.replay import OfflineSession, Reply
    factory = (lambda protocol,replies,**options:OfflineSession(protocol,[Reply(text='The result is 43.')],**options)) if wrong_answer else None
    product=asyncio.run(run_plan(AcceptancePlan(cases=['ordinary.question']),root=root/'product',
        registry=builtin_registry(),session_factory=factory))
    package_path=export_review(product.result_path,root/'review')
    package=json.loads(package_path.read_bytes())
    exported=package['outputs'][0]
    source, output=root/'input.json',root/'output.txt'
    source.write_text(json.dumps(exported['actual_input'],ensure_ascii=False),encoding='utf-8')
    output.write_text(exported['text'],encoding='utf-8')
    row=ScoringOutput(output_id=exported['output_id'],output_type=exported['type'],source_task_id=exported['execution_id'],
        family_id='explicit-local-mock-source',input_path=source,output_path=output,
        original_paths=tuple(package_path.parent/package['originals'][key]['path'] for key in exported['required_originals']),
        ordinary_dimensions=('sufficiency',),source_kind='actual_execution',language='zh',domain='synthetic',
        deterministic_status='passed',review_package_path=package_path)
    study,candidate,predictions=qualified_fixture(root/'qualification')
    scoring=freeze_judge_scoring(root/'scoring',outputs=[row],candidate=candidate,qualification_study=study,
        synthetic_predictions=predictions if mode=='synthetic_test' else None,mode=mode,
        sampling_python=Path('E:/Myrk/.scratch/judge-sampling-python-3.13.0/runtime/python.exe'))
    fixture=FixtureAudit(output_id=row.output_id,ordinary_conclusions={'sufficiency':'passed'},
        critical_conclusions={name:'passed' for name in row.mandatory_human_dimensions},
        reason='Synthetic expected dimensions only; no HumanReview or signature is created.')
    return scoring,package_path,fixture


@pytest.mark.parametrize('failed_dimension',['sufficiency','K1'])
def test_fixture_ordinary_and_critical_dimensions_have_no_actual_human_source(acceptance_public_root,failed_dimension):
    root=acceptance_public_root
    scoring,package,fixture=local_scoring(root)
    ordinary={**fixture.ordinary_conclusions,**({'sufficiency':'failed'} if failed_dimension=='sufficiency' else {})}
    critical={**fixture.critical_conclusions,**({'K1':'failed'} if failed_dimension=='K1' else {})}
    fixture=fixture.model_copy(update={'ordinary_conclusions':ordinary,'critical_conclusions':critical})
    report=scoring_report(scoring,fixture_reviews=[fixture],human_packages=(package,))
    (root/'observed-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    row=report['outputs'][0]
    assert row['final']=='unknown' and row['fixture_final']=='failed'
    assert row['human_ordinary']==row['human_critical']=={}
    assert row['fixture_ordinary']==ordinary and row['fixture_critical']==critical
    assert set(row['ordinary_conclusion_sources'].values())==set(row['critical_conclusion_sources'].values())=={'unknown'}
    assert report['actual_human_reviews']==[] and report['judge_completed_outputs']==0
    assert row['source_facts']['real_originals'] is False
    assert report['unknown']==report['planned_outputs']==1


def test_original_program_failure_cannot_be_replaced_by_a_declared_pass(acceptance_public_root):
    scoring,package,fixture=local_scoring(acceptance_public_root,wrong_answer=True)
    original=json.loads(package.read_bytes())['original_cases'][0]
    assert original['deterministic_status']=='failed'
    report=scoring_report(scoring,fixture_reviews=[fixture])
    (acceptance_public_root/'observed-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    row=report['outputs'][0]
    assert row['declared_deterministic_status']=='passed' and row['deterministic_status']=='failed'
    assert 'deterministic_status' in row['source_facts']['known_failures']
    assert row['final']=='failed' and report['actual_human_reviews']==[]
    assert report['planned_outputs']==1 and report['judge_completed_outputs']==0


def test_actual_human_fallback_does_not_accept_fixture_audits_or_unsigned_templates(acceptance_public_root):
    scoring,package,fixture=local_scoring(acceptance_public_root,mode='actual')
    with pytest.raises(ValueError,match='Fixture audits never supply actual human evidence'):
        scoring_report(scoring,fixture_reviews=[fixture])
    report=scoring_report(scoring,human_packages=(package,))
    (acceptance_public_root/'observed-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert report['outputs'][0]['route']=='human'
    assert report['outputs'][0]['final']=='unknown' and report['outputs'][0]['fixture_final']=='not_applicable'
    assert report['actual_qualification']=='not-started' and report['actual_human_reviews']==[]
    assert report['unknown']==report['planned_outputs']==1
