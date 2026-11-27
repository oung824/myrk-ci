"""Approval routing contracts at the actual Conversation and model transport interfaces."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, Task, TaskScope
from myrk.executor import POWERSHELL
from myrk.permissions import AccessSettings, ApprovalSettings
from tests.support.agent_acceptance.replay import OfflineSession, Reply


ALLOW_REVIEW = Reply(text=json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
    "reason": "The user explicitly requested these exact small files and content.", "same_denied_outcome": False}))


def action_approval(text: str) -> dict:
    states = re.findall(r'<harness_data origin="myrk_action_approval">\s*(\{.*?\})\s*</harness_data>', text, re.S)
    assert states, "Actual model input must identify the effective action approver."
    return json.loads(states[-1])


def sdk_instructions(session: OfflineSession, index: int) -> str:
    return "\n".join(part["content"] for part in session.sdk_inputs[index]["parameters"]["instruction_parts"])


def test_actual_request_distinguishes_user_and_assistant_action_approver(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    instructions = {}

    async def run():
        for mode in ("request_approval", "auto_review"):
            session = OfflineSession("deepseek", [Reply(text="Offline input capture complete.")])
            scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
            task = Task(id="a" * 32, goal="Create the requested file after the selected action approver decides.",
                        scope=scope, approval=ApprovalSettings(mode=mode), provider_config=session.config)
            async with session.model() as model:
                turn = await Conversation(tmp_path / (mode + ".json")).ask(task.goal, document=None,
                    scope=scope, task=task, model=model, review_model=model)
            assert turn.status == "completed", turn.error
            assert len(session.exchanges) == len(session.sdk_inputs) == 1
            instructions[mode] = [part["content"] for part in session.sdk_inputs[0]["parameters"]["instruction_parts"]]

    asyncio.run(run())
    assert instructions["request_approval"] != instructions["auto_review"], (
        "Actual initial SDK instructions describe the user and independent-assistant action approver identically. "
        "The selected approval route is invisible at this interface. "
        "This diagnostic catches missing input guidance, not live-model semantic correctness."
    )


def test_direct_create_uses_actual_action_review_without_delegation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "candidate.txt"
    session = OfflineSession("deepseek", [
        Reply(tool="create_text", arguments={"path": str(target), "content": "approved-candidate-bytes"}),
        ALLOW_REVIEW,
        Reply(text="The exact file was created; user acceptance is pending."),
    ])
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal=f"Create only {target} with exact content approved-candidate-bytes after independent review.",
                scope=scope, approval=ApprovalSettings(mode="auto_review"), provider_config=session.config)

    async def run():
        async with session.model() as model:
            return await Conversation(tmp_path / "direct.json").ask(task.goal, document=None,
                scope=scope, task=task, model=model, review_model=model)

    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert target.read_bytes() == b"approved-candidate-bytes"
    assert task.review_requests == 1
    assert not turn.delegations
    assert turn.actions[0].review.status == "allowed"
    assert [row["request_identity"]["actor"] for row in session.sdk_inputs] == ["main", "action_review", "main"]


@pytest.mark.parametrize("mode,read_only,mandatory,expected", [
    ("request_approval", False, False, {"condition": "on_request", "reviewer": "user", "sandbox": "restricted", "read_only": False, "review_mandatory": False}),
    ("auto_review", False, False, {"condition": "on_request", "reviewer": "assistant", "sandbox": "restricted", "read_only": False, "review_mandatory": False}),
    ("full_access", False, False, {"condition": "never", "reviewer": "none", "sandbox": "full_access", "read_only": False, "review_mandatory": False}),
    ("full_access", True, False, {"condition": "never", "reviewer": "none", "sandbox": "full_access", "read_only": True, "review_mandatory": False}),
    ("auto_review", False, True, {"condition": "on_request", "reviewer": "assistant", "sandbox": "restricted", "read_only": False, "review_mandatory": True}),
])
def test_effective_approval_and_available_tools_reach_sdk_and_wire(tmp_path: Path, monkeypatch, mode, read_only, mandatory, expected):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    session = OfflineSession("deepseek", [Reply(text="No action requested.")])
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="Report current action approval guidance without executing anything.", scope=scope,
        approval=ApprovalSettings(mode=mode, access=AccessSettings(read_only=read_only), review_mandatory=mandatory),
        provider_config=session.config)

    async def run():
        async with session.model() as model:
            turn = await Conversation(tmp_path / "settings.json").ask(task.goal, document=None,
                scope=scope, task=task, model=model, review_model=model)
        assert turn.status == "completed", turn.error
        assert not turn.actions

    asyncio.run(run())
    sdk_text = sdk_instructions(session, 0)
    wire = json.loads(session.exchanges[0]["request_utf8"])
    wire_text = "\n".join(message.get("content") or "" for message in wire["messages"])
    assert action_approval(sdk_text) == action_approval(wire_text) == expected
    for text in (sdk_text, wire_text):
        assert "send_text(path,url)" in text
        assert "delegate_work" in text and "不能代替动作批准" in text
        assert POWERSHELL in text


def test_live_reviewer_update_preserves_pending_action_and_sent_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    session = OfflineSession("deepseek", [
        Reply(tool="create_text", arguments={"path": str(first), "content": "first-exact"}),
        Reply(tool="create_text", arguments={"path": str(second), "content": "second-exact"}),
        ALLOW_REVIEW,
        Reply(text="Both files were created; acceptance is pending."),
    ])
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal=f"Create only {first} and {second} with their specified exact content.", scope=scope,
                approval=ApprovalSettings(mode="request_approval"), provider_config=session.config)
    conversation = Conversation(tmp_path / "live.json")
    pending = []

    async def approve(action):
        pending.append(action.id)
        original = action.approval_settings.model_dump(mode="json")
        conversation.set_turn_reviewer(task.id, "assistant")
        assert action.approval_settings.model_dump(mode="json") == original
        return True

    async def run():
        async with session.model() as model:
            return await conversation.ask(task.goal, document=None, scope=scope, task=task,
                model=model, review_model=model, approve_action=approve)

    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert first.read_bytes() == b"first-exact" and second.read_bytes() == b"second-exact"
    assert [action.approval_settings.reviewer for action in turn.actions] == ["user", "assistant"]
    assert [action.approval_basis for action in turn.actions] == ["user", "assistant"]
    assert pending == [turn.actions[0].id] and task.review_requests == 1
    assert task.approval.reviewer == "user" and turn.approval_settings.reviewer == "assistant"
    main_indices = [index for index, item in enumerate(session.sdk_inputs) if item["request_identity"]["actor"] == "main"]
    sent = [json.loads(item["request_utf8"]) for item in session.exchanges if item["request_identity"]["actor"] == "main"]

    def instruction_texts(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from instruction_texts(item)
        elif isinstance(value, list):
            for item in value:
                yield from instruction_texts(item)

    for index, reviewer in zip(main_indices, ("user", "assistant", "assistant")):
        assert any(action_approval(text)["reviewer"] == reviewer
            for text in instruction_texts(session.sdk_inputs[index]) if '<harness_data origin="myrk_action_approval">' in text)
    for previous, current in zip(sent, sent[1:]):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]
        assert current["tools"] == previous["tools"]
    assert [action_approval("\n".join(message.get("content") or "" for message in item["messages"]))["reviewer"]
            for item in sent] == ["user", "assistant", "assistant"]
    restored = Conversation.open(conversation.path).turns[-1]
    assert [action.approval_settings.reviewer for action in restored.actions] == ["user", "assistant"]


@pytest.mark.parametrize("mode,read_only,status,approval_count", [
    ("request_approval", False, "denied", 1),
    ("full_access", False, "completed", 0),
    ("full_access", True, "denied", 1),
], ids=["user-declines", "full-access-create", "readonly-declines"])
def test_exposed_approval_guidance_preserves_actual_access_decisions(tmp_path: Path, monkeypatch, mode, read_only, status, approval_count):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "candidate.txt"
    session = OfflineSession("deepseek", [
        Reply(tool="create_text", arguments={"path": str(target), "content": "exact-candidate"}),
        Reply(text="The actual access decision is recorded; acceptance is pending."),
    ])
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal=f"Create only {target} with exact content exact-candidate.", scope=scope,
        approval=ApprovalSettings(mode=mode, access=AccessSettings(read_only=read_only)), provider_config=session.config)
    approvals = []

    async def decline(action):
        approvals.append(action.model_copy(deep=True))
        return False

    async def run():
        async with session.model() as model:
            return await Conversation(tmp_path / "access.json").ask(task.goal, document=None,
                scope=scope, task=task, model=model, review_model=model, approve_action=decline)

    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(approvals) == approval_count
    assert turn.actions[0].status == status
    assert target.exists() is (status == "completed")
    for prepared in approvals:
        assert prepared.status == "awaiting_approval"
        assert prepared.approval_settings.access.read_only is read_only
        assert prepared.prepared_parameters["path"] == str(target)
        assert prepared.prepared_parameters["content"] == "exact-candidate"
    if status == "denied":
        assert turn.actions[0].effect == "not_started"
    if target.exists():
        assert target.read_bytes() == b"exact-candidate"
    assert task.review_requests == 0
    assert action_approval(sdk_instructions(session, 0))["read_only"] is read_only


def test_reopening_old_saved_layout_receives_current_action_approval_protocol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "materials"
    folder.mkdir()
    path = tmp_path / "old-session.json"
    initial = OfflineSession("deepseek", [Reply(text="Initial synthetic task finished.")])
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="Continue one synthetic task across an instruction upgrade.", scope=scope,
                approval=ApprovalSettings(mode="auto_review"), provider_config=initial.config)
    legacy_base = "Saved base instructions from before action approval guidance."

    async def run():
        async with initial.model() as model:
            first = await Conversation(path).ask(task.goal, document=None, scope=scope, task=task,
                model=model, review_model=model)
        assert first.status == "completed", first.error
        saved = json.loads(path.read_text(encoding="utf-8"))
        current_base = saved["request_layouts"]["main"]["instructions"]
        protocol = next((part["content"] for part in initial.sdk_inputs[0]["parameters"]["instruction_parts"]
                         if part["name"] == "action-approval-protocol"), "")

        def old_base(value):
            if isinstance(value, dict):
                return {key: old_base(item) for key, item in value.items()}
            if isinstance(value, list):
                return [old_base(item) for item in value if not (
                    isinstance(item, dict) and item.get("name") == "action-approval-protocol" and "content" in item)]
            return value.replace(current_base, legacy_base).replace(protocol, "") if isinstance(value, str) else value

        historical = json.dumps(old_base(saved), ensure_ascii=False)
        assert "send_text(path,url)" not in historical
        path.write_text(historical, encoding="utf-8")
        reopened = Conversation.open(path)
        restored_task = reopened.turns[-1].task
        continued = OfflineSession("deepseek", [Reply(text="Continuation complete.")])
        async with continued.model() as model:
            turn = await reopened.ask("Continue the same saved task.", document=None,
                scope=restored_task.scope, task=restored_task, model=model, review_model=model)
        assert turn.status == "completed", turn.error
        text = sdk_instructions(continued, 0)
        assert legacy_base in text
        assert action_approval(text)["reviewer"] == "assistant"
        assert "send_text(path,url)" in text
        assert POWERSHELL in text
        wire = json.loads(continued.exchanges[0]["request_utf8"])
        wire_text = "\n".join(message.get("content") or "" for message in wire["messages"])
        assert "send_text(path,url)" in wire_text and POWERSHELL in wire_text
        assert json.loads(path.read_text(encoding="utf-8"))["request_layouts"]["main"]["instructions"] == legacy_base

    asyncio.run(run())
