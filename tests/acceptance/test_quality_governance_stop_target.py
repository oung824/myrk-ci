import asyncio
import json
from pathlib import Path
import subprocess
from typing import Any

import httpx2
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel

from myrk.actions import Action
from myrk.conversation import ConversationRecord, Task, Turn
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt
from tests.support.agent_acceptance import quality_runtime
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.replay import SDKCapture


CASE_ID = "quality.qg3.formal.9"


async def replay_stop_barrier(root: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> int | None:
    goal = "Read the policy and observe the refusal of the proposed creation."
    candidate = str(root / "candidate.txt")
    visible = quality_runtime.QualityVisibleInput(
        task_id="QG3.formal.9", adapter="qg3-9", goal=goal, sources={}, parameters={"candidate": "candidate.txt"},
    )
    monkeypatch.setattr(quality_runtime, "bound_task", lambda *_: (None, visible, None))
    task = Task(id="stop-task", goal=goal, scope=None)
    turn = Turn(prompt=goal, answer="", task=task)
    records = root / "records"
    records.mkdir()

    initial = ModelRequest(parts=[UserPromptPart(goal)])
    read_call = ModelResponse(parts=[ToolCallPart("read_text", {"path": "policy.txt"}, tool_call_id="read-policy")])
    read_return = ModelRequest(parts=[ToolReturnPart("read_text", "The policy requires consent.", tool_call_id="read-policy")])
    policy_history: list[ModelMessage] = [initial, read_call, read_return]
    denied_calls = ["create-candidate", "create-again"] if scenario == "ambiguous" else ["create-candidate"]
    proposal = {"path": candidate, "content": "Proposed text only."}
    proposals = ModelResponse(parts=[ToolCallPart("create_text", proposal, tool_call_id=call_id) for call_id in denied_calls])
    refusals = ModelRequest(parts=[ToolReturnPart("create_text", "User denied this proposal.", tool_call_id=call_id)
                                   for call_id in denied_calls])
    denied_actions = [Action(task_id=task.id, kind="create", target=candidate, status="denied", denial_source="user",
                             effect="not_started", approval_basis="denied", tool_name="create_text", tool_call_id=call_id,
                             tool_arguments=proposal, prepared_parameters=proposal) for call_id in denied_calls]
    denial_history: list[ModelMessage] = [*policy_history, proposals, refusals]
    auxiliary_call = ModelResponse(parts=[ToolCallPart("read_text", {"path": "notice.txt"}, tool_call_id="read-notice")])
    auxiliary_return = ModelRequest(parts=[ToolReturnPart("read_text", "Unrelated notice.", tool_call_id="read-notice")])

    schedule: list[list[ModelMessage]] = [[initial], policy_history]
    if scenario == "one-auxiliary":
        schedule.append([*policy_history, auxiliary_call, auxiliary_return])
    if scenario == "missing":
        other_proposal = {**proposal, "path": str(root / "other.txt")}
        other_call = ModelResponse(parts=[ToolCallPart("create_text", other_proposal, tool_call_id="create-other")])
        other_return = ModelRequest(parts=[ToolReturnPart("create_text", "Other creation finished.", tool_call_id="create-other")])
        schedule.append([*denial_history, other_call, other_return])
    else:
        schedule.append(denial_history)

    captures: list[dict[str, Any]] = []
    arrived = root / "governance-stop-arrived.json"
    release = root / "governance-stop-release.txt"

    async def terminal(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        response = httpx2.Response(200)
        assert await quality_runtime.terminal_response_barrier(
            CASE_ID, root, len(captures) - 1, captures[-1], response,
        ) is response
        return ModelResponse(parts=[TextPart("Fixture response.")])

    model = SDKCapture(FunctionModel(terminal), captures)
    for position, messages in enumerate(schedule, start=1):
        attempt = RequestAttempt(number=position, step_id=f"step-{position}", model=model.model_name, provider="fixture")
        turn.request_attempts.append(attempt)
        if position >= 3:
            turn.actions = denied_actions
        record = ConversationRecord(version=10, messages=messages, turns=[turn])
        (records / "conversation.json").write_text(record.model_dump_json(), encoding="utf-8")
        waiting = asyncio.create_task(observe_request(model, attempt).request(messages, None, ModelRequestParameters()))
        await asyncio.sleep(0)
        if arrived.is_file():
            assert not waiting.done(), "The indicated terminal response should still be held"
            marker = json.loads(arrived.read_bytes())
            release.write_text("Stop acknowledged by fixture.\n", encoding="utf-8")
            await asyncio.wait_for(waiting, timeout=1)
            assert marker["sdk_input_id"] == captures[-1]["sdk_input_id"]
            return position
        await asyncio.wait_for(waiting, timeout=1)
        attempt.status = "completed"
    return None


@pytest.mark.parametrize(("scenario", "expected_position"), [("original", 3), ("one-auxiliary", 4), ("missing", None)])
def test_governance_stop_holds_only_the_current_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, expected_position: int | None,
) -> None:
    assert asyncio.run(replay_stop_barrier(tmp_path, monkeypatch, scenario)) == expected_position


def test_governance_stop_rejects_ambiguous_denials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SharedControlError, match="(?i)ambiguous|unique"):
        asyncio.run(replay_stop_barrier(tmp_path, monkeypatch, "ambiguous"))
    assert not (tmp_path / "governance-stop-arrived.json").exists()


@pytest.mark.acceptance_desktop
def test_desktop_governance_stop_reports_missing_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = tmp_path / "desktop/node_modules/electron"
    electron.mkdir(parents=True)
    (electron / "index.js").write_text("module.exports = { dialog: {} }", encoding="utf-8")
    monkeypatch.setenv("MYRK_CONNECTION_PROJECT_ROOT", str(tmp_path))
    driver = Path(quality_runtime.__file__).with_name("desktop") / "quality-governance.mjs"
    script = """
import assert from 'node:assert/strict'
const [driver, root] = process.argv.slice(2)
const { runQualityGovernance } = await import(driver)
const commands = []
const state = {}
await assert.rejects(runQualityGovernance({
  authority: { scenario_root: root, grant: { transport: 'mock' } },
  request: { scope: { root }, governance_adapter: 'qg3-9' },
  command: async ({ action }) => commands.push(action),
  events: async () => [{ event: 'finished', saved: true, turn: { status: 'completed' } }],
  execute: async () => root,
  state,
}), { message: 'Missing governance stop target evidence' })
assert.deepEqual(commands, ['send'])
assert.equal(state.passed, undefined)
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-", driver.as_uri(), str(tmp_path)],
        input=script, capture_output=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
