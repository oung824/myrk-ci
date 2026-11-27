"""Versioned classification of stored actual preview facts; no Main or model launch."""

from pathlib import Path
from zipfile import ZipFile
import pytest
from tests.support.agent_acceptance.text_image_preview import _outcome

ARCHIVE = Path(__file__).parents[2] / '.scratch/retro-agent-live-acceptance-20261116/implementation/t18/checks/originals-02.zip'


@pytest.fixture
def preview_original(tmp_path):
    root = tmp_path / 'actual-preview-original'
    with ZipFile(ARCHIVE) as archive:
        prefix = next(name.rsplit('/work/observation.json', 1)[0] for name in archive.namelist()
            if name.startswith('b-v9a3jgye.') and name.endswith('/work/observation.json'))
        for name in archive.namelist():
            if name.startswith(prefix + '/'):
                path = root / name[len(prefix) + 1:]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
    source_archive = ARCHIVE.parents[1] / 'sources/b-v9a3jgye-exact-source.zip'
    with ZipFile(source_archive) as archive:
        proof = root / 'source-proof/driver.mjs'
        proof.parent.mkdir()
        proof.write_bytes(archive.read('tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs'))
    return root


def test_stored_full_access_preview_does_not_claim_an_approval_review_pass(preview_original):
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts['K2'] == 'not_applicable'


def test_observed_source_write_stays_failed_when_unrelated_driver_capture_fails(preview_original):
    import json
    path = preview_original / 'work/observation.json'
    state = json.loads(path.read_bytes())
    state['source_checks'][1]['rows'][0]['sha256'] = '0' * 64
    state['passed'] = False
    state['error'] = 'controlled unrelated capture failure; no new product execution'
    path.write_text(json.dumps(state), encoding='utf-8')
    outcome = _outcome(preview_original, 1)
    assert outcome.tool_status == 'failed'
    assert outcome.critical_contracts['K1'] == 'failed'


def test_required_approval_cannot_be_hidden_by_full_access_label(preview_original):
    import json
    path = preview_original / 'saved-chats.json'
    chats = json.loads(path.read_bytes())
    action = chats[0]['turn']['actions'][0]
    action['approval_settings']['condition'] = 'on_request'
    action['approval_settings']['review_mandatory'] = True
    action['approval_settings']['reviewer'] = 'assistant'
    path.write_text(json.dumps(chats), encoding='utf-8')
    outcome = _outcome(preview_original, 1)
    assert outcome.critical_contracts['K2'] == 'failed'


def test_observed_reopen_http_attempt_fails_k3_without_erasing_other_contracts(preview_original):
    import json
    path = preview_original / 'reopen/exchange.json'
    exchange = json.loads(path.read_bytes())
    work = json.loads((preview_original / 'work/exchange.json').read_bytes())
    exchange['exchanges'].append(work['exchanges'][0])
    path.write_text(json.dumps(exchange), encoding='utf-8')
    outcome = _outcome(preview_original, 1)
    assert outcome.critical_contracts['K3'] == 'failed'
    assert outcome.critical_contracts['K1'] == 'passed'
    assert outcome.critical_contracts['K2'] == 'not_applicable'


def test_ready_text_claim_must_match_the_retained_original_even_after_capture_error(preview_original):
    import json
    path = preview_original / 'work/observation.json'
    state = json.loads(path.read_bytes())
    state['previews'][0]['content']['text'] = 'controlled incorrect displayed bytes'
    path.write_text(json.dumps(state), encoding='utf-8')
    outcome = _outcome(preview_original, 1)
    assert outcome.critical_contracts['K4'] == 'failed'
    assert outcome.critical_contracts['K3'] == 'passed'


def test_actual_reading_position_violation_fails_k5_despite_other_missing_evidence(preview_original):
    import json
    path = preview_original / 'work/observation.json'
    state = json.loads(path.read_bytes())
    state['image_reading']['afterTop'] += 30
    del state['image_reading']['zoom']
    state['passed'] = False
    path.write_text(json.dumps(state), encoding='utf-8')
    outcome = _outcome(preview_original, 1)
    assert outcome.critical_contracts['K5'] == 'failed'
    assert outcome.critical_contracts['K1'] == 'passed'


@pytest.mark.parametrize('missing', ['processes.json', 'expected/criteria.json'])
def test_missing_original_is_unknown_instead_of_a_capture_exception(preview_original, missing):
    (preview_original / missing).unlink()
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts['K5'] == 'unknown'
    assert outcome.product_status == 'inconclusive'


def test_specific_program_assertion_proof_restores_only_the_matching_old_facts(preview_original):
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts == {'K1':'passed','K2':'not_applicable','K3':'passed','K4':'passed','K5':'passed'}
    assert outcome.product_status == 'passed'
    assert set(outcome.facts['program_assertion_evidence']) == {'report_fulltext','long_fulltext','fixed_snapshot','refresh_fulltext'}


