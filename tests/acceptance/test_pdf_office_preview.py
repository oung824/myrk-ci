"""PDF/Office acceptance observes actual Main and fixed local source content."""

import asyncio
import pytest
from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


@pytest.mark.acceptance_desktop
def test_actual_document_pages_keep_independent_content_and_read_only_sources(acceptance_public_root):
    registry = builtin_registry()
    result = asyncio.run(run_plan(AcceptancePlan(cases=['preview.document-content']), root=acceptance_public_root, registry=registry)).cases[0]
    assert result.tool_status == 'passed', result.facts
    assert result.semantic_status == 'not_measured' and result.user_acceptance == 'pending'
    assert result.facts['preview_http_attempts'] == 0
    assert result.facts['setup_http_attempts'] == 5
    assert result.facts['owned_process_closure'] is True
    rows = result.facts['documents']
    assert [row['scenario'] for row in rows] == ['L14', 'L15', 'L16', 'L17']
    for row in rows:
        assert row['source_unchanged'] is True
        assert row['resources_released'] is True
        if row['status'] == 'passed':
            assert row['independent_content_matches'] is True
            assert row['all_pages_observed'] is True
            assert row['zoom_observed'] is True
            assert row['exact_owned_identity_exited'] is True
        else:
            assert row['status'] in {'failed', 'unknown'} and row['original_error']
    assert result.product_status == ('passed' if all(row['status']=='passed' for row in rows) else 'failed')

@pytest.mark.acceptance_desktop
def test_actual_invalid_document_refusals_preserve_sources_and_foreign_processes(acceptance_public_root):
    registry = builtin_registry()
    result = asyncio.run(run_plan(AcceptancePlan(cases=['preview.document-refusal']), root=acceptance_public_root, registry=registry)).cases[0]
    assert result.tool_status == 'passed', result.facts
    assert result.facts['preview_http_attempts'] == 0
    assert result.facts['setup_http_attempts'] == 5
    assert result.facts['owned_process_closure'] is True
    rows = result.facts['documents']
    assert {row['scenario'] for row in rows} == {'L18','L19','L20','L21'}
    assert len(rows) == 10
    for row in rows:
        assert row['source_unchanged'] and row['resources_released'] and row['foreign_office_unchanged']
        assert row['located'] and row['message']
        assert row['status'] == 'passed', row
    assert result.product_status == 'passed'

@pytest.mark.acceptance_desktop
def test_actual_ninety_second_timeout_and_ui_cancel_close_exact_owned_processes(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['preview.document-stop']), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert result.tool_status == 'passed', result.facts
    assert result.facts['preview_http_attempts'] == 0 and result.facts['setup_http_attempts'] == 5
    assert result.critical_contracts['K3'] == result.critical_contracts['K5'] == 'passed'
    rows = result.facts['documents']
    assert [row['scenario'] for row in rows] == ['L22','L23']
    for row in rows:
        assert row['source_unchanged'] and row['resources_released'] and row['foreign_office_unchanged']
        assert row['exact_owned_identity_exited'] and row['status'] == 'passed'
        assert row['owned_identity']['pid'] and row['owned_identity']['creation_file_time']
    assert rows[0]['elapsed_ms'] >= 90000
    assert '超时' in rows[0]['message'] and '取消' in rows[1]['message']
    assert rows[1]['actual_cancel_flag_seen'] is True


def test_document_cases_are_available_from_the_normal_local_catalogue():
    registry=builtin_registry()
    for name in ['preview.document-content','preview.document-refusal','preview.document-stop']:
        case=registry.cases[name]
        assert case.owner_ticket=='19' and case.group=='document-preview'
        assert case.observation_layer=='electron-main-service-sdk'
        assert case.formal_live is False and case.protocols==('deepseek',)

@pytest.mark.parametrize('damage',['missing','changed'])
def test_prepared_preview_rejects_missing_or_changed_real_desktop_source(prepared_root,damage):
    from tests.support.agent_acceptance import SharedControlError,prepare_live,verify_prepared
    registry=builtin_registry()
    bundle=prepare_live(AcceptancePlan(cases=['preview.document-content'],mode='prepare-live'),root=prepared_root,registry=registry)
    name='desktop/src/renderer/DocumentPreview.tsx'
    assert name in bundle.files
    assert 'desktop/src/main/index.ts' in bundle.files
    assert 'desktop/tests/desktop-process.ts' in bundle.files
    path=prepared_root/name
    if damage=='missing':
        path.unlink()
    else:
        path.write_bytes(b'controlled changed source; never run')
    with pytest.raises(SharedControlError,match='binding'):
        verify_prepared(prepared_root,registry=registry)
