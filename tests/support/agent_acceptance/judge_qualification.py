"""Frozen calibration and finite judge qualification; actual human work stays separate."""

from collections import Counter
import asyncio
import shutil
from base64 import b64encode
from difflib import SequenceMatcher
from secrets import token_hex
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.output_review import HumanReview, OutputType, digest, read_package, review_report

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.frozen import FrozenExecution
    from tests.support.agent_acceptance.replay import Reply

OUTPUT_TYPES: tuple[OutputType, ...] = ('main_answer', 'child_report', 'review_reason', 'memory', 'summary')


class FixtureReference(BaseModel):
    """An explicit test oracle, never an actual human review."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    authorship: Literal['fixture_reference'] = 'fixture_reference'
    conclusions: dict[str, Literal['passed', 'failed', 'unknown']]
    reasons: dict[str, str]


class ActualHumanReference(BaseModel):
    """A caller's blind-review declaration linked to existing signed originals."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    package_path: Path
    review_ids: tuple[str, ...] = Field(min_length=1)
    authorship: Literal['human']
    reviewer: str = Field(min_length=1)
    signed_at: datetime
    predictions_unseen: Literal[True]

    @model_validator(mode='after')
    def timezone_required(self) -> 'ActualHumanReference':
        if self.signed_at.tzinfo is None or not self.reviewer.strip():
            raise ValueError('The actual human blind-review declaration needs an identity and timezone.')
        return self


