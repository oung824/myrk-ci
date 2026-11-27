"""A source read advances only after its actual successful SDK tool return."""

from pathlib import Path
from typing import Any

import httpx2
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart

from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.formal_scenarios import WARMUPS, replies, summary_reply
from tests.support.agent_acceptance.replay import OfflineSession, Reply


class FormalAdoptionMock:
    def __init__(self, protocol: Protocol, *, config: ProviderConfig | None = None) -> None:
        self.terminal = OfflineSession(protocol, [], config=config)
        self.sources = [f"source-{number:02}.txt" for number in range(WARMUPS)]
        self.targets = [*self.sources, "receipt.txt", "reference.txt"]
        self.completed: set[str] = set()
        self.calls: dict[str, str] = {}
        self.returns: dict[str, str] = {}
        self.blocked = False
        self.finished = replies("formal.compaction-adoption", Path("unused"), protocol)[-1]

    def respond(self, request: httpx2.Request, sdk_input: dict[str, Any], *, purpose: str) -> httpx2.Response:
        if purpose == "compaction":
            reply = summary_reply("formal.compaction-adoption")
        elif purpose == "work":
            messages = ModelMessagesTypeAdapter.validate_python(sdk_input["messages"])
            for message in messages:
                for part in message.parts:
                    if isinstance(part, ToolCallPart) and part.tool_name == "read_text":
                        target = part.args_as_dict().get("path")
                        if target in self.targets:
                            previous = self.calls.setdefault(part.tool_call_id, target)
                            if previous != target:
                                raise SharedControlError("One adoption read call changed its actual target.")
            for message in messages:
                for part in message.parts:
                    if not isinstance(part, ToolReturnPart) or part.tool_name != "read_text" or part.tool_call_id not in self.calls:
                        continue
                    previous = self.returns.setdefault(part.tool_call_id, part.outcome)
                    if previous != part.outcome:
                        raise SharedControlError("One adoption read call changed its actual outcome.")
                    target = self.calls[part.tool_call_id]
                    if part.outcome == "success":
                        self.completed.add(target)
                    elif part.outcome != "retried" or target not in self.sources:
                        self.blocked = True
            pending = next((target for target in self.targets if target not in self.completed), None)
            reply = (Reply(text="The controlled source review is incomplete; the actual unsuccessful read remains recorded.")
                if self.blocked else Reply(tool="read_text", arguments={"path": pending}) if pending is not None else self.finished)
        else:
            raise SharedControlError("An unexpected request purpose entered the adoption-only Mock.")
        self.terminal.replies.append(reply)
        return self.terminal.respond(request)


def adoption_mock(case_id: str, protocol: Protocol, *, config: ProviderConfig | None = None) -> FormalAdoptionMock | None:
    return FormalAdoptionMock(protocol, config=config) if case_id == "formal.compaction-adoption" else None