@pytest.mark.parametrize(('contract','filename','keys','measurement'), [
    ('K1','work/observation.json',('source_checks',),'source_rows:text read'),
    ('K2','saved-chats.json',(0,'turn','task','approval','mode'),None),
    ('K3','reopen/exchange.json',('exchanges',),'reopen_terminal_http_zero'),
    ('K4','saved-chats.json',(0,'artifacts',0,'delivered_sha256'),None),
    ('K5','work/observation.json',('image_reading','afterTop'),'image_reading_position'),
])
def test_missing_contract_field_is_unknown_with_global_passed_still_true(preview_original, contract, filename, keys, measurement):
    import json
    path = preview_original / filename
    original = json.loads(path.read_bytes())
    item = original
    for key in keys[:-1]:
        item = item[key]
    del item[keys[-1]]
    path.write_text(json.dumps(original), encoding='utf-8')
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts[contract] == 'unknown'
    if measurement:
        assert outcome.facts['contract_measurements'][contract][measurement] is None


@pytest.mark.parametrize('damage', ['missing_source','changed_source','missing_binding','wrong_launch','missing_marker','wrong_run','missing_gold','unreviewed_control_flow'])
def test_program_evidence_requires_its_specific_original_linkages(preview_original, damage):
    import json
    if damage == 'missing_source':
        (preview_original / 'source-proof/driver.mjs').unlink()
    elif damage == 'changed_source':
        path = preview_original / 'source-proof/driver.mjs'
        path.write_bytes(path.read_bytes() + b' controlled changed source')
    elif damage == 'unreviewed_control_flow':
        from hashlib import sha256
        path = preview_original / 'source-proof/driver.mjs'
        source = path.read_bytes().replace(b"assert.equal(await execute(", b"if(false) assert.equal(await execute(", 1)
        path.write_bytes(source)
        binding_path = preview_original / 'execution-bindings.json'
        binding = json.loads(binding_path.read_bytes())
        binding['sources']['tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs'] = sha256(source).hexdigest()
        binding_path.write_text(json.dumps(binding), encoding='utf-8')
    elif damage == 'missing_gold':
        (preview_original / 'expected/long.txt').unlink()
    else:
        filename = {'missing_binding':'execution-bindings.json','wrong_launch':'processes.json','missing_marker':'work/observation.json','wrong_run':'work/exchange.json'}[damage]
        path = preview_original / filename
        value = json.loads(path.read_bytes())
        if damage == 'missing_binding':
            del value['sources']['tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs']
        elif damage == 'wrong_launch':
            value['observations'][0]['processes'][0]['direct_spawn']['argv'][-1] = 'unrelated-driver.mjs'
        elif damage == 'missing_marker':
            value['source_checks'] = [row for row in value['source_checks'] if row['label'] != 'explicit refresh read only']
        else:
            value['run_id'] = 'different-source-run'
        path.write_text(json.dumps(value), encoding='utf-8')
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts['K4'] == outcome.critical_contracts['K5'] == 'unknown'
    assert not all(row['verified'] for row in outcome.facts['program_assertion_evidence'].values())


def test_actual_contracts_do_not_depend_on_the_runner_capture_exit(preview_original):
    outcome = _outcome(preview_original, 1)
    assert outcome.tool_status == 'failed'
    assert outcome.product_status == 'passed'
    assert outcome.critical_contracts == {'K1':'passed','K2':'not_applicable','K3':'passed','K4':'passed','K5':'passed'}


def test_missing_approval_event_original_prevents_conditional_na(preview_original):
    (preview_original / 'work/public-events.json').unlink()
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts['K2'] == 'unknown'
    assert outcome.tool_status == 'failed'


def test_known_review_denial_followed_by_execution_is_k2_failure(preview_original):
    import json
    from myrk.review import ReviewRecord
    path = preview_original / 'saved-chats.json'
    chats = json.loads(path.read_bytes())
    chats[0]['turn']['actions'][0]['review'] = ReviewRecord(status='denied').model_dump(mode='json')
    path.write_text(json.dumps(chats), encoding='utf-8')
    assert _outcome(preview_original, 1).critical_contracts['K2'] == 'failed'


@pytest.mark.parametrize('gold', ['before.txt','criteria.json','long.txt'])
def test_changed_independent_gold_is_invalid_evidence_not_a_product_failure(preview_original, gold):
    path = preview_original / 'expected' / gold
    path.write_bytes(path.read_bytes() + b' controlled changed independent reference')
    outcome = _outcome(preview_original, 0)
    assert outcome.critical_contracts['K5'] == 'unknown'
    assert outcome.product_status == 'inconclusive'
