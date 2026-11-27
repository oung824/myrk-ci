"""Ordinary tasks through the maintained acceptance entry and real provider SDKs."""

import asyncio

import pytest

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan
from tests.support.agent_acceptance.catalogue import required_protocols


@pytest.mark.parametrize("protocol", required_protocols()["ordinary.question"])
def test_selected_question_records_real_sdk_exchange(acceptance_public_root, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
                                 root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == "passed", case.facts
    assert case.semantic_status == "not_measured"
    assert case.facts["answer"] == "The result is 42."
    assert case.facts["product_attempts"] == case.facts["mock_http_attempts"] == 1
    assert case.evidence["conversation"].is_file()
    assert case.evidence["exchange"].is_file()


@pytest.mark.parametrize("protocol", required_protocols()["ordinary.attachment"])
def test_attachment_is_read_by_tool_and_reaches_next_sdk_input(acceptance_public_root, protocol):
    import json

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.attachment"], protocols=[protocol]),
                                 root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.product_status == "passed", outcome.facts
    exchange = json.loads(outcome.evidence["exchange"].read_text(encoding="utf-8"))
    assert "Purchase threshold: 800." in json.dumps(exchange["sdk_inputs"][1])
    assert [item["actor"] for item in exchange["product_attempts"]] == ["main", "main"]
    assert [item["purpose"] for item in exchange["product_attempts"]] == ["work", "work"]
