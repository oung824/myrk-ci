"""Actual local effects through the maintained acceptance entry."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.support.agent_acceptance import AcceptancePlan, run_plan
from tests.support.agent_acceptance.core import CaseRegistry
from tests.support.agent_acceptance.actions import register


@pytest.fixture
def run_root(tmp_path):
    return Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(tmp_path))) / "reports" / "actions" / tmp_path.name


def test_file_workflow_preserves_exact_bytes_and_versions(run_root):
    registry = CaseRegistry()
    register(registry)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["actions.files"]), root=run_root, registry=registry))
    case = result.cases[0]
    if case.facts.get("known_defect") is not None:
        assert case.product_status == "failed" and case.tool_status == "passed"
        assert case.facts["historical_defect_status"] == "unresolved"
        assert all(path.is_file() for path in case.evidence.values())
        return
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["final_bytes"] == "efbbbf757064617465640d0a7365636f6e640d0a756e746f75636865640d0a"
    assert case.facts["statuses"] == ["completed", "completed", "failed", "completed", "completed", "completed", "completed", "completed"]
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    assert "independent-search-evidence-7319" in json.dumps(exchange["sdk_inputs"])
    assert [item["actor"] for item in exchange["product_attempts"]] == ["main"] * 9


@pytest.mark.parametrize("name", ["calculate", "calculate-invalid", "read-failure", "read-denied", "version-conflict",
                                  "command", "command-failure", "check", "check-failure", "check-timeout", "check-stop"])
def test_local_effect_branches_keep_real_results(run_root, name):
    registry = CaseRegistry()
    register(registry)
    result = asyncio.run(run_plan(AcceptancePlan(cases=[f"actions.{name}"]), root=run_root, registry=registry))
    case = result.cases[0]
    if case.facts.get("known_defect") is not None:
        assert case.product_status == "failed" and case.tool_status == "passed"
        assert case.facts["historical_defect_status"] == "unresolved"
        assert all(path.is_file() for path in case.evidence.values())
        return
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.evidence["conversation"].is_file()
    assert case.evidence["exchange"].is_file()
    assert all(path.is_file() for path in case.evidence.values()), {
        name: str(path) for name, path in case.evidence.items() if not path.is_file()
    }
    if name.startswith(("command", "check")):
        action = case.facts["actions"][0]
        assert action["cleanup_complete"] is True
        assert action["exit_code"] is not None
        assert "actual-stdout" in action["stdout"]
        assert "actual-stderr" in action["stderr"]
    if name == "check-stop":
        assert case.facts["mock_http_attempts"] == case.facts["after_stop_attempts"] == 1
    if name == "calculate":
        assert "0.25" in case.facts["calculation"] and "[amounts.csv:4]" in case.facts["calculation"]


@pytest.mark.parametrize("name", ["allow", "deny", "receiver-failure", "unread", "stop"])
def test_controlled_send_uses_only_exact_approved_bytes(run_root, name):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"send.{name}"]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["received"] == (["65786163742d617070726f7665642d73656e642d626f6479"] if name in {"allow", "receiver-failure"} else [])
    assert case.evidence["receiver"].is_file()
    assert case.semantic_status == "not_measured"


def test_record_write_failure_is_reported_without_inventing_an_action(run_root):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["actions.record-write-failure"]), root=run_root, registry=registry)).cases[0]
    assert case.tool_status == "passed"
    assert case.product_status == "failed", case.facts
    assert case.facts["turn_status"] == "failed"
    assert case.facts["actions"] == []
    assert case.facts["save_failures"]
    assert case.facts["planned_negative"] is True
    assert case.facts["expectation_met"] is True
    assert case.facts["record_write_error_type"] == "PermissionError"
    assert case.facts["product_attempts"] == case.facts["mock_http_attempts"] == 0
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    assert exchange["product_attempts"] == exchange["exchanges"] == []
    assert case.evidence["failed_turn"].is_file()


def test_missing_receipt_after_completed_check_cannot_pass(run_root):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["actions.check-missing-receipt"]), root=run_root, registry=registry)).cases[0]
    assert case.tool_status == "passed"
    assert case.product_status == "failed", case.facts
    assert case.facts["planned_negative"] is True and case.facts["expectation_met"] is True
    assert case.facts["actions"][0]["status"] == "completed" and case.facts["actions"][0]["exit_code"] == 0
    assert any(item["field"] == "receipt" and item["required"] for item in case.facts["missing_action_evidence"])
    assert all(path.is_file() for path in case.evidence.values())
