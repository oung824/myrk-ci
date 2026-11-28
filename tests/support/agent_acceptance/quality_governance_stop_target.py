"""Bind the synthetic governance stop to the saved denied operation."""

from pathlib import Path
from typing import Any

from myrk.conversation import ConversationRecord
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import SharedControlError


def matches_governance_stop_target(root: Path, candidate: Path, sdk: dict[str, Any]) -> bool:
    attempt = current_request_attempt()
    if attempt is None or attempt.actor != "main" or attempt.purpose != "work" or not attempt.step_id:
        return False

    messages = sdk["messages"]
    if not messages or messages[-1]["kind"] != "request":
        return False
    returned_calls = [part["tool_call_id"] for part in messages[-1]["parts"]
                      if part["part_kind"] == "tool-return" and part["tool_name"] == "create_text"]
    if not returned_calls:
        return False

    identity = (attempt.number, attempt.step_id, attempt.actor, attempt.purpose, attempt.delegation_id)
    owners = [
        (turn.task.id, turn.actions)
        for path in (root / "records").glob("*.json")
        for turn in ConversationRecord.model_validate_json(path.read_bytes()).turns
        if turn.task is not None and turn.status == "running" and turn.request_owner_id is None
        for saved in turn.request_attempts
        if (saved.number, saved.step_id, saved.actor, saved.purpose, saved.delegation_id) == identity
    ]
    if not owners:
        return False
    if len(owners) != 1:
        raise SharedControlError("Governance stop target evidence is ambiguous: multiple saved request owners.")

    task_id, actions = owners[0]
    target = str(candidate)
    denials = [action for action in actions
               if action.task_id == task_id and action.delegation_id is None and action.kind == "create"
               and action.target == target and action.prepared_parameters.get("path") == target
               and action.status == "denied" and action.denial_source == "user" and action.effect == "not_started"]
    denied_calls = {action.tool_call_id for action in denials if action.tool_call_id}
    matches = [call for call in returned_calls if call in denied_calls]
    if not matches:
        return False
    if len(denials) != 1 or len(matches) != 1:
        raise SharedControlError("Governance stop target evidence is ambiguous: multiple denied actions or current tool returns.")
    return True
