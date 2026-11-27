"""The canonical cargo recovery uses actual Main, child tools and full reopening."""

import pytest

from tests.acceptance.test_quality_recovery_main import _run_main_control


@pytest.mark.acceptance_desktop
def test_cargo_recovery_main_retains_supplement_child_and_original_parent(prepared_root, acceptance_public_root):
    outcome = _run_main_control("QG5.formal.8", prepared_root, acceptance_public_root)
    assert outcome["product_status"] == outcome["tool_status"] == "passed", outcome["facts"]
    facts = outcome["facts"]
    proof = facts["quality_recovery_contract"]
    assert proof["same_task_and_scope"] is True
    assert proof["applied_supplements"] == 1
    assert proof["supplement_id_and_applied_request_retained"] is True
    assert proof["child_scope_only_side"] is True
    assert proof["child_record_unchanged"] is True
    assert proof["child_cannot_resume_as_parent"] is True
    assert proof["child_requests"] == 2
    assert proof["completed_effects_not_replayed"] is True
    assert proof["open_http_attempts"] == 0
    assert proof["requests_after"] > proof["requests_before"]
    assert facts["failure_origin"] == "planned_local_not_sent_after_completed_child"
    assert facts["supplier_mock_fault"] is False
    assert facts["budget"]["not_sent"] >= 1
    assert facts["budget"]["actual_http"] <= 24
    assert facts["real_http_attempts"] == 0
    assert facts["processes_complete"] is True
    assert outcome["semantic_status"] == "not_measured"
    assert outcome["user_acceptance"] == "pending"
