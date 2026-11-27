"""Compaction acceptance through real conversations and captured SDK requests."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry, run_plan
from tests.support.agent_acceptance.compaction import register
from tests.support.agent_acceptance.compaction import classify_known_failure
from tests.support.agent_acceptance.compaction_review import CRITERIA, HumanReview, apply_reviews


def execute(case_id, protocol="deepseek", *, include_original_materials=True):
    registry = CaseRegistry()
    register(registry, include_original_materials=include_original_materials)
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / "compaction" / uuid4().hex
    return asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=root, registry=registry)).cases[0]


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_short_summary_survives_reopen_and_separates_sdk_input_from_coverage(protocol):
    outcome = execute("compaction.short", protocol, include_original_materials=False)
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["originals_unchanged"]
    assert outcome.facts["attempt_purposes"] == ["compaction", "work", "work"]
    report = json.loads(outcome.evidence["review"].read_text(encoding="utf-8"))
    assert [row["kind"] for row in report["samples"]] == ["summary", "final", "final"]
    assert report["samples"][0]["source_mapping"]["covered"]
    assert report["samples"][0]["actual_input"]["sdk_before_provider_adaptation"]
    assert all(row["semantic_status"] == "not_measured" for row in report["samples"])


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
@pytest.mark.parametrize("case_id", ["long", "truncated", "service-failure", "interrupted", "missing-usage", "window-recovery"])
def test_summary_boundaries_preserve_originals_and_request_ownership(protocol, case_id):
    outcome = execute(f"compaction.{case_id}", protocol, include_original_materials=False)
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    assert outcome.facts["originals_unchanged"]
    assert outcome.semantic_status == "not_measured"
    if case_id in {"truncated", "service-failure", "interrupted"}:
        assert set(outcome.facts["attempt_purposes"]) == {"compaction"}
        assert outcome.facts["turns_used"] == 0
    if case_id == "interrupted":
        exchange = json.loads(outcome.evidence["exchange"].read_text(encoding="utf-8"))
        assert all("Partially received summary." in item["received_utf8"] for item in exchange["exchanges"])
    if case_id == "missing-usage":
        assert outcome.facts["input_tokens"] is None
        assert outcome.facts["cache_read_tokens"] is None
        assert outcome.facts["cache_write_tokens"] is None


SEMANTIC_CASES = ("report-material-pending", "explicit-user-verification", "user-pasted-material",
                  "assistant-proposal-not-adopted", "explicit-adoption", "completed-cancelled-corrected", "previous-summary-error")


@pytest.mark.parametrize("case_id", SEMANTIC_CASES)
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_semantic_material_runs_summary_and_post_reopen_work_without_grading_leak(case_id, protocol):
    outcome = execute(f"compaction.semantic.{case_id}", protocol)
    assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["material_read_after_compaction"]
    assert outcome.facts["criteria_absent_from_model_input"]
    review = json.loads(outcome.evidence["review"].read_text(encoding="utf-8"))
    assert sum(sample["kind"] == "summary" for sample in review["samples"]) == 1
    assert sum(sample["kind"] == "final" for sample in review["samples"]) == 2
    assert all(len(sample["criteria"]) == 6 for sample in review["samples"])
    assert outcome.evidence["criteria"].is_file()
    if case_id == "explicit-adoption":
        assert outcome.facts["comparison_operands_seen"]
        assert outcome.evidence["reference"].read_text(encoding="utf-8") == "Reference status: unresolved.\n"


def test_first_tool_result_below_hard_budget_continues_without_a_summary():
    outcome = execute("compaction.early-no-history", include_original_materials=False)
    assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
    assert outcome.facts["attempt_purposes"] == ["work", "work"]
    assert outcome.facts["mock_http_attempts"] == 2
    assert outcome.facts["compaction_statuses"] == outcome.facts["summary_attempts"] == []
    assert outcome.facts["soft_exceeded_but_hard_fits"]
    assert outcome.facts["layout_version"] == 1
    assert not outcome.facts["active_summary"]
    assert not outcome.facts["capacity_layout_changes"]
    assert outcome.facts["material_preserved"]
    assert outcome.facts["material_seen_after_read"]
    assert outcome.semantic_status == "not_measured"


def test_window_reopen_preserves_the_used_recovery_opportunity():
    outcome = execute("compaction.window-reopen", include_original_materials=False)
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    assert outcome.facts["logical_step_identity_across_reopen"] == "preserved"
    assert outcome.facts["capacity_recovery_used"] is True
    assert outcome.facts["previous_turn_unchanged"] is True
    assert outcome.facts["previous_task_requests"] == 3
    assert outcome.facts["current_task_requests"] == 4
    before = json.loads(outcome.evidence["before_reopen"].read_text(encoding="utf-8"))
    after = json.loads(outcome.evidence["conversation"].read_text(encoding="utf-8"))
    assert after["turns"][-2] == before["turns"][-1]
    assert outcome.facts["turns_before_reopen"] == outcome.facts["turns_used"] == 1
    assert len(outcome.facts["recovery_decisions"]) == 1
    assert outcome.evidence["conversation"].is_file()
    assert outcome.evidence["before_reopen"].is_file()
    assert outcome.evidence["review"].is_file()


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_second_compaction_preserves_inherited_sources_and_its_actual_input(protocol):
    outcome = execute("compaction.repeated", protocol, include_original_materials=False)
    assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
    assert outcome.facts["inherited_coverage"]
    report = json.loads(outcome.evidence["review"].read_text(encoding="utf-8"))
    summaries = [sample for sample in report["samples"] if sample["kind"] == "summary"]
    assert len(summaries) == 2
    assert summaries[1]["visible_previous_summaries"]
    assert summaries[1]["source_mapping"]["compaction"]["replaced_id"]


@pytest.mark.parametrize("symptom", ["window-reopen"])
def test_unrelated_error_and_disappeared_symptom_require_review(symptom):
    from copy import deepcopy

    observed = execute(f"compaction.{symptom}", include_original_materials=False)
    for changes in ({"error": "A new, unrelated failure."}, {"turn_status": "completed", "error": None}, {"mock_http_attempts": 0}):
        changed = deepcopy(observed)
        changed.facts.update(changes)
        classify_known_failure(changed, symptom)
        assert changed.tool_status == "failed"
        assert changed.product_status == "failed"
        assert not changed.facts["known_failure_matched"]


def test_named_six_point_review_is_bound_to_actual_output_but_fixture_stays_unmeasured():
    outcome = execute("compaction.short", include_original_materials=False)
    path = outcome.evidence["review"]
    sample = json.loads(path.read_text(encoding="utf-8"))["samples"][0]
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"], wire_sha256=sample["wire_sha256"],
        response_sha256=sample["response_sha256"], response_evidence_sha256=sample["response_evidence_sha256"],
        reviewer="Offline contract reviewer", criteria={name: "passed" for name in CRITERIA}, notes={})
    result = apply_reviews(path, [review])
    assert result["semantic_status"] == result["samples"][0]["semantic_status"] == "not_measured"
    for updates in ({"reviewer": " "}, {"response_sha256": "0" * 64}, {"criteria": {"invented": "passed"}}):
        with pytest.raises(ValueError):
            apply_reviews(path, [review.model_copy(update=updates)])
    with pytest.raises(ValueError, match="duplicate"):
        apply_reviews(path, [review, review])
    report = json.loads(path.read_text(encoding="utf-8"))
    report["samples"][0]["response_utf8"] += "Changed after review."
    tampered = path.with_name("tampered-review.json")
    tampered.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="original|binding|fingerprint"):
        apply_reviews(tampered, [review])
