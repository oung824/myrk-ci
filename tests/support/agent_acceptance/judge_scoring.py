"""Ordinary scoring and reproducible audits; actual human attestations stay separate."""
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tests.support.agent_acceptance.output_review import OutputType, digest


class AuditOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    output_id: str = Field(min_length=1)
    output_type: OutputType


def freeze_audit_plan(destination: Path, outputs: list[AuditOutput], *, sampling_python: Path) -> Path:
    """Freeze registration order and draw before any predictions are available."""
    if len({row.output_id for row in outputs}) != len(outputs):
        raise ValueError('Audit registration needs unique output IDs.')
    if outputs:
        response = subprocess.run([str(sampling_python), '-I', '-S', '-B', str(Path(__file__).with_name('judge_sampling.py'))],
            input=json.dumps([row.model_dump() for row in outputs]), encoding='utf-8', capture_output=True, check=False)
        if response.returncode:
            raise ValueError('Frozen sampling runtime unavailable; use human review: ' + response.stderr)
        plan: dict[str, Any] = json.loads(response.stdout)
        plan['sampling_executable_sha256'] = sha256(sampling_python.read_bytes()).hexdigest()
    else:
        plan = {'algorithm':'CPython-3.13.0-random.Random.sample', 'seed':20261116, 'sampling_runtime':None,
            'input_ids':[], 'by_type':{kind:{'input_ids':[], 'N':0, 'base_sample_count':0, 'base_selected_ids':[]} for kind in OUTPUT_TYPES},
            'topup_ids':[], 'selected_ids':[], 'actual_sample_count':0}
    plan['caller_runtime'] = sys.version
    plan['sampling_executable'] = str(sampling_python.resolve())
    plan['binding'] = digest(plan)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    return destination


from base64 import b64encode
from hashlib import sha256
from secrets import token_hex
import shutil
from typing import Literal
from pydantic import model_validator

from tests.support.agent_acceptance.frozen import FrozenExecution, SOURCE
from tests.support.agent_acceptance.judge_qualification import (
    JudgeCandidate, OUTPUT_TYPES, _read_study, _native_prediction,
    fixture_qualification_report, qualification_applicability, qualification_report,
)
from tests.support.agent_acceptance.output_review import read_package, review_report


class ScoringOutput(AuditOutput):
    source_task_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    input_path: Path
    output_path: Path
    original_paths: tuple[Path, ...] = Field(min_length=1)
    review_only_paths: tuple[Path, ...] = ()
    ordinary_dimensions: tuple[Literal['sufficiency', 'readability', 'explanation'], ...] = Field(min_length=1)
    source_kind: Literal['actual_execution', 'fixture_execution']
    language: str
    domain: str
    review_package_path: Path | None = None
    deterministic_status: Literal['passed', 'failed', 'unknown'] = 'unknown'
    disputed: bool = False
    new_risk_scope: bool = False
    mandatory_human_dimensions: tuple[str, ...] = ('critical_status', 'authorization', 'isolation', 'prohibited_actions',
        'new_authorization', 'operation_status', 'pending_ownership', 'K1', 'K2', 'K3', 'K4', 'K5')

    @model_validator(mode='after')
    def ordinary_only(self) -> 'ScoringOutput':
        if not set(ScoringOutput.model_fields['mandatory_human_dimensions'].default) <= set(self.mandatory_human_dimensions):
            raise ValueError('The mandatory critical human dimensions cannot be removed.')
        if len(set(self.ordinary_dimensions)) != len(self.ordinary_dimensions):
            raise ValueError('Only unique ordinary dimensions may be scored.')
        return self


def _source_facts(package: dict[str, Any], exported: dict[str, Any], originals: tuple[Path, ...]) -> dict[str, Any]:
    """Use the bound execution's facts, never a caller's source or deterministic-status declaration."""
    case = next((item for item in package['original_cases'] if item['execution_id']==exported['execution_id']), None)
    if case is None:
        return {'real_originals':False, 'deterministic_status':'unknown', 'known_failures':[]}
    failure_fields = [name for name in ('tool_status','product_status','case_status','target_status','deterministic_status','content_status')
        if case.get(name)=='failed']
    failure_fields.extend(name for name,value in case.get('critical_contracts',{}).items() if value=='failed')
    return {'package_binding':package['binding'], 'package_mode':package['mode'], 'provenance':exported['provenance'],
        'required_dimensions':[name for name,standard in exported['standard']['dimensions'].items() if standard['applicable']],
        'real_originals':package['mode']=='live' and exported['provenance']=='current_real'
            and exported['evidence_complete'] and case.get('evidence_status')=='complete'
            and not any(b'"transport_provenance": "fixed_fixture"' in path.read_bytes() for path in originals),
        'deterministic_status':case.get('deterministic_status','unknown'), 'known_failures':failure_fields}

