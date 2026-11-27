"""Task control and recovery through the unified public acceptance entry."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, run_plan


def run_case(case_id, protocol="deepseek"):
    from tests.support.agent_acceptance.resume_queue import register

    registry = CaseRegistry()
    register(registry)
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/resume-queue" / uuid4().hex
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=root, registry=registry)).cases[0]
    assert all(path.is_file() for path in outcome.evidence.values()), outcome.evidence
    return outcome


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_supplement_is_adopted_once_and_queued_task_starts_after_original_finishes(protocol):
    outcome = run_case("queue.supplement", protocol)
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["applied_events"] == 1
    assert outcome.facts["queue_after_finish"] is True
    assert outcome.facts["supplement_input_counts"] == [0, 1, 1]
    assert outcome.facts["real_http_attempts"] == 0
    assert all(path.is_file() for path in outcome.evidence.values())


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_parallel_conversations_keep_separate_state_and_each_stop_targets_only_its_task(protocol):
    outcome = run_case("parallel.stop", protocol)
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["second_running_after_first_stop"] is True
    assert outcome.facts["separate_tasks"] is True
    assert outcome.facts["statuses"] == ["cancelled", "cancelled"]
    assert outcome.facts["http_attempts_after_stop"] == 2


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_original_task_requires_real_impact_verification_and_never_repeats_the_edit(protocol):
    outcome = run_case("recovery.original", protocol)
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["planned_local_exit"] == 71
    assert outcome.facts["open_http_attempts"] == outcome.facts["blocked_resume_http_attempts"] == 0
    assert outcome.facts["verified_effect"] == "completed"
    assert outcome.facts["resumed_task_id"] == outcome.facts["original_task_id"]
    assert outcome.facts["requests_before"] == 2
    assert outcome.facts["requests_after"] == 4
    assert outcome.facts["new_action_kinds"] == ["read"]
    captured = json.loads(outcome.evidence["interrupted.exchange"].read_bytes())
    pending = captured["pending_observations"]
    assert len(pending) == 1
    assert pending[0]["task_id"] == outcome.facts["original_task_id"]
    assert pending[0]["committed"] is False
    assert pending[0]["source_sha256"] == sha256(outcome.evidence["interrupted_pending"].read_bytes()).hexdigest()
    assert outcome.facts["pending_evidence_bindings"][pending[0]["source_path"]] == "interrupted-pending.json"
    reliable = json.loads(outcome.evidence["original"].read_bytes())
    assert [{key: value for key, value in item.items() if key != "task_id"} for item in captured["product_attempts"]] == reliable["turns"][-1]["request_attempts"]
    last_wire = sha256(captured["exchanges"][-1]["request_utf8"].encode()).hexdigest()
    assert any(((item.get("observation") or {}).get("wire") or {}).get("body_sha256") == last_wire for item in pending[0]["attempts"])


def test_default_provider_factory_still_uses_the_explicit_terminal_mock():
    from tests.support.agent_acceptance.core import CaseContext
    from tests.support.agent_acceptance.service_channel import ServiceChannel
    from tests.support.agent_acceptance.replay import Reply

    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/resume-default-factory" / uuid4().hex
    root.mkdir(parents=True)

    async def run_default():
        async with ServiceChannel(CaseContext(root, "deepseek", "default-factory", "default-factory"), "service",
                [Reply(text="The default factory retained the explicit terminal Mock.")]) as channel:
            await channel.send({"action": "configure", "key": "synthetic-default-factory-only"})
            await channel.receive("configured")
            await channel.send({"action": "send", "prompt": "Confirm the synthetic default factory boundary."})
            finished = await channel.receive("finished")
        assert finished["turn"]["status"] == "completed"
        assert finished["turn"]["answer"] == "The default factory retained the explicit terminal Mock."
        actual = json.loads((channel.root / "exchange.json").read_bytes())
        assert len(actual["exchanges"]) == len(actual["sdk_inputs"]) == len(actual["product_attempts"]) == 1
        assert actual["product_attempts"][0]["actor"] == "main"
        assert actual["product_attempts"][0]["purpose"] == "work"
        assert actual["sdk_inputs"][0]["sdk_input_id"] == actual["exchanges"][0]["sdk_input_id"]

    asyncio.run(run_default())


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_legacy_import_keeps_identity_permissions_used_requests_and_explicit_budget_increase(protocol):
    outcome = run_case("recovery.legacy", protocol)
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["source_unchanged"] is True
    assert outcome.facts["request_policy"] == "shared"
    assert outcome.facts["requests_before"] == 1
    assert outcome.facts["requests_after"] == 2
    assert outcome.facts["open_http_attempts"] == outcome.facts["exhausted_http_attempts"] == 0


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_delegated_supplement_and_parent_recovery_preserve_child_identity_and_usage(protocol):
    outcome = run_case("recovery.delegation", protocol)
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["applied_events"] == 1
    assert outcome.facts["child_record_unchanged"] is True
    assert outcome.facts["child_requests"] == 3
    assert outcome.facts["main_requests_after"] == 4
    assert outcome.facts["total_requests_after"] == 7
    assert outcome.facts["supplement_input_counts"] == [0, 0, 1, 1, 1]


@pytest.mark.acceptance_desktop
def test_complete_electron_exit_relaunch_and_original_task_continuation_keep_real_program_exit():
    outcome = run_case("recovery.desktop-restart")
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["normal_exit_phases"] == 2
    assert outcome.facts["open_http_attempts"] == 0
    assert outcome.facts["requests_before"] == 4
    assert outcome.facts["requests_after"] == 5
    assert outcome.facts["program_exit_code"] == 0
    assert outcome.facts["independent_receipt_verified"] is True
    assert outcome.facts["program_runs"] == "x"
    assert outcome.facts["processes_complete"] is True


def test_stopped_original_does_not_start_saved_queue_until_explicit_start_after_service_restart():
    outcome = run_case("queue.saved-stop")
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["open_http_attempts"] == 0
    assert outcome.facts["original_status"] == "cancelled"
    assert outcome.facts["queued_identity_preserved"] is True


def test_service_startup_failure_retains_actual_identity_exit_and_original_stderr():
    from tests.support.agent_acceptance.core import CaseContext
    from tests.support.agent_acceptance.service_channel import ServiceChannel

    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/resume-startup" / uuid4().hex
    root.mkdir(parents=True)

    async def fail_before_ready():
        channel = ServiceChannel(CaseContext(root, "deepseek", "local-plan-fault", "startup-failure"), "service", [])
        channel.script.write_bytes(b"not-json-planned-local-input")
        with pytest.raises(RuntimeError, match="closed before ready"):
            async with channel:
                pytest.fail("Malformed local plan cannot start the service.")
        observation = json.loads((channel.root / "process.json").read_text(encoding="utf-8"))
        assert observation["startup"]["ownership"] == "owned"
        assert observation["after"][0]["state"] == "exited"
        assert observation["returncode"] == 1
        assert "JSONDecodeError" in (channel.root / "stderr.txt").read_text(encoding="utf-8")

    asyncio.run(fail_before_ready())
