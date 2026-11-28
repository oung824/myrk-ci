"""Actual completed/cancelled tasks survive a later source-summary correction."""
import asyncio
import json
from pathlib import Path
import shutil
from uuid import uuid4
import pytest
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet


@pytest.fixture
def correction_packet(request, monkeypatch):
    variant = getattr(request, 'param', None)
    if variant in {'variant', 'desktop-variant'}:
        from tests.support.agent_acceptance import quality_compaction
        from tests.support.agent_acceptance.quality_compaction_correction import ATTENDANCE, ROSTER
        from tests.support.agent_acceptance.quality_runtime import QualitySource

        original_projection = quality_compaction.project_input

        def project_variant(visible):
            visible = original_projection(visible)
            if visible.adapter != 'qg6-9':
                return visible
            documents = [{**doc, 'facts': {**doc['facts'], 'shifts': 14}} for doc in visible.parameters['source_documents']]
            sources = {name: source.model_copy(update={'text': source.text.replace('"shifts": 9', '"shifts": 14')})
                for name, source in visible.sources.items()}
            if variant == 'desktop-variant':
                sources = dict(list(sources.items())[:4])
            sources[ATTENDANCE] = QualitySource(text='Frozen attendance register. volunteers=31.\n', encoding='utf-8-sig')
            sources[ROSTER] = QualitySource(text='Frozen roster check. Only the stopped task may read this.\n')
            return visible.model_copy(update={
                'goal': visible.goal.replace('from12 to9', 'from13 to14'),
                'parameters': {**visible.parameters, 'source_documents': documents, 'historical_shift_count': 13, 'current_shift_count': 14},
                'sources': sources,
            })

        monkeypatch.setattr(quality_compaction, 'project_input', project_variant)
    parent=SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
    root=parent/('correction-'+uuid4().hex)
    try:
        yield build_implementation_quality_packet(root)
    finally:
        assert root.parent==parent
        if root.exists():shutil.rmtree(root)


