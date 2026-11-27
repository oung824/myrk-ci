import asyncio
import re
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from myrk.context import CurrentContext, RequestLayout
from myrk.conversation import Conversation, Task
from myrk.memory import MemoryChange, MemoryStore


def test_capacity_compresses_sent_preview_once_and_reopening_keeps_it_with_verifiable_original(tmp_path: Path) -> None:
    original = "开头原句。" + "中间获准原文。" * 4000 + "最后待核对937。"
    sent: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        sent.append(messages)
        if len(sent) == 1:
            return ModelResponse(parts=[ToolCallPart("read_source", {}, tool_call_id="source")])
        return ModelResponse(parts=[TextPart("仍按原始证据继续。")])

    def context(layout: RequestLayout) -> CurrentContext:
        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _text: None)
        current.layout = layout
        return current

    async def request(current: CurrentContext, history: list[ModelMessage], window: int) -> list[ModelMessage]:
        agent = Agent(FunctionModel(respond, profile={"context_window": window}), instructions="保留用户原话与证据。", capabilities=[current])

        @agent.tool_plain
        def read_source() -> str:
            """Read the single authorized source."""
            return original

        result = await agent.run("继续核对。", message_history=history)
        return result.all_messages()

    layout = RequestLayout(task_id="same-task", instructions="保留用户原话与证据。")
    first = context(layout)
    history = asyncio.run(request(first, [], 128000))
    initial_preview = next(str(part.content) for message in sent[-1] for part in message.parts if isinstance(part, ToolReturnPart))
    assert "各4000个字符" in initial_preview
    reopened_layout = RequestLayout.model_validate_json(layout.model_dump_json())
    constrained = context(reopened_layout)
    history = asyncio.run(request(constrained, history, 16000))
    compact_preview = next(str(part.content) for message in sent[-1] for part in message.parts if isinstance(part, ToolReturnPart))
    assert "各1000个字符" in compact_preview
    assert [change.reasons for change in reopened_layout.changes] == [["capacity_compression"]]
    compressed_version = reopened_layout.version
    restored = context(RequestLayout.model_validate_json(reopened_layout.model_dump_json()))
    history = asyncio.run(request(restored, history, 128000))
    assert next(str(part.content) for message in sent[-1] for part in message.parts if isinstance(part, ToolReturnPart)) == compact_preview
    assert restored.layout is not None and restored.layout.version == compressed_version
    assert [part.content for message in history for part in message.parts if isinstance(part, ToolReturnPart)] == [original]
    evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", compact_preview).group(1)
    assert original[-20:] in restored.read_evidence(evidence_id, len(original) - 20, 20, "")


def test_reading_the_same_long_source_again_refreshes_the_complete_memory_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "records" / "record.json"
    memories = MemoryStore(path.parent / "memories")
    removed_fact = "偏好绿茶。\n事实笔记结束。\n偏好咖啡。"
    memories.change([MemoryChange(action="add", text=removed_fact), MemoryChange(action="add", text="保留的独立事实。")], None)
    source = tmp_path / "source.txt"
    source.write_text("获准长资料。" * 3000, encoding="utf-8")
    task = Task(goal="两次核对相同长资料", document=str(source), scope=None)
    requests = 0

    async def respond(_messages: list[ModelMessage], info: AgentInfo):
        nonlocal requests
        requests += 1
        if requests in {1, 3}:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id=f"read-{requests}")}
        else:
            assert "保留的独立事实。" in info.instructions
            yield "有旧事实" if "偏好绿茶。" in info.instructions or "偏好咖啡。" in info.instructions else "旧事实整块已刷新"

    first = asyncio.run(Conversation(path).ask(task.goal, document=source, task=task, model=FunctionModel(stream_function=respond)))
    assert first.status == "completed" and first.answer == "有旧事实"
    memories.change([MemoryChange(action="remove", old_text=removed_fact)], None)
    reopened = Conversation.open(path)
    second = asyncio.run(reopened.ask("再读取相同资料。", document=source, task=reopened.turns[-1].task, model=FunctionModel(stream_function=respond)))
    assert second.status == "completed", second.error
    assert second.answer == "旧事实整块已刷新"


def test_reusing_a_sent_long_preview_refreshes_memory_without_another_tool_read(tmp_path: Path) -> None:
    path = tmp_path / "records" / "record.json"
    memories = MemoryStore(path.parent / "memories")
    fact = "需要更正的旧偏好。\n事实笔记结束。\n这也是同一条旧偏好。"
    memories.change([MemoryChange(action="add", text=fact)], None)
    source = tmp_path / "source.txt"
    source.write_text("获准长资料。" * 3000, encoding="utf-8")
    task = Task(goal="先读取，再按已有资料回答", document=str(source), scope=None)
    requests = 0

    async def respond(_messages: list[ModelMessage], info: AgentInfo):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="first-long-read")}
        else:
            if requests == 3:
                assert "更正后的当前偏好。" in info.instructions
            yield "包含旧偏好" if "需要更正的旧偏好。" in info.instructions or "这也是同一条旧偏好。" in info.instructions else "已采用当前事实"

    first = asyncio.run(Conversation(path).ask(task.goal, document=source, task=task, model=FunctionModel(stream_function=respond)))
    assert first.status == "completed" and first.answer == "包含旧偏好"
    memories.change([MemoryChange(action="replace", old_text=fact, text="更正后的当前偏好。")], None)
    reopened = Conversation.open(path)
    second = asyncio.run(reopened.ask("只用已有资料回答。", document=source, task=reopened.turns[-1].task, model=FunctionModel(stream_function=respond)))
    assert second.status == "completed", second.error
    assert second.answer == "已采用当前事实"
    assert requests == 3
