"""Cross-capability recovery through complete, normally exited desktop processes."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, run_plan


@pytest.mark.acceptance_desktop
def test_repeated_actual_compaction_survives_complete_electron_restart_without_repeating_actions():
    from tests.support.agent_acceptance.integration import register

    registry = CaseRegistry()
    register(registry)
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/integration" / uuid4().hex
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["integration.compaction-restart"]),
                                  root=root, registry=registry)).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["completed_compactions"] >= 2
    assert outcome.facts["inherited_coverage"] is True
    assert outcome.facts["original_messages_preserved"] is True
    assert outcome.facts["open_http_attempts"] == 0
    assert outcome.facts["same_task_scope_approval"] is True
    assert outcome.facts["normal_exit_phases"] == 2
    assert outcome.facts["processes_complete"] is True
    assert outcome.facts["requests_after"] > outcome.facts["requests_before"]
    assert outcome.facts["program_runs"] == "x"
    assert outcome.facts["independent_receipt_verified"] is True
    assert outcome.facts["real_http_attempts"] == outcome.facts["normal_profile_accesses"] == 0
    assert outcome.facts["product_attempts"] == outcome.facts["mock_http_attempts"]
    assert all(path.is_file() for path in outcome.evidence.values())
    review = json.loads(outcome.evidence["compaction_review"].read_text(encoding="utf-8"))
    summaries = [sample for sample in review["samples"] if sample["kind"] == "summary"]
    assert len(summaries) == outcome.facts["completed_compactions"]
    assert all(len(sample["criteria"]) == 6 and sample["semantic_status"] == "not_measured" for sample in summaries)
    record = json.loads(outcome.evidence["conversation"].read_text(encoding="utf-8"))
    assert [item["kind"] for item in record["turns"][-1]["actions"]] == []
