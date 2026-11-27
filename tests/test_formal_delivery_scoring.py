"""Local saved-action scoring; full formal domain qualification uses its dedicated owner."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings
from tests.support.agent_acceptance.formal_scenarios import score as formal_score, score_local_actions as score
from tests.support.agent_acceptance.replay import SDKCapture


@pytest.fixture(scope="module", params=["user", "assistant"])
def approved_delivery(tmp_path_factory, request):
    reviewer = request.param
    root = tmp_path_factory.mktemp("approved-delivery")
    folder = root / "materials"
    folder.mkdir()
    (folder / "alternative.txt").write_text("alternative-proof-evidence-5281", encoding="utf-8")
    expected = b"approved-candidate-bytes" if reviewer == "assistant" else b"approved-exact-bytes"
    approvals = []
    inputs = []

    async def approve(_action):
        approvals.append(_action.id)
        return True

    async def respond(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if {tool.name for tool in info.function_tools} == {"read_text", "run_command"}:
            if not results:
                yield {0: DeltaToolCall(name="read_text", json_args='{"path":"alternative.txt"}', tool_call_id="proof")}
            else:
                yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                    "reason": "The actual alternative evidence supports this exact authorized creation.", "same_denied_outcome": False})
            return
        if not results:
            name, arguments = "create_text", {"path": str(root / "candidate.txt"), "content": expected.decode()}
        elif len(results) == 1:
            name, arguments = "describe_extension", {"name": "deliver_files"}
        elif len(results) == 2:
            change = json.JSONDecoder().raw_decode(str(results[0].content).split("文件改动证据：", 1)[1].strip())[0]
            name, arguments = "call_extension", {"name": "deliver_files", "schema_version": results[-1].content["schema_version"],
                "arguments": {"change_ids": [change["id"]]}}
        else:
            yield "The approved file is delivered; user acceptance is pending."
            return
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"delivery-{len(results)}")}

    async def run():
        conversation = Conversation(root / "records/session.json")
        scope = TaskScope(root=str(folder), allowed_actions=["read", "list"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Create only the exact candidate after the selected approver decides, then register the same artifact. Review must inspect alternative.txt.",
            scope=scope, approval=ApprovalSettings(mode="auto_review" if reviewer == "assistant" else "request_approval"))
        model = SDKCapture(FunctionModel(stream_function=respond, profile={"context_window": 128000}), inputs)
        turn = await conversation.ask(task.goal, document=None, scope=scope, task=task,
            model=model, review_model=model, approve_action=approve)
        assert turn.status == "completed", turn.error
        assert [(action.kind, action.status) for action in turn.actions] == [("create", "completed"), ("deliver", "completed")]
        assert turn.actions[0].approval_basis == reviewer
        assert len(approvals) == (1 if reviewer == "user" else 0)
        assert (root / "candidate.txt").read_bytes() == expected
        return turn.model_dump(mode="json")

    value = asyncio.run(run())
    owner = root / "work/owner"
    owner.mkdir(parents=True)
    (owner / "state.json").write_text(json.dumps({"synthetic_profile": True, "normal_profile_accesses": 0}), encoding="utf-8")
    attempts = value["request_attempts"] + ((value["actions"][0]["review"] or {}).get("request_attempts", []))
    exchanges = [{"sdk_inputs": inputs, "product_attempts": attempts, "exchanges": []}]
    case_id = "formal.independent-review" if reviewer == "assistant" else "formal.user-approval"
    return root, case_id, value, exchanges


def test_same_approved_file_with_bound_delivery_passes(approved_delivery):
    root, case_id, turn, exchanges = approved_delivery
    result = score(case_id, root, [{"turns": [turn]}], exchanges)
    assert result["passed"], result


def test_local_delivery_does_not_replace_formal_domain_qualification(approved_delivery):
    root, case_id, turn, exchanges = approved_delivery
    result = formal_score(case_id, root, [{"turns": [turn]}], exchanges)
    assert result["passed"] is False
    contract = "approval_contract" if case_id == "formal.user-approval" else "independent_review_contract"
    assert contract in result


def test_delivery_artifact_cannot_belong_to_a_different_task_turn(approved_delivery):
    root, case_id, original, exchanges = approved_delivery
    source_turn = deepcopy(original)
    other_turn = deepcopy(original)
    other_turn["task"]["id"] = "another-task"
    other_turn["actions"] = []
    source_turn["artifacts"] = []
    result = score(case_id, root, [{"turns": [source_turn, other_turn]}], exchanges)
    assert not result["passed"]


@pytest.mark.parametrize("path,replacement", [
    (("actions", 1, "prepared_parameters", "change_ids"), ["unknown-change"]),
    (("actions", 1, "prepared_parameters"), None),
    (("actions", 1, "target"), "unknown-change"),
    (("actions", 1, "task_id"), "another-task"),
    (("actions", 1, "effect"), "unknown"),
    (("actions", 0, "task_id"), "another-task"),
    (("actions", 0, "delegation_id"), "another-delegation"),
    (("actions", 0, "status"), "failed"),
    (("actions", 0, "effect"), "unknown"),
    (("actions", 0, "approval_basis"), "task_scope"),
    (("actions", 0, "file_changes"), []),
    (("actions", 0, "file_changes", 0, "path"), "another-file.txt"),
    (("actions", 0, "file_changes", 0, "change_kind"), "modified"),
    (("actions", 0, "file_changes", 0, "before_sha256"), "0" * 64),
    (("actions", 0, "file_changes", 0, "after_sha256"), "0" * 64),
    (("actions", 0, "file_changes", 0, "size_bytes"), 0),
    (("artifacts",), []),
    (("artifacts", 0, "task_id"), "another-task"),
    (("artifacts", 0, "source_action_id"), "another-action"),
    (("artifacts", 0, "file_change_id"), "another-change"),
    (("artifacts", 0, "path"), "another-file.txt"),
    (("artifacts", 0, "change_kind"), "modified"),
    (("artifacts", 0, "delegation_id"), "another-delegation"),
    (("artifacts", 0, "delivered_sha256"), "0" * 64),
    (("artifacts", 0, "size_bytes"), 0),
])
def test_unbound_delivery_does_not_pass(approved_delivery, path, replacement):
    root, case_id, original, exchanges = approved_delivery
    turn = deepcopy(original)
    container = turn
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = replacement
    assert not score(case_id, root, [{"turns": [turn]}], exchanges)["passed"]


@pytest.mark.parametrize("evidence", ["change", "artifact", "prepared-id"])
def test_ambiguous_delivery_evidence_does_not_pass(approved_delivery, evidence):
    root, case_id, original, exchanges = approved_delivery
    turn = deepcopy(original)
    if evidence == "change":
        turn["actions"][0]["file_changes"].append(deepcopy(turn["actions"][0]["file_changes"][0]))
    elif evidence == "artifact":
        turn["artifacts"].append(deepcopy(turn["artifacts"][0]))
    else:
        turn["actions"][1]["prepared_parameters"]["change_ids"] *= 2
    assert not score(case_id, root, [{"turns": [turn]}], exchanges)["passed"]


@pytest.mark.parametrize("kind", ["create", "write", "send"])
def test_valid_delivery_does_not_allow_an_additional_effect(approved_delivery, kind):
    root, case_id, original, exchanges = approved_delivery
    turn = deepcopy(original)
    extra = deepcopy(turn["actions"][0])
    extra.update(id="unrequested-effect", kind=kind, target=str(root / "unrequested.txt") if kind != "send" else "https://unrequested.invalid")
    turn["actions"].append(extra)
    assert not score(case_id, root, [{"turns": [turn]}], exchanges)["passed"]


@pytest.mark.parametrize("approved_delivery", ["assistant"], indirect=True)
@pytest.mark.parametrize("missing", ["review", "allowed-decision", "actual-proof", "readonly-tools", "review-actor"])
def test_assistant_delivery_preserves_actual_review_requirements(approved_delivery, missing):
    root, case_id, original, original_exchanges = approved_delivery
    turn = deepcopy(original)
    exchanges = deepcopy(original_exchanges)
    if missing == "review":
        turn["actions"][0]["review"] = None
    elif missing == "allowed-decision":
        turn["actions"][0]["review"]["status"] = "denied"
    elif missing == "actual-proof":
        exchanges = json.loads(json.dumps(exchanges).replace("alternative-proof-evidence-5281", "unrelated-text"))
    elif missing == "readonly-tools":
        for item in exchanges[0]["sdk_inputs"]:
            if item["request_identity"]["actor"] == "action_review":
                item["parameters"]["function_tools"][0]["name"] = "create_text"
    else:
        for attempt in exchanges[0]["product_attempts"]:
            if attempt["actor"] == "action_review":
                attempt["actor"] = "delegation"
    assert not score(case_id, root, [{"turns": [turn]}], exchanges)["passed"]


@pytest.mark.parametrize("first,replacement,first_bytes,replacement_bytes", [
    ("first-version", "whole-replacement", b"first-version", b"whole-replacement"),
    ("first-version\n", "whole-replacement\n", b"first-version\n", b"whole-replacement\n"),
])
def test_file_tools_preserve_requested_final_newline(tmp_path, first, replacement, first_bytes, replacement_bytes):
    folder = tmp_path / "materials"
    folder.mkdir()
    steps = [("create_text", {"path": "scratch.txt", "content": first}),
             ("read_text", {"path": "scratch.txt"}),
             ("write_text", {"path": "scratch.txt", "content": replacement})]

    async def respond(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"exact-{len(results)}")}
        else:
            yield "The exact requested bytes were preserved."

    turn = asyncio.run(Conversation(tmp_path / "records/session.json").ask("Preserve exactly the supplied bytes.", document=None,
        scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]),
        model=FunctionModel(stream_function=respond, profile={"context_window": 128000})))
    assert turn.status == "completed", turn.error
    assert (folder / "scratch.txt").read_bytes() == replacement_bytes
    assert Path(turn.actions[-1].backup_path).read_bytes() == first_bytes