class ActualMaterialAudit(BaseModel):
    """An actual human's material-independence and blind-input attestation, not AI inference."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    authorship: Literal['human']
    reviewer: str = Field(min_length=1)
    signed_at: datetime
    reviewed_payload_sha256: dict[str, str]
    substantive_family_evidence: str = Field(min_length=1)
    unseen_from_implementation_prompt_threshold_and_tuning: Literal[True]
    complete_legal_task_and_original_evidence: Literal[True]
    judge_input_excludes_human_answers_labels_and_reasons: Literal[True]

    @model_validator(mode='after')
    def named_and_dated(self) -> 'ActualMaterialAudit':
        if self.signed_at.tzinfo is None or not self.reviewer.strip():
            raise ValueError('Actual material verification requires a named human and timezone.')
        return self


class JudgeCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: str = Field(min_length=1)
    model: Literal['gemini-2.5-pro']
    prompt_path: Path
    criteria_path: Path
    input_processing: Literal['complete-originals-json-v1']
    languages: tuple[str, ...] = Field(min_length=1)
    domains: tuple[str, ...] = Field(min_length=1)
    output_types: tuple[OutputType, ...] = Field(min_length=1)


class JudgeSample(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    output_id: str = Field(min_length=1)
    output_type: OutputType
    source_task_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    input_path: Path
    output_path: Path
    original_paths: tuple[Path, ...] = Field(min_length=1)
    ordinary_dimensions: tuple[str, ...] = Field(min_length=1)
    review_only_paths: tuple[Path, ...] = ()
    source_kind: Literal['actual_execution', 'human_controlled', 'fixture_execution', 'fixture_controlled']
    language: str = 'zh'
    domain: str = 'synthetic'
    fixture_reference: FixtureReference | None = None
    actual_human_reference: ActualHumanReference | None = None

    @model_validator(mode='after')
    def one_reference(self) -> 'JudgeSample':
        if (self.fixture_reference is None) == (self.actual_human_reference is None):
            raise ValueError('Use either a fixture reference or a separate actual HumanReview.')
        if (len(set(self.ordinary_dimensions)) != len(self.ordinary_dimensions)
                or any(name.startswith('K') or name in {'critical_status', 'task_status', 'authorization', 'isolation'}
                    for name in self.ordinary_dimensions)):
            raise ValueError('Only unique frozen ordinary dimensions belong to the judge; critical/task conclusions are separate.')
        if self.source_kind.startswith('fixture') != (self.fixture_reference is not None):
            raise ValueError('Synthetic references cannot label actual originals or actual human reviews.')
        return self


def _reference(sample: JudgeSample) -> dict[str, Any]:
    if sample.fixture_reference is not None:
        reference = sample.fixture_reference.model_dump(mode='json')
    else:
        declaration = sample.actual_human_reference
        assert declaration is not None
        package = read_package(declaration.package_path)
        output = next((row for row in package['outputs'] if row['output_id'] == sample.output_id), None)
        if output is None or output['type'] != sample.output_type or output['execution_id'] != sample.source_task_id:
            raise ValueError('Actual reference must name the same exported product output.')
        if (sample.output_path.read_text(encoding='utf-8') != output['text']
                or json.loads(sample.input_path.read_bytes()) != output['actual_input']
                or {sha256(path.read_bytes()).hexdigest() for path in sample.original_paths} !=
                    {package['originals'][key]['sha256'] for key in output['required_originals']}):
            raise ValueError('Actual reference must preserve complete original input, output and required evidence.')
        if sample.source_kind == 'actual_execution' and (output['provenance'] != 'current_real' or package['mode'] != 'live'
                or any(b'"transport_provenance": "fixed_fixture"' in path.read_bytes() for path in sample.original_paths)):
            raise ValueError('Mock originals cannot be relabelled as an actual execution.')
        reviewed = next(row for row in review_report(declaration.package_path)['outputs'] if row['output_id']==sample.output_id)
        history = [HumanReview.model_validate(row) for row in reviewed['history']]
        if set(declaration.review_ids) != {row.review_id for row in history} or any(row.signed_at > declaration.signed_at for row in history):
            raise ValueError('The blind declaration must cover all existing human decisions before judge predictions.')
        final = [row for row in history if row.resolves]
        used = ([*final, *(row for row in history if row.review_id not in {*final[0].resolves, final[0].review_id})]
            if final else history)
        conclusions = {}
        standard = output['standard']
        mandatory_human = {'critical_status', 'authorization', 'isolation', 'prohibited_actions',
            'new_authorization', 'operation_status', 'pending_ownership', 'K1', 'K2', 'K3', 'K4', 'K5'}
        for name in sample.ordinary_dimensions:
            if name not in output['standard']['dimensions'] or not output['standard']['dimensions'][name]['applicable']:
                raise ValueError('Ordinary reference dimensions must match the same frozen applicable standard.')
            if (standard['dimensions'][name].get('critical') is True
                    or name in standard.get('critical_dimensions', []) or name in mandatory_human):
                raise ValueError('Frozen critical and mandatory human-only dimensions cannot supply ordinary judge references.')
            conclusions[name] = reviewed['dimensions'][name]
        reference = {'authorship':'human', 'conclusions':conclusions,
            'reasons':{name:'; '.join(row.reasons.get(name, '') for row in used) for name in conclusions},
            'review_only_sha256':[package['originals'][key]['sha256'] for key in output['required_originals']
                if package['originals'][key].get('purpose')=='review_context' and key.rsplit('/',1)[-1]!='quality.input'],
            'declaration':declaration.model_dump(mode='json'), 'original_human_decisions':[row.model_dump(mode='json') for row in history]}
    if set(reference['conclusions']) != set(sample.ordinary_dimensions):
        raise ValueError('Reference labels must use exactly the same frozen ordinary dimensions.')
    if any(value != 'passed' and not reference['reasons'].get(name, '').strip() for name, value in reference['conclusions'].items()):
        raise ValueError('Ordinary non-pass references need a reason, independently of critical/task conclusions.')
    return reference


def _label(conclusions: dict[str, str]) -> str:
    return 'unknown' if 'unknown' in conclusions.values() else 'failed' if 'failed' in conclusions.values() else 'passed'


def _read_study(path: Path) -> dict[str, Any]:
    study: dict[str, Any] = json.loads(path.read_bytes())
    if digest({key:value for key,value in study.items() if key != 'binding'}) != study['binding']:
        raise ValueError('Frozen judge study changed; candidate_refreeze_required.')
    for relative, fingerprint in study['files'].items():
        file = (path.parent / relative).resolve()
        if not file.is_relative_to(path.parent.resolve()) or sha256(file.read_bytes()).hexdigest() != fingerprint:
            raise ValueError('Frozen judge original changed; candidate_refreeze_required.')
    return study


def freeze_judge_study(destination: Path, *, calibration: list[JudgeSample], qualification: list[JudgeSample],
                       candidate: JudgeCandidate, mode: Literal['synthetic_test', 'actual'],
                       tuning_inputs: tuple[Path, ...] = (), tuning_families: tuple[str, ...] = (),
                       calibration_rounds: int = 1, material_audit: ActualMaterialAudit | None = None) -> Path:
    """Freeze one 30/100 package before any prediction; no execution authority is granted."""
    if calibration_rounds not in {1, 2}:
        raise ValueError('Calibration permits at most two pre-frozen candidate rounds and 60 calls.')
    groups = {'calibration':calibration, 'qualification':qualification}
    refs = {row.output_id:_reference(row) for rows in groups.values() for row in rows}
    all_samples = [*calibration, *qualification]
    if len(refs) != len(all_samples):
        raise ValueError('Every output has one primary type and one unique identity.')
    if mode == 'actual' and any(row.fixture_reference is not None for row in all_samples):
        raise ValueError('Fixture references never establish actual qualification.')
    material_manifest = {row.output_id:_material_manifest(row,candidate,refs[row.output_id]) for row in all_samples}
    if mode == 'actual' and (material_audit is None or material_audit.reviewed_payload_sha256 != material_manifest):
        raise ValueError('Actual unseen-family independence and complete blind input need an actual named human material audit.')
    for kind in OUTPUT_TYPES:
        if len({row.ordinary_dimensions for row in all_samples if row.output_type==kind}) != 1:
            raise ValueError('Each type must freeze the same ordinary dimensions across human reference, calibration and qualification.')
    for purpose, rows in groups.items():
        per_type = 6 if purpose == 'calibration' else 20
        if len(rows) != per_type * 5:
            raise ValueError(f'{purpose} quota requires exactly {per_type * 5} frozen outputs; remain on human review.')
        counts = Counter((row.output_type, _label(refs[row.output_id]['conclusions'])) for row in rows)
        if any(counts[kind,label] != per_type // 2 for kind in OUTPUT_TYPES for label in ('passed','failed')):
            raise ValueError(f'{purpose} quota requires {per_type // 2} ordinary positive/negative references per type.')
    original_kinds = {'actual_execution'} if mode == 'actual' else {'actual_execution','fixture_execution'}
    actual = [row for row in qualification if row.source_kind in original_kinds]
    if len(actual) < 60 or any(sum(_label(refs[row.output_id]['conclusions']) == label for row in actual) < 20 for label in ('passed','failed')):
        raise ValueError('Qualification quota needs at least 60 original executions, including 20 positive and 20 negative ordinary references.')
    families = Counter(row.family_id for row in qualification)
    if len(families) < 50 or max(families.values()) > 2:
        raise ValueError('Qualification needs at least 50 substantive families, with at most two outputs per family.')
    prior = set(tuning_families) | {row.family_id for row in calibration}
    if prior & set(families):
        raise ValueError('Qualification families must be unseen by calibration or tuning.')
    inputs: dict[str, str] = {}
    normalized_inputs: list[tuple[str,str]] = []
    tuning_hashes = {digest(''.join(path.read_text(encoding='utf-8').split()).casefold()) for path in tuning_inputs}
    for row in all_samples:
        key = digest(''.join(row.input_path.read_text(encoding='utf-8').split()).casefold())
        if key in inputs and inputs[key] != row.family_id or row in qualification and key in tuning_hashes:
            raise ValueError('Renamed or copied substantive material is not a new unseen family.')
        normalized = ''.join(row.input_path.read_text(encoding='utf-8').split()).casefold()
        if any(family != row.family_id and SequenceMatcher(None,normalized,previous,autojunk=False).ratio() >= .90
                for family,previous in normalized_inputs):
            raise ValueError('Near-duplicate substantive material cannot become unseen through a new family label.')
        if row in qualification and any(SequenceMatcher(None,normalized,''.join(path.read_text(encoding='utf-8').split()).casefold(),autojunk=False).ratio() >= .90
                for path in tuning_inputs):
            raise ValueError('Near-duplicate tuning material cannot enter the unseen qualification set.')
        normalized_inputs.append((row.family_id,normalized))
        inputs[key] = row.family_id
        if row.output_type not in candidate.output_types or row.language not in candidate.languages or row.domain not in candidate.domains:
            raise ValueError('Candidate scope must cover the frozen language, domain and output type.')
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    def copy(path: Path) -> str:
        body = path.read_bytes()
        name = 'originals/' + sha256(body).hexdigest()[:24]
        target = destination / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
        files[name] = sha256(body).hexdigest()
        return name
    config = candidate.model_dump(mode='json')
    config['prompt_path'], config['criteria_path'] = copy(candidate.prompt_path), copy(candidate.criteria_path)
    candidate_fingerprints = {'model':candidate.model,'prompt_sha256':sha256(candidate.prompt_path.read_bytes()).hexdigest(),
        'criteria_sha256':sha256(candidate.criteria_path.read_bytes()).hexdigest(),'input_processing':candidate.input_processing}
    frozen = []
    for purpose, rows in groups.items():
        for row in rows:
            frozen.append({**row.model_dump(mode='json', exclude={'fixture_reference','actual_human_reference'}),
                'purpose':purpose, 'input_path':copy(row.input_path), 'output_path':copy(row.output_path),
                'original_paths':[copy(path) for path in row.original_paths],
                'review_only_paths':[copy(path) for path in row.review_only_paths], 'reference':refs[row.output_id]})
    judge_inputs = []
    for purpose, rounds in (('calibration',range(1,calibration_rounds+1)),('qualification',range(1,2))):
        for round_number in rounds:
            for output in (item for item in frozen if item['purpose'] == purpose):
                identity = f"judge:{purpose}:{round_number}:{output['output_id']}"
                payload = {'purpose':purpose, 'candidate_binding':digest(config),
                    'instruction':(destination/config['prompt_path']).read_text(encoding='utf-8'),
                    'ordinary_standard':(destination/config['criteria_path']).read_text(encoding='utf-8'),
                    'model_unit_id':token_hex(12), 'output_type':output['output_type'],
                    'ordinary_dimensions':output['ordinary_dimensions'], 'language':output['language'], 'domain':output['domain'],
                    'actual_input':(destination/output['input_path']).read_text(encoding='utf-8'),
                    'output':(destination/output['output_path']).read_text(encoding='utf-8'),
                    'original_evidence':[{'sha256':files[name], 'body_base64':b64encode((destination/name).read_bytes()).decode('ascii')}
                        for name in output['original_paths'] if name not in output['review_only_paths']
                        and files[name] not in output['reference'].get('review_only_sha256',[])]}
                relative = 'judge-inputs/'+sha256(identity.encode()).hexdigest()[:24]+'.json'
                target = destination/relative
                target.parent.mkdir(exist_ok=True)
                target.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                files[relative] = sha256(target.read_bytes()).hexdigest()
                judge_inputs.append({'execution_id':identity,'output_id':output['output_id'],'purpose':purpose,
                    'round':round_number,'material_path':relative,'material_sha256':files[relative],
                    'family_id':output['family_id']})
    study = {'schema_version':1, 'mode':mode, 'candidate':config, 'candidate_binding':digest(config),
        'source_relative_root':destination.resolve().relative_to(SOURCE).as_posix() if destination.resolve().is_relative_to(SOURCE) else None,
        'blind_material_manifest':material_manifest,
        'candidate_fingerprints':candidate_fingerprints, 'material_audit':material_audit.model_dump(mode='json') if material_audit else None,
        'judge_inputs':judge_inputs, 'calibration_rounds':calibration_rounds, 'samples':frozen, 'files':files,
        'tuning_families':list(tuning_families), 'tuning_input_sha256':sorted(tuning_hashes),
        'sending_authorized':False, 'actual_qualification':'not-started',
        'material_isolation':'Exact normalized copies are rejected; substantive near-duplicate independence still requires actual human verification.'}
    study['binding'] = digest(study)
    path = destination / 'judge-study.json'
    path.write_text(json.dumps(study,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return path


def _counts(rows: list[dict[str, Any]], predictions: dict[str, dict[str, str]]) -> dict[str, Any]:
    negative = sum(_label(row['reference']['conclusions']) == 'failed' for row in rows)
    positive = sum(_label(row['reference']['conclusions']) == 'passed' for row in rows)
    false_accepts = false_rejects = unknown = 0
    for row in rows:
        prediction = predictions.get(row['output_id'], {})
        if (set(prediction) != set(row['ordinary_dimensions'])
                or any(value not in {'passed','failed'} for value in prediction.values())):
            unknown += 1
            continue
        expected, actual = _label(row['reference']['conclusions']), _label(prediction)
        false_accepts += expected == 'failed' and actual == 'passed'
        false_rejects += expected == 'passed' and actual == 'failed'
    return {'planned':len(rows), 'human_negative_denominator':negative, 'human_positive_denominator':positive,
        'false_accepts':false_accepts, 'false_rejects':false_rejects, 'errors':false_accepts+false_rejects,
        'unknown':unknown, 'agreements':len(rows)-unknown-false_accepts-false_rejects}


def _report(study: dict[str, Any], predictions: dict[str, dict[str, str]]) -> dict[str, Any]:
    rows = [row for row in study['samples'] if row['purpose'] == 'qualification']
    counts = _counts(rows,predictions)
    counts['by_type'] = {kind:_counts([row for row in rows if row['output_type'] == kind],predictions) for kind in OUTPUT_TYPES}
    counts['actual_original_subset'] = _counts([row for row in rows if row['source_kind'] == 'actual_execution'],predictions)
    counts['controlled_subset'] = _counts([row for row in rows if row['source_kind'] == 'human_controlled'],predictions)
    counts['fixture_subset'] = _counts([row for row in rows if row['source_kind'].startswith('fixture')],predictions)
    counts['source_task_ids'] = sorted({row['source_task_id'] for row in rows})
    counts['output_units_are_product_task_count'] = False
    counts['actual_original_outputs'] = counts['actual_original_subset']['planned']
    counts['fixture_reference_outputs'] = sum(row['reference']['authorship']=='fixture_reference' for row in rows)
    status = ('incomplete' if counts['unknown'] else 'qualified' if counts['false_accepts'] <= 1
        and counts['false_rejects'] <= 4 and counts['errors'] <= 5
        and all(row['errors'] <= 2 for row in counts['by_type'].values()) else 'failed')
    return {'actual_qualification':'not-started', 'fixture_status':status if study['mode']=='synthetic_test' else 'not-applicable',
        'qualification':counts, 'candidate_binding':study['candidate_binding'], 'candidate':study['candidate'],
        'candidate_fingerprints':study['candidate_fingerprints'],
        'study_binding':study['binding'], 'route':'human', 'user_acceptance':'pending',
        'claim':'Only this frozen 100-output scope can qualify; no population error rate, perfect reliability, first human baseline or user acceptance is inferred.'}


def qualification_report(study_path: Path, *, result_path: Path | None = None) -> dict[str, Any]:
    """Unstarted studies keep every planned output unknown, including the 50/50 denominators."""
    study = _read_study(study_path)
    if result_path is None:
        return _report(study, {})
    return _native_report(study_path,study,result_path)


def fixture_qualification_report(study_path: Path, predictions: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Exercise finite thresholds with explicit synthetic predictions, without granting real qualification."""
    study = _read_study(study_path)
    if study['mode'] != 'synthetic_test':
        raise ValueError('Fixture predictions cannot supply actual human or actual judge evidence.')
    if set(predictions) - {row['output_id'] for row in study['samples'] if row['purpose']=='qualification'}:
        raise ValueError('Predictions cannot supplement or replace the frozen 100 outputs.')
    return _report(study,predictions)