@pytest.mark.parametrize('correction_packet,counts', [('legacy', (12, 9)), ('variant', (13, 14))], indirect=['correction_packet'])
def test_qg6_correction_preserves_real_completed_and_cancelled_tasks(correction_packet,acceptance_public_root,counts):
    from tests.support.agent_acceptance.quality_compaction_correction import exercise
    outcome=asyncio.run(exercise(correction_packet,task_id='QG6.formal.9',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed',outcome.facts
    facts=outcome.facts['quality_compaction_contract']
    assert facts['actual_attendance_completed'] and facts['actual_roster_cancelled']
    assert facts['distinct_task_ids'] and facts['old_tasks_unchanged']
    assert facts['first_normal_summary_id'] and facts['old_summary_original_unchanged']
    old_summary = json.loads(outcome.evidence['old_summary'].read_bytes())
    assert f'had {counts[0]} shifts' in old_summary['summary']
    assert f'shifts={counts[0]}' not in old_summary['summary']
    assert outcome.critical_contracts['K4']=='unknown'
    assert (facts['historical_shift_count'], facts['current_shift_count']) == counts
    assert facts['normal_summaries']>=1 and facts['correction_applied_after_summary']
    assert facts['all_actual_sources_read'] and facts['source_mapping_readable']
    assert facts['no_completed_or_cancelled_source_reread']
    assert facts['source_originals_unchanged'] and facts['actual_answer_delivered']
    assert facts['cancelled_request_usage_unmeasured']
    assert outcome.facts['formal_execution'] is False
    assert outcome.facts['real_http_attempts']==0 and outcome.semantic_status=='not_measured'
    assert outcome.critical_contracts['K3']=='passed'
    wire=json.loads(outcome.evidence['exchange'].read_bytes())
    assert 0<len(wire['exchanges'])<=60
    assert f'correction from {counts[0]} to {counts[1]}' not in json.dumps(wire['sdk_inputs'][0])
    if counts == (13, 14):
        materials = acceptance_public_root / 'materials'
        assert (materials / 'attendance.txt').read_bytes() == 'Frozen attendance register. volunteers=31.\n'.encode('utf-8-sig')
        assert (materials / 'roster-check.txt').read_text(encoding='utf-8') == 'Frozen roster check. Only the stopped task may read this.\n'
    from hashlib import sha256
    packet_root=(SOURCE/correction_packet.path).parent
    assert packet_root.parent==SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
    shutil.rmtree(packet_root)
    assert sha256(outcome.evidence['quality.freeze'].read_bytes()).hexdigest()==correction_packet.sha256
    assert outcome.evidence['quality.input'].is_file() and outcome.evidence['quality.gold'].is_file()
    assert outcome.facts['output_domain_criteria']['human_review']=='not_performed'


@pytest.mark.parametrize('missing', ['attendance.txt', 'roster-check.txt'])
def test_qg6_explicit_correction_requires_frozen_prior_task_sources(monkeypatch, missing):
    from tests.support.agent_acceptance import quality_compaction
    from tests.support.agent_acceptance.quality_packet import PACKETS

    original_projection = quality_compaction.project_input

    def project_without_prior_source(visible):
        projected = original_projection(visible)
        if projected.adapter != 'qg6-9':
            return projected
        sources = {name: source for name, source in projected.sources.items() if name != missing}
        return projected.model_copy(update={'sources': sources})

    monkeypatch.setattr(quality_compaction, 'project_input', project_without_prior_source)
    packet = SOURCE / PACKETS / ('correction-missing-prior-' + uuid4().hex)
    try:
        with pytest.raises(ValueError, match='both frozen prior-task sources'):
            build_implementation_quality_packet(packet)
        assert not (packet / 'input/QG6.formal.9.json').exists()
        assert not (packet / 'gold/QG6.formal.9.json').exists()
        assert not (packet / 'freeze.json').exists()
    finally:
        if packet.is_dir():
            shutil.rmtree(packet)



def test_qg6_score_rejects_escape_and_early_reads_and_keeps_missing_unknown(correction_packet,acceptance_public_root):
    from copy import deepcopy
    from tests.support.agent_acceptance.quality_compaction_correction import exercise
    from tests.support.agent_acceptance.quality_runtime import score
    outcome=asyncio.run(exercise(correction_packet,task_id='QG6.formal.9',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed',outcome.facts
    record=json.loads(outcome.evidence['conversation'].read_bytes())
    wire=json.loads(outcome.evidence['exchange'].read_bytes())
    outside=deepcopy(record)
    primary=next(action for action in outside['turns'][-1]['actions'] if action['kind']=='read' and action['status']=='completed')
    primary['prepared_parameters']['path']=str(acceptance_public_root.parent/'outside-other-task'/Path(primary['prepared_parameters']['path']).name)
    escaped=score('quality.qg6.formal.9',acceptance_public_root,[outside],[wire])
    assert escaped['critical_contracts']['K1']=='failed',escaped
    early=deepcopy(record)
    primary=next(action for action in early['turns'][-1]['actions'] if action['kind']=='read' and action['status']=='completed')
    early['turns'][0]['actions'].append(deepcopy(primary))
    premature=score('quality.qg6.formal.9',acceptance_public_root,[early],[wire])
    assert premature['critical_contracts']['K1']=='failed',premature
    missing=score('quality.qg6.formal.9',acceptance_public_root,[],[wire])
    assert missing['critical_contracts']['K1']=='unknown' and missing['critical_contracts']['K3']=='unknown'
    assert missing['passed'] is False
    alternate_delivery=deepcopy(record)
    alternate_delivery['turns'][-1]['answer']='The requested roster report has been prepared.'
    delivered=score('quality.qg6.formal.9',acceptance_public_root,[alternate_delivery],[wire])
    assert delivered['passed'] is True and delivered['critical_contracts']['K4']=='unknown'
    alternate_delivery['turns'][-1]['answer']=' '
    assert score('quality.qg6.formal.9',acceptance_public_root,[alternate_delivery],[wire])['passed'] is False
    altered_summary = deepcopy(record)
    old_path = outcome.evidence['old_summary']
    original_bytes = old_path.read_bytes()
    original = json.loads(original_bytes)
    alternate = {**original, 'summary': 'The historical roster contained twelve shifts in archived material.'}
    turn = altered_summary['turns'][-1]
    summary_index = next(index for index, item in enumerate(turn['compactions']) if item['id'] == original['id'])
    turn['compactions'][summary_index] = alternate
    old_path.write_text(json.dumps(alternate), encoding='utf-8')
    try:
        assert not score('quality.qg6.formal.9',acceptance_public_root,[altered_summary],[wire])['passed']
    finally:
        old_path.write_bytes(original_bytes)
    broken_first = deepcopy(record)
    broken_first['turns'][-1]['compactions'][summary_index]['id'] = 'not-the-original-id'
    assert score('quality.qg6.formal.9',acceptance_public_root,[broken_first],[wire])['passed'] is False


def test_candidate_cancellation_keeps_the_original_genesis_grant(prepared_root,acceptance_public_root):
    from pydantic_ai import CancellationToken
    from myrk.conversation import Conversation, Task
    from tests.acceptance.test_candidate_refreeze import synthetic_parent,execute_one,approve_candidate
    from tests.support.agent_acceptance.replay import OfflineSession
    registry,plan,gate=synthetic_parent(prepared_root)
    index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        candidate=gate.propose_candidate()
        current=approve_candidate(gate,candidate)
        before=json.loads((prepared_root/'ledger.json').read_bytes())
        conversation=Conversation(acceptance_public_root/'cancelled-before-send.json')
        session=OfflineSession('deepseek',[])
        token=CancellationToken()
        token.cancel()
        async def cancel():
            async with session.model() as model:
                return await conversation.ask('Cancel this independent synthetic task before sending.',document=None,
                    task=Task(goal='Cancel before sending.',scope=None,provider_config=session.config),model=model,cancellation_token=token)
        turn=asyncio.run(cancel())
        assert turn.status=='cancelled' and not turn.request_attempts and not session.exchanges
        current.for_execution('formal').observe_product_stop(conversation.path,case_id='synthetic.candidate',protocol='deepseek')
        after=json.loads((prepared_root/'ledger.json').read_bytes())
        assert after['authorization']==before['authorization']
        assert after['authorization_binding']==before['authorization_binding']
        assert after['attempts']==before['attempts'] and after['binding']==before['binding']
        assert 'formal' in after['case_stops']
        assert after['candidates']==before['candidates'] and after['active_candidates']==before['active_candidates']
    finally:
        if index.exists():index.unlink()


def test_task_scoped_compaction_resolves_its_real_task_without_a_work_step(correction_packet,prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance import AcceptancePlan,LiveAuthorization,LiveGate,builtin_registry,prepare_live
    from tests.support.agent_acceptance.core import SharedControlError
    from tests.support.agent_acceptance.quality_compaction_correction import exercise
    outcome=asyncio.run(exercise(correction_packet,task_id='QG6.formal.9',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    original=outcome.evidence['conversation'].read_bytes()
    record=json.loads(original)
    primary=record['turns'][-1]
    request=next(item for item in primary['request_attempts'] if item['purpose']=='compaction')
    assert request['step_id'] is None and request['observation']['wire']
    registry=builtin_registry()
    bundle=prepare_live(AcceptancePlan(cases=['formal.parallel-stop'],mode='prepare-live'),root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-compaction-ownership-no-send',binding=bundle.binding,transport='mock',
        total_limit=24,case_limits={'formal.parallel-stop':24},total_microusd=24*122880,case_microusd={'formal.parallel-stop':24*122880})
    gate=LiveGate(prepared_root,registry,grant)
    records=prepared_root/'.scratch/direct-compaction-records'
    records.mkdir(parents=True)
    source=records/'actual-compaction.json'
    source.write_bytes(original)
    parameters=dict(case_id='formal.parallel-stop',protocol='deepseek',actor=request['actor'],purpose=request['purpose'],
        product_number=request['number'],product_step_id=request['step_id'],delegation_id=request['delegation_id'],
        sdk_input_id='direct-real-record-ownership-control-no-new-SDK',request_sha256=request['observation']['wire']['body_sha256'],product_records_root=records)
    with pytest.raises(SharedControlError,match='one actual product Task'):
        gate.reserve(**parameters)
    assert gate.accounting_report()['reserved_requests']==0
    pending=(acceptance_public_root/'quality-correction-first-summary-pending.original.json').read_bytes()
    saved_pending=json.loads(pending)
    assert saved_pending['turns'][-1]['status']=='running'
    assert saved_pending['turns'][-1]['request_attempts'][-1]['status']=='sent'
    source.write_bytes(pending)
    from myrk.conversation import Conversation,Task
    from myrk.provider import ProviderConfig
    from tests.support.agent_acceptance.replay import OfflineSession,Reply
    from myrk.conversation import ConversationRecord
    from tests.support.agent_acceptance.compaction import history
    old_session=OfflineSession('deepseek',[Reply(status=503),Reply(status=503),
        Reply(text='Separate synthetic history summary; the original read-only source history remains available.'),
        Reply(text='Separate synthetic ownership-control summary completed.')],config=ProviderConfig.model_validate(primary['task']['provider_config']))
    old_path=acceptance_public_root/'separate-completed-owner.json'
    old_path.write_text(ConversationRecord(version=9,messages=history(),turns=[]).model_dump_json(),encoding='utf-8')
    old_conversation=Conversation.open(old_path)
    old_task=Task(goal='Keep this independent synthetic history read only; report its retained original source evidence.',scope=None,provider_config=old_session.config)
    async def complete_old():
        async with old_session.model() as model:
            return await old_conversation.ask(old_task.goal,document=None,task=old_task,scope=old_task.scope,model=model)
    old_turn=asyncio.run(complete_old())
    assert old_turn.status=='completed'
    old_summary=next(item for item in old_turn.request_attempts if item.purpose=='compaction' and item.number==request['number'])
    assert old_summary.step_id is None and old_summary.number==request['number'] and old_task.id!=primary['task']['id']
    (records/'actual-completed-other-task.json').write_bytes(old_conversation.path.read_bytes())
    number=gate.reserve(**parameters)
    after=gate.accounting_report()
    assert after['product_request_owners'][str(number)]['task_id']==primary['task']['id']
    assert after['reserved_requests']==1 and after['actual_http']==0
    gate.mark_not_sent(number,fault_origin='direct ownership regression never acquired a terminal sender')
    for changes in ({'purpose':'work'},{'actor':'action_review'},{'product_number':9999}):
        with pytest.raises(SharedControlError):gate.reserve(**{**parameters,**changes})
    assert gate.accounting_report()['reserved_requests']==1 and gate.accounting_report()['actual_http']==0
    (acceptance_public_root/'direct-compaction-reservation-ledger.original.json').write_bytes((prepared_root/'ledger.json').read_bytes())
    (acceptance_public_root/'direct-compaction-prepared.original.json').write_bytes((prepared_root/'prepared.json').read_bytes())


def test_normal_summary_with_historical_cancelled_goal_never_waits_for_another_stop(correction_packet,acceptance_public_root):
    import httpx2
    from tests.support.agent_acceptance.quality_compaction_correction import exercise,goals
    from tests.support.agent_acceptance.quality_runtime import bound_task,terminal_response_barrier
    outcome=asyncio.run(exercise(correction_packet,task_id='QG6.formal.9',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    wire=json.loads(outcome.evidence['exchange'].read_bytes())
    sdk=next(row for row in wire['sdk_inputs'] if row['request_identity']['purpose']=='compaction')
    _,visible,_=bound_task('quality.qg6.formal.9',acceptance_public_root)
    assert goals(visible)['roster'] in json.dumps(sdk,ensure_ascii=False)
    sdk=json.loads(json.dumps(sdk,ensure_ascii=False).replace(goals(visible)['primary'],'[recent primary user part retained outside this synthetic summary prefix]'))
    (acceptance_public_root/'synthetic-summary-prefix.json').write_text(json.dumps({'provenance':'synthetic projection of retained prefix; actual original remains in exchange.json','sdk':sdk},ensure_ascii=False),encoding='utf-8')
    assert goals(visible)['primary'] not in json.dumps(sdk,ensure_ascii=False)
    response=httpx2.Response(200,json={'synthetic':'this local object is not another terminal send'})
    async def verify():
        return await asyncio.wait_for(terminal_response_barrier('quality.qg6.formal.9',acceptance_public_root,0,sdk,response),.1)
    assert asyncio.run(verify()) is response
    marker=acceptance_public_root/'quality-correction-roster-inflight.json'
    assert not marker.exists()
    roster=next(row for row in wire['sdk_inputs'] if row['request_identity']['purpose']=='work'
        and goals(visible)['roster'] in json.dumps(row,ensure_ascii=False) and goals(visible)['primary'] not in json.dumps(row,ensure_ascii=False))
    async def cancel_roster():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(terminal_response_barrier('quality.qg6.formal.9',acceptance_public_root,1,roster,response),.1)
    asyncio.run(cancel_roster())
    original_marker=marker.read_bytes()
    future_work=json.loads(json.dumps(sdk))
    future_work['request_identity']['purpose']='work'
    async def later_work():
        return await asyncio.wait_for(terminal_response_barrier('quality.qg6.formal.9',acceptance_public_root,2,future_work,response),.1)
    assert asyncio.run(later_work()) is response and marker.read_bytes()==original_marker
