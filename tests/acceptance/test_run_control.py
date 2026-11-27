"""The run entry distinguishes isolated product failures from broken controls."""

import asyncio
import json

import pytest

from tests.support.agent_acceptance import (AcceptanceCase, AcceptancePlan, CaseOutcome,
    CaseRegistry, SharedControlError, builtin_registry, run_plan)


def test_isolated_case_failure_keeps_original_and_continues(tmp_path):
    registry = builtin_registry()

    async def failed(_context):
        raise ValueError("first original symptom")

    registry.register(AcceptanceCase("controlled.failure", "control", ("deepseek",), failed))
    result = asyncio.run(run_plan(AcceptancePlan(cases=["controlled.failure", "ordinary.question"]), root=tmp_path / "run", registry=registry))
    assert [item.product_status for item in result.cases] == ["failed", "passed"]
    assert [item.case_status for item in result.cases] == ["failed", "pending_review"]
    assert result.cases[0].execution_id == f"{result.run_id}:controlled.failure:deepseek:1"
    assert "first original symptom" in result.cases[0].evidence["failure"].read_text(encoding="utf-8")


def test_shared_control_failure_stops_and_records_unexecuted_cases(tmp_path):
    registry = builtin_registry()

    async def broken(_context):
        raise SharedControlError("evidence association invalid")

    registry.register(AcceptanceCase("controlled.broken", "control", ("deepseek",), broken))
    root = tmp_path / "run"
    with pytest.raises(SharedControlError, match="association"):
        asyncio.run(run_plan(AcceptancePlan(cases=["controlled.broken", "ordinary.question"]), root=root, registry=registry))
    saved = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert saved["cases"][1]["product_status"] == "not_executed"
    assert saved["cases"][0]["case_status"] == "failed"
    assert saved["cases"][0]["execution_id"] == f"{saved['run_id']}:controlled.broken:deepseek:1"
    assert saved["cases"][1]["case_status"] == "not_executed"


@pytest.mark.parametrize("plan", [AcceptancePlan(cases=["missing.case"]),
                                  AcceptancePlan(cases=["ordinary.input-budget"], protocols=["google"])])
def test_required_case_or_protocol_cannot_be_silently_skipped(tmp_path, plan):
    with pytest.raises(ValueError, match="Required"):
        asyncio.run(run_plan(plan, root=tmp_path / "run", registry=builtin_registry()))
    assert not (tmp_path / "run").exists()
