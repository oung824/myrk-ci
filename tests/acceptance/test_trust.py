"""Project source and local trust survive real service import and restart."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import AcceptancePlan, CaseOutcome, CaseRegistry, Protocol, run_plan
from tests.support.agent_acceptance.trust import register


def execute(case_id: str, protocol: Protocol = "deepseek") -> CaseOutcome:
    registry = CaseRegistry()
    register(registry)
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / "trust" / uuid4().hex
    return asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=root, registry=registry)).cases[0]


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_project_rules_keep_sources_and_do_not_change_explicit_settings(trusted: bool, protocol: Protocol) -> None:
    case = execute("trust.guidance-trusted" if trusted else "trust.guidance-untrusted", protocol)
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["project_trusted"] is trusted
    assert case.facts["correct_sources"]
    assert case.facts["provider_unchanged"]
    assert case.facts["approval_mode"] == "request_approval"
    assert case.facts["settings_loaded"] is trusted


def test_import_does_not_replace_machine_trust_and_opening_and_restarting_send_nothing() -> None:
    case = execute("trust.import-local-choice")
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["imported_source_unchanged"]
    assert case.facts["local_choice_applied"] == [False, True]
    assert case.facts["trust_survived_restart"]
    assert case.facts["revoked"]
    assert case.facts["no_request_before_resume"]
    assert case.facts["restart_mock_http_attempts"] == 0
    assert case.facts["service_exit_codes"] == [0, 0]
    assert json.loads(case.evidence["exchange"].read_text(encoding="utf-8"))["semantic_status"] == "not_measured"
    restart = json.loads(case.evidence["restart_exchange"].read_text(encoding="utf-8"))
    assert restart["product_attempts"] == restart["exchanges"] == []


@pytest.mark.parametrize("allowed", [False, True])
def test_new_recipient_requires_history_confirmation_even_with_full_access(allowed: bool) -> None:
    case = execute("trust.recipient-allow" if allowed else "trust.recipient-deny", "openai-chat")
    assert case.tool_status == case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["history_sent_after_confirmation"] is allowed
    assert case.facts["approval_reviewer"] == "user"
    assert case.facts["target_http_attempts"] == int(allowed)
    source = json.loads(case.evidence["source_exchange"].read_text(encoding="utf-8"))
    inherited_guide = json.dumps(str(project_root(Path(__file__).resolve()) / "AGENTS.md"))[1:-1]
    assert inherited_guide not in json.dumps(source["sdk_inputs"])
