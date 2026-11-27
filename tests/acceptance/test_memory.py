"""Memory acceptance uses synthetic facts and the public conversation entry."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry, Protocol, run_plan
from tests.support.agent_acceptance.memory import register
from tests.support.agent_acceptance.core import CaseOutcome


def execute(case_id: str, protocol: Protocol = "deepseek") -> CaseOutcome:
    registry = CaseRegistry()
    register(registry)
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / "memory" / uuid4().hex
    return asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=root, registry=registry)).cases[0]


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_personal_fact_is_saved_recalled_corrected_and_removed_without_erasing_history(protocol: Protocol) -> None:
    case = execute("memory.personal", protocol)
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["shared_state"] == "same synthetic profile, separate conversations"
    assert case.facts["original_history_unchanged"]
    assert case.facts["current_facts"] == []
    assert case.facts["old_chat_retains_original_background"]
    assert case.facts["new_chat_omits_removed_fact"]
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    assert "旧聊天、摘要、备份、导出和其他待审提议保留" in json.dumps(exchange, ensure_ascii=False)
    assert case.evidence["history_copy"].read_bytes() == case.evidence["seed_conversation"].read_bytes()


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_project_fact_can_be_corrected_and_removed_inside_its_selected_project(protocol: Protocol) -> None:
    case = execute("memory.project-lifecycle", protocol)
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.facts["fact_scope"] == "project"
    assert case.facts["current_facts"] == []
    assert case.facts["original_history_unchanged"]
    assert case.facts["new_chat_omits_removed_fact"]
    assert case.semantic_status == "not_measured"
    exchange = json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))
    inherited_guide = json.dumps(str(project_root(Path(__file__).resolve()) / "AGENTS.md"))[1:-1]
    assert inherited_guide not in json.dumps(exchange["sdk_inputs"])


@pytest.mark.parametrize("name", ["allow", "deny", "stop"])
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_project_memory_is_isolated_until_the_exact_source_and_recipient_are_confirmed(name: str, protocol: Protocol) -> None:
    case = execute(f"memory.project-{name}", protocol)
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["initial_project_isolation"]
    assert case.facts["shared_state"] == "two projects and separate chats in one synthetic profile"
    assert len(case.facts["approvals"]) == 1
    assert case.facts["source_seen_after_confirmation"] is (name == "allow")
    if name == "stop":
        assert case.facts["after_stop_attempts"] == case.facts["mock_http_attempts"] == 5


@pytest.mark.parametrize("name", ["batch-failure", "write-denied", "write-stop", "save-failure"])
def test_unsaved_notes_remain_absent_and_failed_batches_do_not_partially_apply(name: str) -> None:
    case = execute(f"memory.{name}")
    assert case.tool_status == "passed", case.facts
    assert case.product_status == "passed", case.facts
    assert case.expectation_status == "met", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["current_texts"] == (["Synthetic valid seed."] if name == "batch-failure" else [])
    assert case.facts["unsaved_claim_absent"]
    if name == "write-stop":
        assert case.facts["mock_http_attempts"] == case.facts["after_stop_attempts"] == 1
    if name == "save-failure":
        action = case.facts["actions"][0]
        assert action["status"] == "failed" and action["denial_source"] is None
        assert action["approval_basis"] == "task_scope" and action["effect"] == "unknown"
        assert action["error"].startswith("事实笔记保存失败：")
        assert case.facts["approvals"] == []
        assert case.facts["facts_unchanged"]
        assert case.evidence["facts"].read_bytes() == case.evidence["facts_before"].read_bytes()


@pytest.mark.parametrize("name", ["add", "defer-allow", "defer-deny", "no-budget", "stop"])
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_one_post_delivery_review_keeps_original_delivery_and_remaining_budget(name: str, protocol: Protocol) -> None:
    case = execute(f"memory.review-{name}", protocol)
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["delivery_answer"] == "Synthetic main delivery."
    assert case.facts["memory_reviews"] == 1
    if name in {"stop", "no-budget"}:
        assert case.facts["repeat_review_refused"] is None
        assert case.facts["review_tools_are_memory_only"] is None
        assert case.facts["review_input_count"] == 0
    else:
        assert case.facts["repeat_review_refused"]
        assert case.facts["review_tools_are_memory_only"]
        assert case.facts["review_input_count"] == 2
        report = json.loads(case.evidence["semantic_review"].read_text(encoding="utf-8"))
        assert {sample["kind"] for sample in report["samples"]} == {"main_answer", "memory_change", "memory_review"}
        assert all(sample["semantic_status"] == "not_measured" for sample in report["samples"])
        assert case.facts["criteria_absent_from_model_input"]
    if name.startswith("defer-"):
        assert case.facts["stale_approval_rejected"]
        assert case.facts["double_approval_rejected"]
    if name == "stop":
        assert case.facts["after_stop_attempts"] == case.facts["mock_http_attempts"] == 2
