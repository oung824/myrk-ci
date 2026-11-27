"""K3 assessment reuses bound original recovery facts without starting Main."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from tests.support.agent_acceptance.core import CaseContext, CaseOutcome
from tests.support.agent_acceptance.save_import_recovery import finish

MATERIAL = Path(__file__).parent / "materials/save-import-recovery/k3-original-facts.json"


def original(execution_id: str) -> tuple[dict, bytes, dict]:
    raw = MATERIAL.read_bytes()
    case = next(item for item in json.loads(raw)["cases"] if item["execution_id"] == execution_id)
    assert case["critical_contracts"]["K3"] == "not_applicable"
    return case, raw, case["original_source"]


def assess(case: dict, root: Path) -> CaseOutcome:
    root.mkdir(parents=True)
    outcome = CaseOutcome(case["product_status"], deepcopy(case["facts"]), tool_status=case["tool_status"], execution_id=case["execution_id"],
        critical_contracts=deepcopy(case["critical_contracts"]))
    finish(CaseContext(root, case["protocol"], "k3-classification-only", case["case_id"]), outcome)
    return outcome


def test_bound_r17_recovery_is_applicable_and_keeps_original_publication(acceptance_public_root: Path) -> None:
    case, raw, row = original("r17:deepseek:1")
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == "passed"
    assert outcome.product_status == case["product_status"] == "passed"
    assert case["critical_contracts"]["K3"] == "not_applicable"
    assert MATERIAL.read_bytes() == raw
    assert row["critical_contracts"]["K3"] == "not_applicable"


@pytest.mark.parametrize("execution_id", ["r16:deepseek:1", "r18:deepseek:1"])
def test_completed_and_family_recovery_have_their_own_k3_evidence(acceptance_public_root: Path, execution_id: str) -> None:
    case, _, _ = original(execution_id)
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == "passed"


def test_absent_original_records_are_unknown_even_if_old_scorer_defaulted_false(acceptance_public_root: Path) -> None:
    case, _, _ = original("r17:deepseek:1")
    case["facts"]["recovery_contract"] = {"identity_scope_approval_preserved": False, "usage_retained": False,
        "duplicate_mutating_actions": [], "unknown_verified_before_resume": False, "imported_unknown_blocks_resume": False,
        "open_http_attempts": None, "original_task_id": None, "resumed_task_id": None, "requests_before": None, "requests_after": None}
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == "unknown"


@pytest.mark.parametrize("execution_id", [item["execution_id"] for item in json.loads(MATERIAL.read_bytes())["cases"]])
def test_all_sixteen_original_publications_have_a_separate_k3_assessment(acceptance_public_root: Path, execution_id: str) -> None:
    case, _, _ = original(execution_id)
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == "passed"
    assert outcome.product_status == case["product_status"]
    assert outcome.facts["recovery_k3_assessment"]["criteria_version"] == "save-import-recovery-k3-v2"
    assert outcome.semantic_status == "not_measured" and outcome.user_acceptance == "pending"


@pytest.mark.parametrize("execution_id,field,value,expected", [
    ("r16:deepseek:1", "no_completed_effect_replayed", False, "failed"),
    ("r16:deepseek:1", "independent_check_receipt_verified", None, "unknown"),
    ("r17:deepseek:1", "unknown_verified_before_resume", False, "failed"),
    ("r17:deepseek:1", "imported_unknown_blocks_resume", False, "failed"),
    ("r17:deepseek:1", "blocked_resume_http_attempts", 1, "failed"),
    ("r17:deepseek:1", "identity_scope_approval_preserved", False, "failed"),
    ("r17:deepseek:1", "usage_retained", False, "failed"),
    ("r17:deepseek:1", "duplicate_mutating_actions", ["controlled-replayed-edit"], "failed"),
    ("r17:deepseek:1", "resumed_task_id", "controlled-different-identity", "failed"),
    ("r17:deepseek:1", "requests_after", 0, "failed"),
    ("r17:deepseek:1", "open_http_attempts", 1, "failed"),
    ("r17:deepseek:1", "unknown_verified_before_resume", None, "unknown"),
    ("r17:deepseek:1", "reliable_and_pending_preserved", None, "unknown"),
    ("r17:deepseek:1", "identity_scope_approval_preserved", None, "unknown"),
    ("r17:deepseek:1", "requests_before", None, "unknown"),
    ("r17:deepseek:1", "original_task_id", None, "unknown"),
    ("r17:deepseek:1", "duplicate_mutating_actions", None, "unknown"),
    ("r18:deepseek:1", "child_scope_and_account_preserved", False, "failed"),
    ("r18:deepseek:1", "child_import_cannot_promote_to_main", False, "failed"),
    ("r18:deepseek:1", "applied_supplement_preserved_once", False, "failed"),
    ("r18:deepseek:1", "unfinished_parent_work_retained", False, "failed"),
    ("r18:deepseek:1", "total_requests_after", 0, "failed"),
    ("r18:deepseek:1", "child_requests", 0, "failed"),
    ("r18:deepseek:1", "child_record_unchanged", None, "unknown"),
    ("r18:deepseek:1", "child_id", None, "unknown"),
    ("r18:deepseek:1", "total_requests_before", None, "unknown"),
])
def test_known_violations_fail_and_missing_facts_stay_unknown(acceptance_public_root: Path, execution_id: str, field: str, value, expected: str) -> None:
    case, _, _ = original(execution_id)
    case["facts"]["recovery_contract"][field] = value
    case["facts"]["controlled_classification_counterexample"] = field
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == expected
    assert outcome.product_status == case["product_status"]


def test_unqualified_observation_cannot_establish_recovery_pass(acceptance_public_root: Path) -> None:
    case, _, _ = original("r17:deepseek:1")
    case["tool_status"] = "failed"
    outcome = assess(case, acceptance_public_root)
    assert outcome.critical_contracts["K3"] == "unknown"


def test_missing_tool_qualification_keeps_k3_unknown(acceptance_public_root: Path) -> None:
    case, _, _ = original("r17:deepseek:1")
    case["facts"].pop("tool_contract_passed")
    assert assess(case, acceptance_public_root).critical_contracts["K3"] == "unknown"
