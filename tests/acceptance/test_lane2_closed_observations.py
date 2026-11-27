"""Closed lane2 evidence keeps original outcomes while checking current native projections."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from tests.acceptance.test_formal_known_memory_observations import closed_outcome
from tests.acceptance.test_formal_scenarios import assert_formal_qualification
from tests.support.agent_acceptance.memory_isolation import PROJECT, CHANGED_PROJECT

ORIGINALS = Path(__file__).parent / "materials/formal-observations-lane2-4ef"


@pytest.mark.parametrize("name", ["cross-deepseek", "cross-google"])
def test_native_closed_cross_project_observation_keeps_content_approval_unknown(tmp_path, name):
    outcome, _ = closed_outcome(tmp_path, name, ORIGINALS)
    before = asdict(outcome)
    assert_formal_qualification(outcome)
    assert asdict(outcome) == before
    assert outcome.product_status == "inconclusive"
    assert outcome.critical_contracts["K1"] == outcome.critical_contracts["K2"] == "unknown"


@pytest.mark.parametrize("damage", ["body", "tool-id", "model"])
def test_native_google_known_observation_rejects_other_wire_content_identity_or_model(tmp_path, damage):
    outcome, paths = closed_outcome(tmp_path, "cross-google", ORIGINALS)
    path = paths["work/receiver/exchange.json"]
    exchange = json.loads(path.read_bytes())
    wire = exchange["exchanges"][-1]
    body = json.loads(wire["request_utf8"])
    response = next(part["functionResponse"] for content in body["contents"] for part in content["parts"]
        if part.get("functionResponse", {}).get("name") == "recall_project_memory")
    if damage == "body":
        response["response"]["return_value"] = "Other source content: 9999."
    elif damage == "tool-id":
        response["id"] += "-other-call"
    else:
        driver = json.loads(paths["work/receiver/state.json"].read_bytes())
        wire["url"] = wire["url"].replace(driver["recipient_binding"][1], "other-model")
    wire["request_utf8"] = json.dumps(body)
    path.write_text(json.dumps(exchange), encoding="utf-8")
    with pytest.raises((AssertionError, KeyError)):
        assert_formal_qualification(outcome)


@pytest.mark.parametrize("name", ["save-cross", "save-changed"])
def test_actual_memory_save_failure_is_not_a_completed_recall_qualification(tmp_path, name):
    outcome, paths = closed_outcome(tmp_path, name, ORIGINALS)
    original = asdict(outcome)
    assert outcome.tool_status == outcome.product_status == "failed"
    assert outcome.critical_contracts["K1"] == outcome.critical_contracts["K2"] == "unknown"
    contract = outcome.facts["memory_contract"]
    assert contract["cross_project_effect"] == "cancelled" and contract["source_seen_after_approval"] is False
    recall = next(action for action in outcome.facts["actions"] if action["kind"] == "memory_recall")
    assert recall["status"] == "cancelled" and recall["effect"] == "not_started" and recall["output"] == ""
    diagnostics = json.loads(paths["work/receiver/save-diagnostics.json"].read_bytes())
    assert diagnostics["errors"] and all(error["exception_type"] == "PermissionError" and error["winerror"] == 5
        and error["operation"] == "replace" and error["filename"].endswith(".json.tmp") for error in diagnostics["errors"])
    assert any("进展未能保存" in error and "limited" in error for error in outcome.facts["driver_errors"])
    driver = json.loads(paths["work/receiver/state.json"].read_bytes())
    exchange = json.loads(paths["work/receiver/exchange.json"].read_bytes())
    start = driver["project_b_window"]["sdk_start"]
    assert driver["normal_exit"] is True and driver["synthetic_profile"] is True
    for text in (PROJECT, CHANGED_PROJECT):
        assert text not in json.dumps(exchange["sdk_inputs"][start:], ensure_ascii=False)
        assert all(text not in wire["request_utf8"] for wire in exchange["exchanges"][start:])
    assert len(exchange["exchanges"]) == outcome.facts["budget"]["actual_http"]
    with pytest.raises(AssertionError):
        assert_formal_qualification(outcome)
    assert asdict(outcome) == original


def test_complete_old_openai_delivery_adds_only_two_actual_phase_counters(tmp_path):
    from tests.support.agent_acceptance.deliverables import score

    outcome, paths = closed_outcome(tmp_path, "deliverables-openai", ORIGINALS)
    before = asdict(outcome)
    assert "normal_profile_accesses" not in outcome.facts
    for phase in ("work", "reopen"):
        target = tmp_path / "counter-projection" / phase / "owner/state.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(paths[phase + "/owner/state.json"].read_bytes())
    projection = score(outcome.case_id, tmp_path / "counter-projection", [], [])
    assert projection["passed"] is False and projection["product_execution"] == "incomplete"
    assert projection["normal_profile_accesses"] == outcome.facts["real_http_attempts"] == 0
    outcome.facts["normal_profile_accesses"] = projection["normal_profile_accesses"]
    assert_formal_qualification(outcome)
    after = asdict(outcome)
    after["facts"].pop("normal_profile_accesses")
    assert after == before and outcome.product_status == "passed"
