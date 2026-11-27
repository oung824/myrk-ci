"""Complete saved owner originals stay verifiable without bloating the live ledger."""
from base64 import b64decode
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.support.agent_acceptance import AcceptancePlan, LiveAuthorization, LiveGate, builtin_registry, prepare_live


@pytest.fixture
def original_source():
    fixture = Path(__file__).parent / 'materials/owner-codec-20261117'
    index = json.loads((fixture / 'index.json').read_bytes())
    with ZipFile(fixture / 'originals.zip') as archive:
        bodies = {item['member']: archive.read(item['member']) for item in index['members']}
    for item in index['members']:
        assert len(bodies[item['member']]) == item['bytes'] and sha256(bodies[item['member']]).hexdigest() == item['sha256']
    return bodies


@pytest.fixture
def owner_gate(prepared_root, original_source):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=['formal.parallel-stop'], mode='prepare-live'), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-owner-codec-no-terminal', binding=bundle.binding, transport='mock',
        total_limit=24, case_limits={'formal.parallel-stop': 24}, total_microusd=24 * 122880, case_microusd={'formal.parallel-stop': 24 * 122880})
    gate = LiveGate(prepared_root, registry, grant)
    records = prepared_root / '.scratch/actual-owner-codec-records'
    records.mkdir(parents=True)
    source = records / 'saved-original.json'
    raw = original_source['first-record.json']
    source.write_bytes(raw)
    record = json.loads(raw)
    turn = record['turns'][-1]
    attempt = next(item for item in turn['request_attempts'] if item['number'] == 4)
    identity = {key: attempt[key] for key in ('number', 'actor', 'purpose', 'delegation_id')}
    exchange = json.loads(original_source['exchange.json'])
    sdk = next(item for item in exchange['sdk_inputs'] if item['request_identity'] == identity)
    wire = next(item for item in exchange['exchanges'] if item['sdk_input_id'] == sdk['sdk_input_id'])
    parameters = dict(case_id='formal.parallel-stop', protocol='deepseek', actor=attempt['actor'], purpose=attempt['purpose'],
        product_number=attempt['number'], product_step_id=attempt['step_id'], delegation_id=attempt['delegation_id'],
        sdk_input_id=sdk['sdk_input_id'], request_sha256=sha256(wire['request_utf8'].encode()).hexdigest(), product_records_root=records)
    return gate, parameters, raw


def test_public_owner_report_keeps_exact_raw_while_live_ledger_uses_explicit_lossless_codec(owner_gate):
    gate, parameters, raw = owner_gate
    number = gate.reserve(**parameters)
    gate.mark_not_sent(number, fault_origin='codec regression has no terminal transport')
    ledger_path = gate.root / 'ledger.json'
    original_ledger = ledger_path.read_bytes()
    stored = json.loads(original_ledger)['product_request_owners'][str(number)]['sources'][0]
    assert stored.get('original_codec') == 'gzip-base64-v1', stored.keys()
    assert 'original_base64' not in stored
    assert len(original_ledger) < len(raw)
    report = gate.accounting_report()
    public = report['product_request_owners'][str(number)]['sources'][0]
    assert b64decode(public['original_base64'], validate=True) == raw
    assert set(public) == {'task_id', 'path', 'sha256', 'bytes', 'original_base64'}
    assert public['sha256'] == sha256(raw).hexdigest() and public['bytes'] == len(raw)
    assert gate.accounting_report()['reserved_requests'] == gate.accounting_report()['not_sent'] == 1
    assert gate.accounting_report()['actual_http'] == 0 and ledger_path.read_bytes() == original_ledger
def write_control_ledger(gate, ledger):
    from tests.support.agent_acceptance.preparation import digest
    ledger['integrity'] = digest({key: value for key, value in ledger.items() if key != 'integrity'})
    (gate.root / 'ledger.json').write_text(json.dumps(ledger), encoding='utf-8')


def test_legacy_owner_raw_is_readable_without_rewriting_its_ledger(owner_gate):
    from tests.support.agent_acceptance.preparation import restore_product_original
    gate, parameters, raw = owner_gate
    number = gate.reserve(**parameters)
    gate.mark_not_sent(number, fault_origin='legacy read-only regression has no terminal transport')
    public = gate.accounting_report()['product_request_owners'][str(number)]['sources'][0]
    ledger = json.loads((gate.root / 'ledger.json').read_bytes())
    ledger['product_request_owners'][str(number)]['sources'] = [public]
    write_control_ledger(gate, ledger)
    original = (gate.root / 'ledger.json').read_bytes()
    assert restore_product_original(public) == raw
    gate.validate(case_id='formal.parallel-stop', protocol='deepseek')
    reopened = LiveGate(gate.root, gate.registry, gate.authorization)
    assert reopened.accounting_report()['product_request_owners'][str(number)]['sources'][0] == public
    assert (gate.root / 'ledger.json').read_bytes() == original


@pytest.mark.parametrize('damage', ['base64', 'gzip', 'deflate', 'length', 'bool_length', 'sha', 'codec', 'both',
    'record', 'task', 'owner_binding'])
def test_invalid_owner_codec_refuses_before_new_reservation_and_http_state(owner_gate, damage):
    from base64 import b64encode
    import gzip
    from tests.support.agent_acceptance.core import SharedControlError
    gate, parameters, raw = owner_gate
    number = gate.reserve(**parameters)
    gate.mark_not_sent(number, fault_origin='damaged codec control never acquires terminal transport')
    ledger = json.loads((gate.root / 'ledger.json').read_bytes())
    source = ledger['product_request_owners'][str(number)]['sources'][0]
    if damage == 'base64':
        source['original_compressed_base64'] = '!!invalid-base64!!'
    elif damage == 'gzip':
        source['original_compressed_base64'] = b64encode(gzip.compress(raw, mtime=0)[:20]).decode()
    elif damage == 'deflate':
        source['original_compressed_base64'] = b64encode(bytes.fromhex('1f8b0800000000000003060000000000000000')).decode()
    elif damage == 'length':
        source['bytes'] += 1
    elif damage == 'bool_length':
        source['bytes'] = True
    elif damage == 'sha':
        source['sha256'] = '0' * 64
    elif damage == 'codec':
        source['original_codec'] = 'unknown-codec-v99'
    elif damage == 'both':
        source['original_base64'] = b64encode(raw).decode()
    elif damage == 'record':
        changed = json.loads(raw)
        del changed['turns'][-1]['task']['scope']
        changed_raw = json.dumps(changed).encode()
        source['original_compressed_base64'] = b64encode(gzip.compress(changed_raw, mtime=0)).decode()
        source['bytes'] = len(changed_raw)
        source['sha256'] = sha256(changed_raw).hexdigest()
    elif damage == 'task':
        source['task_id'] = 'not-the-recorded-task'
    else:
        source['task_id'] = json.loads(raw)['turns'][0]['task']['id']
    write_control_ledger(gate, ledger)
    original = (gate.root / 'ledger.json').read_bytes()
    with pytest.raises(SharedControlError, match='owner original'):
        gate.reserve(**parameters)
    assert (gate.root / 'ledger.json').read_bytes() == original
    with pytest.raises(SharedControlError, match='owner original'):
        gate.mark_http_started(number)
    assert (gate.root / 'ledger.json').read_bytes() == original
    assert len(json.loads(original)['attempts']) == 1 and ledger['attempts'][0]['state'] == 'not_sent'
