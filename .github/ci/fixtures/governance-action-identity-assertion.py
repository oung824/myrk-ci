import json
from pathlib import Path

import pytest

from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_governance import score
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput


SCENARIO = "QG3.formal.9"
TASK_ID = "refusal-task"


@pytest.mark.parametrize("entry", ["desktop", "inprocess"])
@pytest.mark.parametrize(("observed_task_id", "expected_status"), [
    pytest.param(TASK_ID, "passed", id="matching"),
    pytest.param(None, "unknown", id="missing"),
    pytest.param("other-task", "unknown", id="conflicting"),
])
def test_refusal_score_requires_observed_action_task_identity(
    tmp_path: Path, entry: str, observed_task_id: str | None, expected_status: str,
):
    bundle = SOURCE / "tests/acceptance/materials/quality/integrated-inputs.json"
    material = json.loads(bundle.read_bytes())[SCENARIO]
    material.pop("gold")
    visible = QualityVisibleInput.model_validate({**material, "task_id": SCENARIO, "adapter": "qg3-9"})
    materials = tmp_path / "materials"
    materials.mkdir()
    policy = visible.sources["policy.txt"]
    (materials / "policy.txt").write_bytes(policy.text.encode(policy.encoding))

    target = str(tmp_path / visible.parameters["candidate"])
    proposal = {
        "id": "creation",
        "kind": "create",
        "task_id": TASK_ID,
        "target": target,
        "prepared_parameters": {"path": target, "content": "Proposed text only."},
        "status": "denied",
        "effect": "not_started",
        "denial_source": "user",
        "approval_id": "approval",
        "prepared_action_sha256": "subject",
        "approval_context_sha256": "context",
    }
    decision = {
        key: proposal[key] for key in (
            "task_id", "target", "prepared_parameters", "approval_id",
            "prepared_action_sha256", "approval_context_sha256",
        )
    }
    decision.update(action_id=proposal["id"], approved=False)
    task_event = {"path": str(tmp_path / "conversation.json"), "task_id": TASK_ID}
    observed_action = {key: proposal[key] for key in ("id", "status", "effect", "approval_id")}
    if observed_task_id is not None:
        observed_action["task_id"] = observed_task_id
    denial_event = {**task_event, "event": "action", "action": observed_action}
    stop_event = {
        **task_event,
        "event": "conversation_cancellation" if entry == "inprocess" else "stopping",
    }
    receiver = tmp_path / "work/receiver"
    receiver.mkdir(parents=True)
    state = {"approval_decisions": [decision], "stop_accepted": True, "task_id": TASK_ID}
    if entry == "inprocess":
        state.update(entry="public_conversation", action_events=[denial_event, stop_event], stop_event=stop_event)
    else:
        finished_event = {
            **task_event, "event": "finished", "saved": True,
            "turn": {"status": "cancelled", "task": {"id": TASK_ID}},
        }
        (receiver / "events.json").write_text(
            json.dumps([denial_event, stop_event, finished_event]), encoding="utf-8",
        )
        (receiver / "transport.json").write_text(
            json.dumps({"mock_http_attempts": 2, "real_http_attempts": 0}), encoding="utf-8",
        )
    (receiver / "state.json").write_text(json.dumps(state), encoding="utf-8")

    request_identities = [
        {"number": number, "actor": "main", "purpose": "work", "delegation_id": None}
        for number in (1, 2)
    ]
    attempts = [{**identity, "step_id": f"step-{identity['number']}"} for identity in request_identities]
    sdk_inputs = [
        {"sdk_input_id": f"sdk-{identity['number']}", "request_identity": identity}
        for identity in request_identities
    ]
    exchanges = [
        {**sdk_input, "attempt": index, "terminal_http_started": True, "product_step_id": f"step-{index}"}
        for index, sdk_input in enumerate(sdk_inputs, 1)
    ]
    stop_snapshot = [
        {"attempt": row["attempt"], "sdk_input_id": row["sdk_input_id"]} for row in exchanges
    ]
    record = {"turns": [{
        "task": {"id": TASK_ID},
        "project_trusted": False,
        "status": "cancelled",
        "actions": [proposal],
        "request_attempts": attempts,
    }]}
    envelope = {
        "task_id": TASK_ID,
        "sdk_inputs": sdk_inputs,
        "exchanges": exchanges,
        "stop_boundaries": [{"event": stop_event, "terminal_attempts": stop_snapshot}],
    }

    facts = score(visible, tmp_path, [record], [envelope])
    assert facts["denial_preserved"] is True
    assert facts["request_stop_status"] == "passed"
    assert facts["action_stop_status"] == expected_status
    assert facts["critical_contracts"]["K3"] == expected_status
    assert facts["passed"] is (expected_status == "passed")