def judge_executions(study_path: Path, *, order_offset: int = 0) -> tuple['FrozenExecution', ...]:
    """Append the pre-frozen judge segment to the existing parent plan, never a new grant."""
    from tests.support.agent_acceptance.frozen import FrozenExecution
    study = _read_study(study_path)
    calibration = tuple(row['execution_id'] for row in study['judge_inputs'] if row['purpose']=='calibration')
    return tuple(FrozenExecution(execution_id=row['execution_id'],case_id='judge.'+row['purpose'],protocol='google',
        scenario_id=row['execution_id'],material_family_id=row['family_id'],material_variant='baseline',
        material_path=(SOURCE/study['source_relative_root'] if study['source_relative_root'] else study_path.parent)/row['material_path'],material_sha256=row['material_sha256'],
        startpoint=row['execution_id'],order=number,dependencies=calibration if row['purpose']=='qualification' else (),
        role='judge',stage='judge',judge_purpose=row['purpose'],request_cap=1,cost_cap_microusd=122880)
        for number,row in enumerate(study['judge_inputs'],order_offset+1))


def _decision(text: str, dimensions: list[str]) -> dict[str, str] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if (not isinstance(value,dict) or set(value) != {'conclusions','reasons'}
            or not isinstance(value['conclusions'],dict) or set(value['conclusions']) != set(dimensions)
            or any(item not in {'passed','failed','unknown'} for item in value['conclusions'].values())
            or not isinstance(value['reasons'],dict)
            or any(not isinstance(value['reasons'].get(name),str) or not value['reasons'][name].strip() for name in dimensions)):
        return None
    return dict(value['conclusions'])


