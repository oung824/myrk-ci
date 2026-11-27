"""Public human-reference contracts with explicitly synthetic signed-form fixtures."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest

from tests.acceptance.test_judge_qualification import candidate, samples
from tests.support.agent_acceptance import (
    AcceptancePlan, HumanReview, builtin_registry, export_review, import_reviews,
    review_report, run_plan,
)
from tests.support.agent_acceptance.judge_qualification import (
    ActualHumanReference, JudgeSample, freeze_judge_study,
)


def human_reference(root: Path, *, final_verdict='passed', later_verdict='failed', resolve=True, later_dimension='sufficiency', standard=None):
    """Exercise real import/report code; form authors are named fixture identities, not actual people."""
    root.mkdir(parents=True)
    result = asyncio.run(run_plan(AcceptancePlan(cases=['ordinary.question']),
        root=root/'run', registry=builtin_registry()))
    package_path = export_review(result.result_path, root/'review', final_reviewer='Synthetic final form fixture',
        standards={'main_answer': standard} if standard is not None else None)
    package = json.loads(package_path.read_bytes())
    output = package['outputs'][0]
    first = HumanReview(review_id='first', output_id=output['output_id'], reviewer='Synthetic first form fixture',
        authorship='human', signed_at='2026-11-16T06:00:00+08:00', scope='Explicit synthetic form contract only',
        package_sha256=package['binding'], criteria_version=output['standard']['version'],
        output_sha256=output['sha256'], reviewed_originals=output['required_originals'],
        conclusions={name:'passed' for name in output['standard']['dimensions']}, reasons={})
    second = first.model_copy(update={'review_id':'second',
        'conclusions':{name:'failed' for name in first.conclusions},
        'reasons':{name:'Synthetic disagreement, with all originals reviewed.' for name in first.conclusions}})
    import_reviews(package_path,[first,second])
    ids = ['first','second']
    if resolve:
        final = first.model_copy(update={'review_id':'z-final','reviewer':'Synthetic final form fixture',
            'resolves':['first','second'],
            'conclusions':{**first.conclusions,'sufficiency':final_verdict},
            'reasons':{name:'Synthetic final form resolves only the two named prior forms.' for name in first.conclusions}})
        import_reviews(package_path,[final])
        ids.append('z-final')
    if later_verdict is not None:
        later = first.model_copy(update={'review_id':'third',
            'conclusions':{**first.conclusions,later_dimension:later_verdict},
            'reasons':{later_dimension:'Synthetic later form is not covered by the earlier final resolution.'}})
        import_reviews(package_path,[later])
        ids.append('third')
    actual_input, text = root/'actual-input.json', root/'actual-output.txt'
    actual_input.write_text(json.dumps(output['actual_input'],ensure_ascii=False),encoding='utf-8')
    text.write_text(output['text'],encoding='utf-8')
    sample = JudgeSample(output_id=output['output_id'],output_type=output['type'],source_task_id=output['execution_id'],
        family_id='synthetic-public-human-dispute',input_path=actual_input,output_path=text,
        original_paths=tuple(package_path.parent/package['originals'][key]['path'] for key in output['required_originals']),
        ordinary_dimensions=('sufficiency',),source_kind='human_controlled',
        actual_human_reference=ActualHumanReference(package_path=package_path,review_ids=tuple(ids),authorship='human',
            reviewer='Synthetic blind declaration fixture',signed_at='2026-11-16T07:00:00+08:00',predictions_unseen=True))
    return sample, package_path


def test_later_unresolved_human_form_cannot_be_frozen_as_a_positive_reference(tmp_path,acceptance_public_root):
    reference, package_path = human_reference(acceptance_public_root/'human')
    current = review_report(package_path)['outputs'][0]
    assert current['dimensions']['sufficiency']=='unknown'
    assert current['content_status']=='pending_review'
    calibration = samples(tmp_path/'calibration','cal',6)
    qualification = samples(tmp_path/'qualification','qual',20)
    calibration[0] = reference
    with pytest.raises(ValueError,match='quota|unknown|unresolved'):
        freeze_judge_study(acceptance_public_root/'study',calibration=calibration,qualification=qualification,
            candidate=candidate(tmp_path),mode='synthetic_test')


@pytest.mark.parametrize(('purpose','final_verdict','later_verdict','resolve','later_dimension','expected'), [
    ('calibration','passed','passed',True,'sufficiency','passed'),
    ('qualification','passed','failed',True,'sufficiency','unknown'),
    ('qualification','passed','unknown',True,'sufficiency','unknown'),
    ('calibration','failed','passed',True,'sufficiency','failed'),
    ('qualification','failed','unknown',True,'sufficiency','failed'),
    ('calibration','failed','failed',True,'sufficiency','failed'),
    ('qualification','passed',None,False,'sufficiency','unknown'),
    ('calibration','passed',None,True,'sufficiency','passed'),
    ('calibration','passed','failed',True,'readability','passed'),
], ids=['same-later-pass','later-fail-unresolved','later-unknown-unresolved','known-final-failure',
    'known-final-failure-plus-unknown','same-later-failure','no-final-unresolved','only-final','other-dimension-dispute'])
def test_human_reference_uses_the_same_frozen_ordinary_dimensions_and_keeps_all_versions(
        tmp_path,acceptance_public_root,purpose,final_verdict,later_verdict,resolve,later_dimension,expected):
    reference, package_path = human_reference(acceptance_public_root/'human',final_verdict=final_verdict,
        later_verdict=later_verdict,resolve=resolve,later_dimension=later_dimension)
    current = review_report(package_path)['outputs'][0]
    assert current['dimensions']['sufficiency']==expected
    originals = {path.name:sha256(path.read_bytes()).hexdigest() for path in (package_path.parent/'reviews').glob('*.json')}
    calibration, qualification = samples(tmp_path/'calibration','cal',6), samples(tmp_path/'qualification','qual',20)
    selected = calibration if purpose=='calibration' else qualification
    selected[3 if purpose=='calibration' and expected=='failed' else 10 if expected=='failed' else 0] = reference
    destination = acceptance_public_root/'study'
    if expected=='unknown':
        with pytest.raises(ValueError,match='quota|unknown|unresolved'):
            freeze_judge_study(destination,calibration=calibration,qualification=qualification,
                candidate=candidate(tmp_path),mode='synthetic_test')
        assert not destination.exists()
    else:
        study_path=freeze_judge_study(destination,calibration=calibration,qualification=qualification,
            candidate=candidate(tmp_path),mode='synthetic_test')
        study=json.loads(study_path.read_bytes())
        frozen=next(row['reference'] for row in study['samples'] if row['output_id']==reference.output_id)
        assert frozen['conclusions']=={'sufficiency':expected}
        assert {row['review_id'] for row in frozen['original_human_decisions']}==set(reference.actual_human_reference.review_ids)
        if later_verdict is not None:
            later_form=next(row for row in frozen['original_human_decisions'] if row['review_id']=='third')
            assert 'Synthetic later form' in later_form['reasons'][later_dimension]
            if later_dimension=='sufficiency':
                assert 'Synthetic later form' in frozen['reasons']['sufficiency']
            else:
                assert current['content_status']=='pending_review'
        assert study['actual_qualification']=='not-started' and not study['sending_authorized']
    assert {path.name:sha256(path.read_bytes()).hexdigest() for path in (package_path.parent/'reviews').glob('*.json')}==originals


@pytest.mark.parametrize('representation', ['critical_flag', 'critical_dimensions', 'mandatory_human'])
def test_bound_source_critical_dimension_cannot_become_an_ordinary_judge_reference(tmp_path, acceptance_public_root, representation):
    from copy import deepcopy
    from tests.support.agent_acceptance.judge_qualification import judge_material_manifest
    source = Path(__file__).parent / 'materials/compaction/continuity/criteria.json'
    standard = json.loads(source.read_bytes())
    standard['dimensions'].update({name: {'description': 'Synthetic ordinary form field.', 'applicable': True} for name in ('sufficiency', 'readability')})
    selected = 'pending_ownership'
    if representation == 'critical_dimensions':
        selected = 'frozen_source_constraint'
        standard['dimensions'][selected] = deepcopy(standard['dimensions']['no_new_authority'])
        standard['dimensions'][selected].pop('critical')
        standard['critical_dimensions'] = [selected]
    elif representation == 'mandatory_human':
        selected = 'operation_status'
        standard['dimensions'][selected] = {'applicable': True, 'description': 'Actual operation state requires human review.'}
    reference, _ = human_reference(acceptance_public_root / 'human', later_verdict=None, standard=standard)
    reference = JudgeSample(**{**reference.model_dump(), 'ordinary_dimensions': (selected,)})
    with pytest.raises(ValueError, match='critical|human-only'):
        judge_material_manifest([reference], candidate(tmp_path))
    assert not (acceptance_public_root / 'study').exists()


def test_bound_source_ordinary_dimension_stays_available_to_the_judge(tmp_path, acceptance_public_root):
    from tests.support.agent_acceptance.judge_qualification import judge_material_manifest
    standard = json.loads((Path(__file__).parent / 'materials/compaction/continuity/criteria.json').read_bytes())
    standard['dimensions'].update({name: {'description': 'Synthetic ordinary form field.', 'applicable': True} for name in ('sufficiency', 'readability')})
    reference, _ = human_reference(acceptance_public_root / 'human', later_verdict=None, standard=standard)
    reference = JudgeSample(**{**reference.model_dump(), 'ordinary_dimensions': ('source_use',)})
    assert set(judge_material_manifest([reference], candidate(tmp_path))) == {reference.output_id}
