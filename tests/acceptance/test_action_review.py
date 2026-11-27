"""Approval outcomes and independently owned requests through the entry."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.support.agent_acceptance import AcceptancePlan, run_plan
from tests.support.agent_acceptance.core import CaseRegistry
from tests.support.agent_acceptance.review_cases import register


@pytest.fixture
def run_root(tmp_path):
    return Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(tmp_path))) / "reports" / "action-review" / tmp_path.name


@pytest.mark.parametrize("name", ["request-allow", "request-deny", "auto-allow", "auto-deny", "full-access", "running-settings"])
def test_approval_modes_keep_scope_and_captured_reviewer(run_root, name):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"approval.{name}"]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["outside_read"] == "outside-readable-evidence-9041"


@pytest.mark.parametrize("name", ["evidence-recovery", "readonly", "service-retry", "service-failure", "stream-retry", "timeout", "stop"])
def test_independent_review_preserves_actual_decision_and_failures(run_root, name):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"review.{name}"]), root=run_root, registry=registry)).cases[0]
    if case.facts.get("known_defect") is not None:
        assert name == "service-retry" and case.protocol == "deepseek"
        assert case.product_status == "failed" and case.tool_status == "passed" and case.semantic_status == "not_measured"
        assert case.facts["historical_defect_status"] == "unresolved"
        assert case.facts["turn_status"] == "limited" and case.facts["answer"] == ""
        assert case.facts["review_status"] == "allowed" and case.facts["review_requests"] == 2
        assert [(item["kind"], item["status"], item["effect"]) for item in case.facts["actions"]] == [("create", "cancelled", "unknown")]
        assert not (case.evidence["conversation"].parent / "candidate.txt").exists()
        assert case.evidence["proof"].read_bytes() == b"alternative-proof-evidence-5281"
        assert all(path.is_file() for path in case.evidence.values())
        exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
        assert [(item["number"], item["actor"], item["status"], item["http_status"]) for item in exchange["product_attempts"]] == [
            (1, "main", "completed", None), (2, "action_review", "failed", 503), (3, "action_review", "completed", None)]
        return
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    assert sum(item["actor"] == "action_review" for item in exchange["product_attempts"]) == case.facts["review_requests"]
    assert all(item["purpose"] == "work" for item in exchange["product_attempts"] if item["actor"] == "action_review")
    if name == "evidence-recovery":
        assert case.facts["review_status"] == "allowed"
        assert case.facts["review_requests"] == 3
        assert "未取得本次证据" in json.dumps(exchange["sdk_inputs"], ensure_ascii=False)
        assert "alternative-proof-evidence-5281" in json.dumps(exchange["sdk_inputs"], ensure_ascii=False)
    if name in {"timeout", "service-failure"}:
        assert case.facts["review_status"] != "denied"
    if name == "stop":
        assert case.facts["after_stop_attempts"] == case.facts["mock_http_attempts"] == 2


@pytest.mark.parametrize("protocol", ["openai-chat", "google"])
def test_review_reselects_and_reads_alternative_evidence_across_protocols(run_root, protocol):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["review.evidence-recovery"], protocols=[protocol]),
                               root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["review_requests"] == 3
    assert case.semantic_status == "not_measured"