def register_judge_study(registry: 'CaseRegistry', study_path: Path, *, fixed_replies: dict[str, 'Reply'],
                         preceding_cases: tuple[str, ...] = ()) -> None:
    """Register native SDK calls at the unified runner; only terminal Mock replies are injected."""
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
    import httpx2
    from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, SharedControlError
    study = _read_study(study_path)
    if study['mode'] != 'synthetic_test':
        raise ValueError('Actual judge transport remains unqualified; no real qualification is started.')
    frozen_binding = study['binding']
    scripted = dict(fixed_replies)
    async def judge(context: CaseContext) -> CaseOutcome:
        current = _read_study(study_path)
        if current['binding'] != frozen_binding:
            raise SharedControlError('candidate_refreeze_required; no judge send is allowed.')
        row = next((item for item in current['judge_inputs'] if item['execution_id']==context.execution_id), None)
        if row is None:
            raise SharedControlError('The frozen judge output ID is not registered; no send is allowed.')
        snapshot_dir = context.root.parent/('judge-study-'+frozen_binding[:16])
        if not snapshot_dir.exists():
            shutil.copytree(study_path.parent,snapshot_dir)
        snapshot = snapshot_dir/'judge-study.json'
        if _read_study(snapshot)['binding'] != frozen_binding:
            raise SharedControlError('The retained judge material package differs from its frozen original.')
        payload = json.loads(context.material_input)
        if context.material_input != (study_path.parent/row['material_path']).read_text(encoding='utf-8'):
            raise SharedControlError('candidate_refreeze_required; frozen dynamic judge input changed.')
        if len(context.material_input.encode('utf-8')) > 32768:
            path = context.root/'judge-input-unfit.txt'
            path.write_text('Complete evidence exceeds the frozen 32768 input bound; keep the original and use human review.\n',encoding='utf-8')
            return CaseOutcome('inconclusive',{'judge_output_id':row['output_id'],'actual_qualification':'not-started',
                'reason':'complete_input_unfit; human review required; no truncation or model send'}, {'input_unfit':path,'judge_study':snapshot})
        session = context.judge_session([scripted[row['output_id']]],purpose=row['purpose'])
        answer_text = ''
        failure_reason = None
        try:
            async with session.model() as model:
                answer = await Agent(model,retries=0).run(context.material_input,cancellation_token=context.cancellation_token)
                answer_text = answer.output
        except (ModelAPIError, UnexpectedModelBehavior, httpx2.HTTPError, asyncio.CancelledError) as error:
            failure_reason = type(error).__name__+': '+str(error)
        finally:
            session.save(context.root/'judge-exchange.json',attempts=[],run_id=context.run_id,
                case_id=context.case_id,task_id=context.execution_id)
        decision = _decision(answer_text,payload['ordinary_dimensions']) if failure_reason is None else None
        path = context.root/'judge-decision.json'
        path.write_text(json.dumps({'study_binding':frozen_binding,'candidate_binding':current['candidate_binding'],
            'output_id':row['output_id'],'purpose':row['purpose'],'round':row['round'],
            'raw_answer':answer_text,'exception':failure_reason,'conclusions':decision,'provenance':'fixed_fixture'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return CaseOutcome('passed' if decision is not None and 'unknown' not in decision.values() else 'inconclusive',
            {'study_binding':frozen_binding,'judge_output_id':row['output_id'],'judge_purpose':row['purpose'],
             'judge_round':row['round'],'judge_conclusions':decision,'external_exception':failure_reason,'actual_qualification':'not-started'},
            {'exchange':context.root/'judge-exchange.json','judge_decision':path,'judge_study':snapshot})
    for purpose in dict.fromkeys(row['purpose'] for row in study['judge_inputs']):
        registry.register(AcceptanceCase('judge.'+purpose,'request-input',('google',),judge,owner_ticket='23' if purpose=='scoring' else '22',
            bindings=(study_path,*(study_path.parent/name for name in study['files'])),frozen_material_input=True,
            dependencies=('judge.calibration',) if purpose=='qualification' else preceding_cases))


def _native_report(study_path: Path, study: dict[str, Any], result_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_bytes())
    planned = {row['execution_id']:row for row in result.get('planned_executions',[])}
    expected = {row.execution_id:row.model_dump(mode='json') for row in judge_executions(study_path)}
    if any(identity in planned and any(planned[identity][key] != value for key,value in row.items() if key != 'order')
            for identity,row in expected.items()):
        raise ValueError('Native results must preserve this exact frozen calibration and unique qualification segment.')
    predictions: dict[str, dict[str, str]] = {}
    completed: Counter[int] = Counter()
    calibration_predictions: dict[int, dict[str, dict[str, str]]] = {}
    observations = []
    seen = set()
    for case in result['cases']:
        identity = case['execution_id']
        if identity not in expected:
            continue
        if identity in seen:
            raise ValueError('A qualification output cannot receive replacement votes or duplicate results.')
        seen.add(identity)
        row = next(item for item in study['judge_inputs'] if item['execution_id']==identity)
        decision, issue = _native_prediction(study_path,study,result_path,case,row)
        if row['purpose']=='calibration' and decision is not None and 'unknown' not in decision.values():
            completed[row['round']] += 1
            calibration_predictions.setdefault(row['round'],{})[row['output_id']] = decision
        if row['purpose']=='qualification' and decision is not None:
            predictions[row['output_id']] = decision
        observations.append({'execution_id':identity,'output_id':row['output_id'],'purpose':row['purpose'],
            'round':row['round'],'conclusions':decision,'issue':issue,'original_case_status':case['case_status']})
    report = _report(study,predictions)
    report['calibration'] = {'planned_rounds':study['calibration_rounds'],
        'completed_rounds':sum(value==30 for value in completed.values()),
        'by_round':{str(number):_counts([row for row in study['samples'] if row['purpose']=='calibration'],
            calibration_predictions.get(number,{})) for number in range(1,study['calibration_rounds']+1)},
        'maximum_calls':60,'planned_calls':study['calibration_rounds']*30}
    if report['calibration']['completed_rounds'] != study['calibration_rounds']:
        report['fixture_status']='incomplete'
    report['judge_call_results']=observations
    report['source_result']=str(result_path)
    report['sending_authorized']=False
    return report






def qualification_applicability(report: dict[str, Any], candidate: JudgeCandidate, *, output_type: OutputType,
                                language: str, domain: str) -> dict[str, Any]:
    """A filename change does not change capability; actual candidate bytes and validated scopes do."""
    current = {'model':candidate.model,'prompt_sha256':sha256(candidate.prompt_path.read_bytes()).hexdigest(),
        'criteria_sha256':sha256(candidate.criteria_path.read_bytes()).hexdigest(),'input_processing':candidate.input_processing}
    old = report['candidate']
    changed = current != report['candidate_fingerprints']
    in_scope = (output_type in old['output_types'] and output_type in candidate.output_types
        and language in old['languages'] and language in candidate.languages
        and domain in old['domains'] and domain in candidate.domains)
    return {'scope_valid':not changed and in_scope,
        'reason':'candidate_refreeze_required' if changed else 'scope_not_verified' if not in_scope else 'same_frozen_scope',
        'actual_qualification':report['actual_qualification'],'fixture_status':report['fixture_status'],
        'sending_authorized':False,'affected_scope':{'output_type':output_type,'language':language,'domain':domain}}




def _native_prediction(study_path: Path, study: dict[str, Any], result_path: Path,
                       case: dict[str, Any], row: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """A missing/corrupt native original is unknown evidence, never a smaller denominator."""
    def path(value: str) -> Path:
        return Path(value) if Path(value).is_absolute() else result_path.parent/value
    evidence = case['evidence']
    if 'exchange' not in evidence or 'evidence_index' not in evidence:
        return None, 'native_original_not_available'
    try:
        index = json.loads(path(evidence['evidence_index']).read_bytes())
        body = path(evidence['exchange']).read_bytes()
        if index['execution_id'] != row['execution_id'] or index['links']['exchange']['sha256'] != sha256(body).hexdigest():
            return None, 'native_original_fingerprint_changed'
        exchange = json.loads(body)
        if exchange['product_attempts']:
            return None, 'judge_impersonated_product_attempt'
        wire = exchange['exchanges']
        if len(wire)!=1 or wire[0].get('status')!=200 or wire[0].get('error'):
            return None, 'native_call_incomplete_or_exception'
        sdk = exchange['sdk_inputs']
        identity = sdk[0]['request_identity'] if len(sdk)==1 else {}
        material = (study_path.parent/row['material_path']).read_text(encoding='utf-8')
        native = json.loads(wire[0]['request_utf8'])
        texts = [part.get('text') for content in native.get('contents',[]) for part in content.get('parts',[])]
        if (material not in texts or identity.get('actor')!='judge' or identity.get('purpose')!=row['purpose']
                or identity.get('judge_call_id')!=wire[0]['request_identity'].get('judge_call_id')
                or not identity.get('judge_call_id')):
            return None, 'sdk_identity_or_complete_input_changed'
        response = json.loads(wire[0]['received_utf8'])
        candidates = response.get('candidates',[])
        if len(candidates)!=1 or candidates[0].get('finishReason')!='STOP':
            return None, 'native_output_incomplete_or_truncated'
        parts = candidates[0].get('content',{}).get('parts',[])
        text = ''.join(part['text'] for part in parts if isinstance(part.get('text'),str) and not part.get('thought'))
        sample = next(item for item in study['samples'] if item['output_id']==row['output_id'])
        decision = _decision(text,sample['ordinary_dimensions'])
        return decision, None if decision is not None else 'unreadable_ordinary_judgment'
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        return None, 'required_native_original_unavailable: '+type(error).__name__


def _material_manifest(sample: JudgeSample, candidate: JudgeCandidate, reference: dict[str, Any]) -> str:
    """Bind the complete meaningful judge input and explicit exclusions, excluding opaque random identity."""
    hidden = set(reference.get('review_only_sha256',[]))
    originals = [{'sha256':sha256(path.read_bytes()).hexdigest(),
        'sent':path not in sample.review_only_paths and sha256(path.read_bytes()).hexdigest() not in hidden}
        for path in sample.original_paths]
    return digest({'candidate':candidate.model_dump(mode='json',exclude={'version','prompt_path','criteria_path'}),
        'prompt_sha256':sha256(candidate.prompt_path.read_bytes()).hexdigest(),
        'criteria_sha256':sha256(candidate.criteria_path.read_bytes()).hexdigest(),
        'output_id':sample.output_id,'output_type':sample.output_type,'source_task_id':sample.source_task_id,
        'family_id':sample.family_id,'language':sample.language,'domain':sample.domain,
        'ordinary_dimensions':sample.ordinary_dimensions,'input_sha256':sha256(sample.input_path.read_bytes()).hexdigest(),
        'output_sha256':sha256(sample.output_path.read_bytes()).hexdigest(),'originals':originals,
        'explicit_exclusions':sorted(sha256(path.read_bytes()).hexdigest() for path in sample.review_only_paths)})


def judge_material_manifest(samples: list[JudgeSample], candidate: JudgeCandidate) -> dict[str, str]:
    """Prepare the exact per-output manifest an actual human must review and attest before freezing."""
    return {row.output_id:_material_manifest(row,candidate,_reference(row)) for row in samples}