def freeze_judge_scoring(destination: Path, *, outputs: list[ScoringOutput], candidate: JudgeCandidate,
        qualification_study: Path, sampling_python: Path, mode: Literal['actual', 'synthetic_test'],
        qualification_result: Path | None = None, synthetic_predictions: dict[str, dict[str, str]] | None = None,
        baseline_all_human: bool = False) -> Path:
    """Freeze complete ordinary inputs, eligibility and the draw before model calls; grants no authority."""
    if len({row.output_id for row in outputs}) != len(outputs):
        raise ValueError('Each ordinary output has one unique ID and one primary type.')
    qualification = (fixture_qualification_report(qualification_study, synthetic_predictions)
        if mode == 'synthetic_test' and synthetic_predictions is not None
        else qualification_report(qualification_study, result_path=qualification_result))
    if mode == 'actual' and synthetic_predictions is not None:
        raise ValueError('Synthetic qualification cannot authorize actual scoring.')
    destination.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    def copy(path: Path) -> str:
        body = path.read_bytes()
        name = 'originals/' + sha256(body).hexdigest()[:24]
        target = destination/name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
        files[name] = sha256(body).hexdigest()
        return name
    config = candidate.model_dump(mode='json')
    config['prompt_path'], config['criteria_path'] = copy(candidate.prompt_path), copy(candidate.criteria_path)
    frozen: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    shutil.copytree(qualification_study.parent, destination/'qualification')
    for path in (destination/'qualification').rglob('*'):
        if path.is_file():
            files[path.relative_to(destination).as_posix()] = sha256(path.read_bytes()).hexdigest()
    qualification_package = _read_study(qualification_study)
    for row in outputs:
        applicable = qualification_applicability(qualification, candidate, output_type=row.output_type,
            language=row.language, domain=row.domain)
        old_dimensions = {tuple(item['ordinary_dimensions']) for item in qualification_package['samples']
            if item['purpose']=='qualification' and item['output_type']==row.output_type}
        reason = ('first_baseline_all_human' if baseline_all_human else
            'actual_qualification_not_started' if mode=='actual' and qualification['actual_qualification']!='qualified' else
            'fixture_qualification_incomplete' if mode=='synthetic_test' and qualification['fixture_status']!='qualified' else
            applicable['reason'] if not applicable['scope_valid'] else
            'ordinary_dimensions_not_qualified' if row.ordinary_dimensions not in old_dimensions else
            'new_risk_scope' if row.new_risk_scope else 'disputed' if row.disputed else
            'scoring_cap_320' if len(inputs)>=320 else None)
        source_facts: dict[str, Any] = {'real_originals':False, 'deterministic_status':'unknown', 'known_failures':[]}
        hidden = {sha256(path.read_bytes()).hexdigest() for path in row.review_only_paths}
        if row.review_package_path is not None:
            package = read_package(row.review_package_path)
            exported = next((item for item in package['outputs'] if item['output_id']==row.output_id), None)
            if (exported is None or exported['type']!=row.output_type or exported['execution_id']!=row.source_task_id
                    or exported['text']!=row.output_path.read_text(encoding='utf-8')
                    or exported['actual_input']!=json.loads(row.input_path.read_bytes())
                    or {sha256(path.read_bytes()).hexdigest() for path in row.original_paths}
                        != {package['originals'][key]['sha256'] for key in exported['required_originals']}):
                raise ValueError('Scoring must preserve the same exported complete input, output and originals.')
            source_facts = _source_facts(package, exported, row.original_paths)
            source_facts['package_original_path'] = copy(row.review_package_path)
            hidden.update(package['originals'][key]['sha256'] for key in exported['required_originals']
                if package['originals'][key].get('purpose')=='review_context' and key.rsplit('/',1)[-1]!='quality.input')
        elif row.source_kind=='actual_execution':
            reason = 'actual_original_binding_missing'
        original_names = [copy(path) for path in row.original_paths]
        payload = {'purpose':'scoring', 'candidate_binding':qualification['candidate_binding'],
            'instruction':candidate.prompt_path.read_text(encoding='utf-8'),
            'ordinary_standard':candidate.criteria_path.read_text(encoding='utf-8'), 'model_unit_id':token_hex(12),
            'output_type':row.output_type, 'ordinary_dimensions':list(row.ordinary_dimensions),
            'language':row.language, 'domain':row.domain,
            'actual_input':row.input_path.read_text(encoding='utf-8'), 'output':row.output_path.read_text(encoding='utf-8'),
            'original_evidence':[{'sha256':files[name], 'body_base64':b64encode((destination/name).read_bytes()).decode('ascii')}
                for name in original_names if files[name] not in hidden]}
        body = json.dumps(payload, ensure_ascii=False, indent=2)+'\n'
        if reason is None and len(body.encode('utf-8')) > 32768:
            reason = 'complete_input_unfit'
        frozen.append({**row.model_dump(mode='json'), 'input_path':copy(row.input_path), 'output_path':copy(row.output_path),
            'original_paths':original_names, 'review_only_paths':[copy(path) for path in row.review_only_paths],
            'route':'human' if reason else 'judge', 'reason':reason, 'source_facts':source_facts,
            'reference':{'review_only_sha256':sorted(hidden)}})
        if reason is None:
            identity = 'judge:scoring:'+row.output_id
            relative = 'judge-inputs/'+sha256(identity.encode()).hexdigest()[:24]+'.json'
            target = destination/relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(body, encoding='utf-8')
            files[relative] = sha256(target.read_bytes()).hexdigest()
            inputs.append({'execution_id':identity, 'output_id':row.output_id, 'purpose':'scoring', 'round':1,
                'family_id':row.family_id, 'material_path':relative, 'material_sha256':files[relative]})
    audit_failure: str | None = None
    audit_path = destination/'audit.json'
    try:
        freeze_audit_plan(audit_path,
            [AuditOutput(output_id=row['output_id'], output_type=row['output_type']) for row in frozen if row['route']=='judge'],
            sampling_python=sampling_python)
    except (OSError, ValueError) as error:
        for fallback_row in frozen:
            if fallback_row['route']=='judge':
                fallback_row['route'], fallback_row['reason'] = 'human', 'sampling_runtime_unavailable'
        inputs = []
        freeze_audit_plan(audit_path, [], sampling_python=sampling_python)
        audit_failure = str(error)
    else:
        audit_failure = None
    files['audit.json'] = sha256(audit_path.read_bytes()).hexdigest()
    study: dict[str, Any] = {'schema_version':1, 'mode':mode, 'candidate':config,
        'candidate_binding':qualification['candidate_binding'], 'candidate_fingerprints':qualification['candidate_fingerprints'],
        'qualification':qualification, 'qualification_study':'qualification/'+qualification_study.name,
        'source_relative_root':destination.resolve().relative_to(SOURCE).as_posix() if destination.resolve().is_relative_to(SOURCE) else None,
        'samples':frozen, 'judge_inputs':inputs, 'files':files, 'audit':json.loads(audit_path.read_bytes()),
        'baseline_all_human':baseline_all_human, 'sampling_failure':audit_failure, 'actual_qualification':'not-started', 'sending_authorized':False}
    study['binding'] = digest(study)
    path = destination/'judge-study.json'
    path.write_text(json.dumps(study, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return path


def scoring_executions(scoring_path: Path, *, order_offset: int = 0, dependencies: tuple[str, ...] = ()) -> tuple[FrozenExecution, ...]:
    """Append this scoring segment to the same parent plan and ledger as qualification."""
    study = _read_study(scoring_path)
    return tuple(FrozenExecution(execution_id=row['execution_id'], case_id='judge.scoring', protocol='google',
        scenario_id=row['execution_id'], material_family_id=row['family_id'], material_variant='baseline',
        material_path=(SOURCE/study['source_relative_root'] if study['source_relative_root'] else scoring_path.parent)/row['material_path'],
        material_sha256=row['material_sha256'], startpoint=row['execution_id'], order=number, dependencies=dependencies,
        role='judge', stage='judge', judge_purpose='scoring', request_cap=1, cost_cap_microusd=122880)
        for number, row in enumerate(study['judge_inputs'], order_offset+1))



from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.replay import Reply


def register_judge_scoring(registry: 'CaseRegistry', scoring_path: Path, *, fixed_replies: dict[str, 'Reply'],
                           preceding_cases: tuple[str, ...] = ()) -> None:
    """Reuse qualification's native SDK and terminal owner, never a second sending path."""
    from tests.support.agent_acceptance.judge_qualification import register_judge_study
    study = _read_study(scoring_path)
    if study['mode']=='actual' or not study['judge_inputs']:
        return
    register_judge_study(registry, scoring_path, fixed_replies=fixed_replies, preceding_cases=preceding_cases)


def scoring_report(scoring_path: Path, *, result_path: Path | None = None,
        fixture_reviews: list['FixtureAudit'] | None = None, human_packages: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Unknown remains in the original output denominator; AI cannot supply critical human work."""
    study = _read_study(scoring_path)
    predictions: dict[str, dict[str, Any]] = {}
    calls = []
    if result_path is not None:
        result = json.loads(result_path.read_bytes())
        expected = {row['execution_id']:row for row in study['judge_inputs']}
        planned = {row['execution_id']:row for row in result.get('planned_executions',[])}
        frozen = {row.execution_id:row.model_dump(mode='json') for row in scoring_executions(scoring_path)}
        if any(identity in planned and any(planned[identity][key]!=value for key,value in row.items() if key not in {'order','dependencies'})
                for identity,row in frozen.items()):
            raise ValueError('Scoring native results must preserve the frozen output segment.')
        for case in result['cases']:
            identity = case['execution_id']
            if identity not in expected:
                continue
            row = expected[identity]
            if row['output_id'] in predictions:
                raise ValueError('No replacement calls or duplicate judge votes may replace an original score.')
            decision, issue = _native_prediction(scoring_path, study, result_path, case, row)
            real_http = False
            if decision is not None:
                exchange_path = Path(case['evidence']['exchange'])
                exchange_path = exchange_path if exchange_path.is_absolute() else result_path.parent/exchange_path
                wire = json.loads(exchange_path.read_bytes())['exchanges']
                real_http = len(wire)==1 and wire[0].get('transport_provenance')=='real_http'
            observation = {'execution_id':identity, 'output_id':row['output_id'], 'conclusions':decision, 'issue':issue, 'real_http':real_http,
                'original_case_status':case['case_status'], 'evidence':case['evidence']}
            predictions[row['output_id']] = observation
            calls.append(observation)

    fixtures = {row.output_id:row for row in (fixture_reviews or [])}
    if len(fixtures) != len(fixture_reviews or []) or set(fixtures)-{row['output_id'] for row in study['samples']}:
        raise ValueError('Fixture audits cannot replace an earlier audit or invent an output.')
    if fixtures and study['mode']!='synthetic_test':
        raise ValueError('Fixture audits never supply actual human evidence.')
    human: dict[str, dict[str, Any]] = {}
    sources = {row['output_id']:row.get('source_facts',{'real_originals':False,'deterministic_status':'unknown','known_failures':[]}) for row in study['samples']}
    for package_path in human_packages:
        package = read_package(package_path)
        reviewed = review_report(package_path)
        for row in study['samples']:
            exported = next((item for item in package['outputs'] if item['output_id']==row['output_id']), None)
            if exported is None:
                continue
            if (row['source_kind']!='actual_execution' or exported['type']!=row['output_type']
                    or exported['execution_id']!=row['source_task_id']
                    or exported['text']!=(scoring_path.parent/row['output_path']).read_text(encoding='utf-8')
                    or exported['actual_input']!=json.loads((scoring_path.parent/row['input_path']).read_bytes())
                    or {package['originals'][key]['sha256'] for key in exported['required_originals']}
                        != {study['files'][name] for name in row['original_paths']}):
                raise ValueError('Actual audit must cover this same complete exported output and originals.')
            observed_source = _source_facts(package,exported,tuple(scoring_path.parent/name for name in row['original_paths']))
            prior_source = sources[row['output_id']]
            observed_source['known_failures'] = sorted(set(prior_source['known_failures']) | set(observed_source['known_failures']))
            observed_source['required_dimensions'] = sorted(set(prior_source.get('required_dimensions',[])) | set(observed_source.get('required_dimensions',[])))
            if prior_source['deterministic_status']=='failed':
                observed_source['deterministic_status'] = 'failed'
            sources[row['output_id']] = observed_source
            decision = next(item for item in reviewed['outputs'] if item['output_id']==row['output_id'])
            if not decision['history']:
                continue
            if row['output_id'] in human and human[row['output_id']] != decision:
                raise ValueError('Use the common final human review; conflicting exports cannot vote.')
            human[row['output_id']] = decision
    selected = set(study['audit']['selected_ids'])
    by_type: dict[str, dict[str, Any]] = {}
    ordinary: dict[str, dict[str, str]] = {}
    critical: dict[str, dict[str, str]] = {}
    for row in study['samples']:
        identity = row['output_id']
        if identity in human:
            dimensions = human[identity]['dimensions']
            ordinary[identity] = {name:dimensions.get(name,'unknown') for name in row['ordinary_dimensions']}
            critical[identity] = {name:dimensions.get(name,'unknown') for name in row['mandatory_human_dimensions']}
        elif identity in fixtures:
            fixture = fixtures[identity]
            if set(fixture.ordinary_conclusions) != set(row['ordinary_dimensions']):
                raise ValueError('Audit ordinary conclusions must use the same frozen dimensions.')
            ordinary[identity] = dict(fixture.ordinary_conclusions)
            critical[identity] = {name:fixture.critical_conclusions.get(name,'unknown') for name in row['mandatory_human_dimensions']}
    def label(values: dict[str, str] | None) -> str:
        return ('failed' if values and 'failed' in values.values() else 'passed'
            if values and all(value in {'passed','not_applicable'} for value in values.values()) else 'unknown')
    for kind in OUTPUT_TYPES:
        rows = [row for row in study['samples'] if row['output_type']==kind and row['route']=='judge']
        audited = [row for row in rows if row['output_id'] in selected]
        false_accepts = [row['output_id'] for row in rows if label(predictions.get(row['output_id'],{}).get('conclusions'))=='passed'
            and label(ordinary.get(row['output_id']))=='failed']
        human_passes = [row['output_id'] for row in audited if label(ordinary.get(row['output_id']))=='passed']
        false_rejects = [identity for identity in human_passes if label(predictions.get(identity,{}).get('conclusions'))=='failed']
        reason = ('confirmed_false_acceptance' if false_accepts else 'confirmed_false_rejects_over_10_percent'
            if len(false_rejects)>=2 and len(false_rejects)*10>len(human_passes) else None)
        recheck = [row['output_id'] for row in rows if reason is not None and (not false_accepts
            or label(predictions.get(row['output_id'],{}).get('conclusions'))=='passed')]
        by_type[kind] = {'N':len(rows), 'random_sample_count':len(audited), 'sampled_human_pass_denominator':len(human_passes),
            'sampled_human_unknown':sum(label(ordinary.get(row['output_id']))=='unknown' for row in audited),
            'false_accepts':len(false_accepts), 'false_accept_ids':false_accepts, 'false_rejects':len(false_rejects),
            'false_reject_ids':false_rejects, 'paused':reason is not None, 'pause_reason':reason, 'recheck_ids':recheck,
            'confirmation_basis':'synthetic_fixture' if study['mode']=='synthetic_test' else 'actual_human',
            'other_scopes':'Require evidence of actual impact; no automatic global pause.'}
    outputs = []
    for row in study['samples']:
        identity = row['output_id']
        ai = predictions.get(identity)
        reference_ordinary = ordinary.get(identity,{})
        reference_critical = critical.get(identity,{})
        dimensions = human.get(identity,{}).get('dimensions',{})
        human_ordinary = {name:dimensions.get(name,'unknown') for name in row['ordinary_dimensions']} if identity in human else {}
        human_critical = {name:dimensions.get(name,'unknown') for name in row['mandatory_human_dimensions']} if identity in human else {}
        paused = by_type[row['output_type']]['paused']
        disposition = label(reference_ordinary) if reference_ordinary else ('unknown' if paused or identity in selected or row['route']=='human'
            else label(ai['conclusions']) if ai else 'unknown')
        fixture_values = [disposition, label(reference_critical), row['deterministic_status']]
        fixture_final = 'failed' if 'failed' in fixture_values else 'passed' if all(value=='passed' for value in fixture_values) else 'unknown'
        source = sources[identity]
        required = set(source.get('required_dimensions',[])) | set(dimensions) | set(row['ordinary_dimensions']) | set(row['mandatory_human_dimensions'])
        actual_dimensions = {name:dimensions.get(name,'unknown') for name in sorted(required)}
        actual_sources = {name:'actual_human' if name in dimensions else 'unknown' for name in actual_dimensions}
        actual_ai_valid = (study['mode']=='actual' and study['qualification']['actual_qualification']=='qualified'
            and row['route']=='judge' and source['real_originals'] and ai is not None and ai['real_http']
            and not paused and identity not in selected and not row['disputed'] and not row['new_risk_scope'])
        if actual_ai_valid and not human.get(identity,{}).get('disagreement',False):
            assert ai is not None
            for name in row['ordinary_dimensions']:
                if actual_dimensions[name]=='unknown' and ai['conclusions'] is not None:
                    actual_dimensions[name] = ai['conclusions'][name]
                    actual_sources[name] = 'qualified_actual_ai'
        actual_values = [label(actual_dimensions),label({'deterministic_status':source['deterministic_status']})]
        final = ('failed' if source['known_failures'] or 'failed' in actual_values else 'passed'
            if source['real_originals'] and all(value=='passed' for value in actual_values) else 'unknown')
        outputs.append({'output_id':identity, 'output_type':row['output_type'], 'route':row['route'], 'reason':row['reason'],
            'judge_original':ai, 'human_critical':human_critical, 'human_ordinary':human_ordinary,
            'fixture_ordinary':dict(fixtures[identity].ordinary_conclusions) if identity in fixtures else {},
            'fixture_critical':dict(fixtures[identity].critical_conclusions) if identity in fixtures else {},
            'actual_human_history':human.get(identity,{}).get('history',[]), 'source_facts':source,
            'actual_dimensions':actual_dimensions, 'actual_conclusion_sources':actual_sources,
            'final':final, 'fixture_final':fixture_final if study['mode']=='synthetic_test' else 'not_applicable',
            'deterministic_status':source['deterministic_status'], 'declared_deterministic_status':row['deterministic_status'],
            'required_human_dimensions':row['mandatory_human_dimensions'],
            'ordinary_conclusion_sources':{name:actual_sources[name] for name in row['ordinary_dimensions']},
            'critical_conclusion_sources':{name:actual_sources[name] for name in row['mandatory_human_dimensions']},
            'random_audit_selected':identity in selected, 'ordinary_automatic_release_paused':paused})
    audit = {**study['audit'], 'reviewed_sample_count':sum(label(ordinary.get(identity))!='unknown' for identity in selected),
        'status':'complete_judge_segment' if len(predictions)==len(study['judge_inputs']) else 'incomplete_judge_segment'}
    return {'mode':study['mode'], 'planned_outputs':len(outputs), 'unknown':sum(row['final']=='unknown' for row in outputs),
        'outputs':outputs, 'audit':audit, 'by_type':by_type,
        'judge_completed_outputs':sum(row['conclusions'] is not None for row in predictions.values()),
        'actual_qualification':'not-started', 'user_acceptance':'pending', 'product_executions_added':0,
        'judge_call_results':calls, 'actual_human_reviews':list(human.values()),
        'fixture_reviews':[row.model_dump(mode='json') for row in fixtures.values()]}


class FixtureAudit(BaseModel):
    """Explicit synthetic expected judgments; never a generated HumanReview or signature."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    output_id: str
    authorship: Literal['fixture_reference'] = 'fixture_reference'
    ordinary_conclusions: dict[str, Literal['passed', 'failed', 'unknown']]
    critical_conclusions: dict[str, Literal['passed', 'failed', 'unknown', 'not_applicable']]
    reason: str = Field(min_length=1)
