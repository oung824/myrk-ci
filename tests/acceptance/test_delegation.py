"""Delegation evidence through the maintained entry and real conversations."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.support.agent_acceptance import AcceptancePlan, run_plan
from tests.support.agent_acceptance.core import CaseRegistry
from tests.support.agent_acceptance.delegation import register


@pytest.fixture
def run_root(tmp_path):
    return Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(tmp_path))) / "reports" / "delegation" / tmp_path.name


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_child_delivers_real_file_parent_reads_back_and_checks_final_bytes(run_root, protocol):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["delegation.delivery"], protocols=[protocol]),
                               root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["artifact"] == "def total(values):\n    return sum(values)\n"
    assert case.facts["verification_registration"] == "passed"
    assert case.facts["actual_parent_readback"] is True
    assert case.facts["final_check"] is True
    assert case.facts["main_turns"] == 5
    assert case.facts["child_turns"] == [3]
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    attempts = exchange["product_attempts"]
    assert [item["number"] for item in attempts] == list(range(1, 9))
    assert [item["actor"] for item in attempts] == ["main", "main", "delegation", "delegation", "delegation", "main", "main", "main"]
    assert all(item["purpose"] == "work" for item in attempts)
    parts = [part for request in exchange["sdk_inputs"] for message in request["messages"] for part in message["parts"]]
    report = next(part["content"] for part in parts if part["part_kind"] == "tool-return" and part["tool_name"] == "delegate_work")
    verify = next(part["args"] for part in parts if part["part_kind"] == "tool-call" and part["tool_name"] == "verify_delegation")
    if isinstance(report, str):
        report = json.loads(report)
    if isinstance(verify, str):
        verify = json.loads(verify)
    assert verify["delegation_id"] == report["id"]
    assert case.evidence["child.0.conversation"].is_file()
    assert all(path.is_file() for path in case.evidence.values())


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_skill_body_and_attachment_are_read_only_after_selection_without_scope_expansion(run_root, protocol):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["skills.selected"], protocols=[protocol]),
                               root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["statuses"] == ["completed", "completed", "denied", "completed", "denied"]
    assert case.facts["file_bytes"] == "6b656570"
    assert case.facts["body_only_after_selection"] is True
    assert case.facts["unused_skill_body_absent"] is True
    assert case.semantic_status == "not_measured"


@pytest.mark.parametrize("name", ["allow", "deny", "failure", "stop", "stale-schema"])
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_extension_discovery_and_call_keep_exact_schema_and_approval_boundary(run_root, protocol, name):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"extensions.{name}"], protocols=[protocol]),
                               root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["fixed_tool_table"] is True
    assert case.facts["outside_unchanged"] is True
    if name == "stale-schema":
        assert case.facts["actions"] == []
    else:
        action = case.facts["actions"][0]
        assert action["tool_name"] == "calculate_table" and action["tool_source"] == "extension"
        assert action["tool_schema_version"] == case.facts["described_schema_version"]
        assert action["status"] == {"allow": "completed", "deny": "denied", "failure": "failed", "stop": "cancelled"}[name]
    if name == "allow":
        assert "0.25" in case.facts["result"] and "[../outside.csv:4]" in case.facts["result"]
    if name == "stop":
        assert case.facts["after_stop_attempts"] == case.facts["mock_http_attempts"] == 3
    assert all(path.is_file() for path in case.evidence.values())


@pytest.mark.parametrize("name", ["service-retry", "service-failure", "interrupted", "turn-limit", "review-retry"])
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_child_service_and_budget_facts_keep_actual_request_owner(run_root, protocol, name):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"delegation.{name}"], protocols=[protocol]),
                               root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["main_status"] == "completed"
    assert case.facts["child_status"] == {"service-retry": "completed", "service-failure": "failed",
                                            "interrupted": "failed", "turn-limit": "limited", "review-retry": "completed"}[name]
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    attempts = exchange["product_attempts"]
    assert [item["number"] for item in attempts] == list(range(1, len(attempts) + 1))
    children = [item for item in attempts if item["actor"] != "main"]
    assert children and all(item["delegation_id"] == case.facts["delegation_id"] for item in children)
    assert all(item["purpose"] == "work" for item in attempts)
    assert case.facts["main_limit"] is None
    if name == "service-retry":
        assert case.facts["child_turns"] == 1 and case.facts["child_requests"] == 2
        assert children[0]["status"] == "failed" and children[0]["http_status"] == 503
    if name == "interrupted":
        assert case.facts["child_answer"] == "Partial child report."
        assert "Partial child report." in exchange["exchanges"][1]["received_utf8"]
        assert exchange["exchanges"][1]["error"] == "synthetic interruption after delivered output"
    if name == "review-retry":
        assert [item["actor"] for item in attempts] == ["main", "delegation", "action_review", "action_review", "delegation", "main"]
        assert case.facts["child_requests"] == 4 and case.facts["child_turns"] == 3
        assert case.facts["main_requests"] == case.facts["main_turns"] == 2
    if name == "turn-limit":
        assert case.facts["child_limit"] == case.facts["child_turns"] == 1
        assert case.facts["main_requests"] == 3
    assert all(path.is_file() for path in case.evidence.values())


@pytest.mark.parametrize("name", ["scope-folder-denied", "scope-actions-denied", "scope-check-denied", "scope-receiver-denied", "child-action-denied"])
def test_delegation_cannot_widen_parent_authorization_or_its_narrower_child_scope(run_root, name):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=[f"delegation.{name}"]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["unchanged"] is True
    assert case.facts["statuses"][-1] == "denied"
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    if name.startswith("scope-"):
        assert case.facts["delegations"] == []
        assert len(exchange["exchanges"]) == 2
        assert all(item["actor"] == "main" for item in exchange["product_attempts"])
    else:
        assert case.facts["delegations"][0]["scope"]["allowed_actions"] == ["read"]
        assert case.facts["actions"][-1]["effect"] == "not_started"


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_stopping_parent_waits_for_real_child_program_cleanup_and_sends_nothing_more(run_root, protocol):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["delegation.stop"], protocols=[protocol]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["turn_status"] == case.facts["child_status"] == "cancelled"
    assert case.facts["mock_http_attempts"] == case.facts["after_stop_attempts"] == 2
    assert case.facts["partial_file"] == "real-child-partial-output"
    action = next(action for action in case.facts["actions"] if action["kind"] == "check")
    assert action["cleanup_complete"] is True and action["exit_code"] is not None
    assert action["status"] == "cancelled"
    assert all(path.is_file() for path in case.evidence.values())


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_ordinary_readback_and_final_check_do_not_invent_special_verification_registration(run_root, protocol):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["delegation.pending-registration"], protocols=[protocol]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["actual_parent_readback"] is True and case.facts["final_check"] is True
    assert case.facts["verification_registration"] == "pending"
    assert "专用核验尚未登记" in case.facts["main_answer"]
    assert "未核对或未通过" not in case.facts["main_answer"]
    assert all(action["kind"] != "verify" for action in case.facts["actions"])
    assert all(review["status"] == "not_measured" for review in case.facts["answer_reviews"])


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_correct_artifact_cannot_hide_scripted_false_acceptance_claims(run_root, protocol):
    registry = CaseRegistry()
    register(registry)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["delegation.answer-divergence"], protocols=[protocol]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "failed", case.facts
    assert case.tool_status == "passed" and case.semantic_status == "not_measured"
    assert case.facts["expectation_met"] is True and case.facts["planned_negative"] is True
    assert case.facts["actual_parent_readback"] is True and case.facts["final_check"] is True
    assert case.facts["artifact_contract"] == "passed"
    assert case.facts["answer_fact_contract"] == "failed"
    assert case.facts["delivery_status"] == "awaiting_acceptance"
    assert all(review["status"] == "not_measured" for review in case.facts["answer_reviews"])


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_plan_can_be_selected_and_revised_without_executing_its_steps(run_root, protocol):
    registry = CaseRegistry()
    register(registry, include_delivery_materials=False)
    case = asyncio.run(run_plan(AcceptancePlan(cases=["plans.update"], protocols=[protocol]), root=run_root, registry=registry)).cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["plan_updates"] == [["Read the rule", "Propose a change"], ["Keep the read-only rule", "Wait for user acceptance"]]
    assert case.facts["plan"] == ["Keep the read-only rule", "Wait for user acceptance"]
    assert case.facts["actions"] == [] and case.facts["unchanged"] is True
    assert case.semantic_status == "not_measured"
