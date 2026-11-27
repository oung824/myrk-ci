"""Independent input, usage and interrupted-output criteria at the entry seam."""

import asyncio
import json

import pytest

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan
from tests.support.agent_acceptance.catalogue import required_combinations, required_protocols


@pytest.mark.parametrize("case_id,protocol", required_combinations("ordinary.retry", "ordinary.interrupted", "ordinary.missing-usage", "ordinary.input-budget"))
def test_ordinary_failure_and_input_contracts(acceptance_public_root, case_id, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.product_status == "passed", outcome.facts
    exchange = json.loads(outcome.evidence["exchange"].read_text(encoding="utf-8"))
    if case_id == "ordinary.retry":
        assert [item["http_status"] for item in exchange["product_attempts"]] == [503, None]
        assert len(exchange["exchanges"]) == 2
        assert exchange["exchanges"][0]["received_utf8"] == '{"error":{"code":503,"status":"UNAVAILABLE","message":"synthetic service failure"}}'
    elif case_id == "ordinary.interrupted":
        assert outcome.facts["answer"] == "Partial answer."
        assert exchange["product_attempts"][0]["status"] == "failed"
        assert "Partial answer." in exchange["exchanges"][0]["received_utf8"]
    elif case_id == "ordinary.missing-usage":
        observation = exchange["product_attempts"][0]["observation"]
        assert observation["input_tokens"] is None
        assert observation["cache_read_tokens"] is None
        assert observation["usage_status"] == "unknown"
    else:
        assert exchange["exchanges"] == []
        assert outcome.facts["prepared"][0]["accepted"] is False


@pytest.mark.parametrize("protocol", required_protocols()["ordinary.input-layout"])
def test_current_input_keeps_tool_contract_prefix_and_measures_entire_request(acceptance_public_root, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.input-layout"], protocols=[protocol]), root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.product_status == "passed", outcome.facts
    exchange = json.loads(outcome.evidence["exchange"].read_text(encoding="utf-8"))
    requests = [json.loads(item["request_utf8"]) for item in exchange["exchanges"]]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert len(requests[1]["messages"]) > len(requests[0]["messages"])
    assert all(item["accepted"] and item["input_tokens"] > 0 and item["input_budget_tokens"] > item["input_tokens"]
               for item in outcome.facts["prepared"])
