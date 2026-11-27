import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.exceptions import ModelHTTPError

from myrk.desktop_service import main


async def recovery_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
    prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    if any("用户明确选择继续原工作" in prompt for prompt in prompts):
        yield "用户选择后续办完成"
        return
    prompt = prompts[-1] if prompts else ""
    if prompt == "独立补货工作":
        yield "补货完成"
        return
    if prompt == "库存工作一次失败":
        await asyncio.sleep(0.25)
        raise ModelHTTPError(400, "controlled", {"message": "controlled failure before next work"})
    raise ModelHTTPError(503, "controlled", {"message": "temporary controlled outage"})


async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
    prompt = str(messages[-1].parts[0])
    if "检查思考设置" in prompt:
        yield "额外思考已开启" if _info.model_request_parameters.thinking else "额外思考已关闭"
        return
    if "检查旧资料" in prompt:
        parts = [part for message in messages for part in message.parts]
        has_tool = any(isinstance(part, ToolReturnPart) and "采购金额800以上需审批。" in str(part.content) for part in parts)
        has_thinking = any(isinstance(part, ThinkingPart) and part.content == "先核对以前资料。" for part in parts)
        has_call = any(isinstance(part, ToolCallPart) and part.tool_call_id == "read-1" for part in parts)
        has_provider = any(isinstance(message, ModelResponse) and message.provider_response_id == "old-response" for message in messages)
        yield "旧工具结果与思考都保留" if has_tool and has_thinking and has_call and has_provider else "历史缺失"
        return
    if "排版" in prompt:
        yield "# 示例回答\n\n- 第一项\n\n> 引用内容\n\n```python\nprint('你好')\n```"
        return
    yield "第一段"
    if "停止" in prompt:
        await asyncio.sleep(30)
    if "请求后保存失败" in prompt:
        record = next(Path(os.environ["MYRK_DATA_DIRECTORY"]).glob("*.json"))
        record.chmod(0o400)
    if "失败" in prompt:
        raise RuntimeError("synthetic-secret-never-save")
    await asyncio.sleep(0.03)
    yield "第二段"


async def lifecycle_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
    directory = Path(os.environ["MYRK_LIFECYCLE_DIRECTORY"])
    with (directory / "service.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"step": "request", "pid": os.getpid()}) + "\n")
    for count in range(10):
        yield f"进展{count + 1}。"
        await asyncio.sleep(0.1)
    await asyncio.sleep(30)


async def read_document(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [
        part.content for message in messages for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "read_authorized_text"
    ]
    if not results:
        yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="authorized-text")}
        return
    yield str(results[-1])


async def limited_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    prompt = next(
        str(part.content) for message in reversed(messages) for part in message.parts
        if isinstance(part, UserPromptPart)
    )
    if prompt == "反复读取":
        yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id=f"limit-read-{len(messages)}")}
    else:
        yield "由你决定后继续了。"


async def scoped_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    steps = [
        ("list_files", '{"path":"."}'),
        ("read_text", '{"path":"采购.md"}'),
        ("read_text", '{"path":"报销.txt"}'),
        ("search_text", '{"query":"审批","path":"采购.md"}'),
    ]
    if len(results) < len(steps):
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=args, tool_call_id=f"scope-{len(results)}")}
        return
    assert "unrequested-material-must-not-be-sent" not in str(messages)
    yield "\n".join(str(part.content) for part in results[1:3])


async def context_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    if not results:
        yield {0: DeltaToolCall(name="search_text", json_args='{"query":"needle","path":"long.txt"}', tool_call_id="long-search")}
        return
    if len(results) == 1:
        preview = str(results[0].content)
        assert "[long.txt:1]" in preview and "[long.txt:600]" in preview and "省略" in preview
        evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", preview).group(1)
        yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": evidence_id, "start": 36000, "length": 4000}), tool_call_id="read-sourced-middle")}
        return
    assert "[long.txt:300] needle 300:" in str(results[-1].content)
    yield "已回查真实中间原句[long.txt:300]；尚待核对的内容没有冒称读完。"


async def parallel_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    goal = next(prompt for prompt in reversed(prompts) if not prompt.startswith("补充："))
    call_id = f"parallel-{goal}"
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_call_id == call_id and part.outcome == "success"]
    if not results:
        yield "等待真实读取。"
        await asyncio.sleep(0.35)
        name = "second.txt" if "另一聊天" in goal or any(prompt == "补充：改读second.txt" for prompt in prompts) else "first.txt"
        yield {0: DeltaToolCall(name="read_text", json_args='{"path":"' + name + '"}', tool_call_id=call_id)}
        return
    yield str(results[-1].content)
    if "并等待" in goal:
        await asyncio.sleep(30)


async def action_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    if len(results) < 2:
        yield {0: DeltaToolCall(name="delete_file", json_args='{"path":".agents/notes.txt"}', tool_call_id=f"delete-{len(results)}")}
    else:
        yield "本次处理结束，删除被拒绝。"


async def delivery_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    steps = [("read_text", '{"path":"notes.txt"}'), ("edit_text", '{"path":"notes.txt","old_text":"苹果 3","new_text":"苹果 5"}'), ("describe_extension", '{"name":"calculate_table"}'), ("call_extension", ''), ("run_check", '{"name":"指定检查"}')]
    if len(results) < len(steps):
        name, args = steps[len(results)]
        if name == "call_extension":
            args = json.dumps({"name": "calculate_table", "schema_version": results[-1].content["schema_version"], "arguments": {"path": "amounts.csv", "column": "amount"}})
        yield {0: DeltaToolCall(name=name, json_args=args, tool_call_id=f"delivery-{len(results)}")}
    else:
        yield "\n".join(str(part.content) for part in results)


async def artifacts_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    if not results:
        yield {0: DeltaToolCall(name='create_text', json_args=json.dumps({'path': 'report.md', 'content': '实际交付正文\n'}), tool_call_id='artifact-create')}
    elif len(results) == 1:
        yield {0: DeltaToolCall(name='describe_extension', json_args='{"name":"deliver_files"}', tool_call_id='artifact-describe')}
    elif len(results) == 2:
        change = json.loads(str(results[0].content).split('文件改动证据：', 1)[1])
        yield {0: DeltaToolCall(name='call_extension', json_args=json.dumps({'name': 'deliver_files',
            'schema_version': results[1].content['schema_version'], 'arguments': {'change_ids': [change['id']]}}), tool_call_id='artifact-deliver')}
    else:
        yield '报告已明确交付。'


async def check_stop_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    if any(isinstance(part, UserPromptPart) and "另一聊天" in str(part.content) for message in messages for part in message.parts):
        yield "另一聊天正常完成"
    elif not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
        yield {0: DeltaToolCall(name="run_check", json_args='{"name":"持续检查"}', tool_call_id="desktop-stop-check")}
    else:
        yield "真实检查已结束"


async def memory_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "memory"]
    if not results:
        yield {0: DeltaToolCall(name="memory", json_args=json.dumps({"action": "add", "text": "用户喜欢简短的中文回复。"}), tool_call_id="remember-personal")}
    else:
        yield str(results[-1].content)


async def memory_review_reply(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    if "交付后有限复盘" in info.instructions:
        if not any(part.tool_call_id == "review-change" for part in results):
            yield {0: DeltaToolCall(name="memory", json_args=json.dumps({"action": "replace", "old_text": "用户喝绿茶。", "text": "用户喝红茶。"}), tool_call_id="review-change")}
        else:
            yield "复盘结束"
    elif not any(part.tool_name == "queue_memory_review" for part in results):
        yield {0: DeltaToolCall(name="queue_memory_review", json_args="{}", tool_call_id="queue-memory")}
    else:
        yield "主要工作交付。"


async def delegation_reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    if prompts[-1] == "普通问题":
        yield "普通问题直接回答。"
        return
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    child = prompts[0].startswith("受委派工作：")
    if child:
        steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"})]
        if len(results) == 2:
            yield "已改好，请主助理核验原文及指定检查。"
            return
    else:
        steps = [("update_plan", {"steps": ["选用相关步骤", "委派修改", "主助理核验"]}), ("read_skill", {"name": "edit-notes"}), ("read_skill", {"name": "edit-notes", "attachment": "details.txt"}), ("delegate_work", {"goal": "把苹果改为5", "context": "只改notes.txt的一处", "evidence_requirements": "保留原件及真实动作", "expected_files": ["notes.txt"]})]
        if len(results) == 4:
            report = json.loads(str(results[-1].content))
            yield {0: DeltaToolCall(name="verify_delegation", json_args=json.dumps({"delegation_id": report["id"], "path": "notes.txt", "expected_text": "苹果 5\n"}), tool_call_id="desktop-main-verify")}
            return
        if len(results) == 5:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"指定检查"}', tool_call_id="desktop-main-check")}
            return
        if len(results) == 6:
            assert "真实原文一致" in str(results[-2].content)
            assert "real-delegation-check" in str(results[-1].content)
            yield "主助理已经读回真实文件并运行指定检查，成果待你验收。"
            return
    name, args = steps[len(results)]
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"desktop-{'child' if child else 'main'}-{len(results)}")}


@asynccontextmanager
async def model_factory(_key: str) -> AsyncIterator[Model]:
    if _key == "synthetic-connect-failure":
        raise RuntimeError(_key)
    if _key == "memory-review-controlled":
        yield FunctionModel(stream_function=memory_review_reply, profile=ModelProfile(context_window=128000))
        return
    if _key == 'artifacts-controlled':
        yield FunctionModel(stream_function=artifacts_reply, profile=ModelProfile(context_window=128000))
        return
    try:
        yield FunctionModel(
            stream_function=memory_reply if _key == "memory-controlled" else recovery_reply if _key == "recovery-controlled" else delegation_reply if _key == "delegation-controlled" else check_stop_reply if _key == "check-stop-controlled" else delivery_reply if _key == "delivery-controlled" else action_reply if _key == "actions-controlled" else lifecycle_reply if _key == "lifecycle-controlled" else context_reply if _key == "context-controlled" else parallel_reply if _key == "parallel-controlled" else scoped_reply if _key == "scope-controlled" else limited_reply if _key == "limit-controlled" else read_document if _key == "document-controlled" else respond,
            profile=ModelProfile(context_window=128000, supports_thinking=True, supports_inline_system_prompts=_key in {"limit-controlled", "parallel-controlled"}),
        )
    finally:
        await asyncio.sleep(0.05)
        if _key == "lifecycle-controlled":
            await asyncio.sleep(0.2)
            with (Path(os.environ["MYRK_LIFECYCLE_DIRECTORY"]) / "service.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step": "cleanup", "pid": os.getpid()}) + "\n")
        if _key == "synthetic-close-failure":
            raise RuntimeError(_key)


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    if receipts := os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipts), __file__)
    if os.environ.get("MYRK_LIFECYCLE_DIRECTORY"):
        with (Path(os.environ["MYRK_LIFECYCLE_DIRECTORY"]) / "service.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"step": "started", "pid": os.getpid()}) + "\n")
    main(data_directory=Path(sys.argv[1]) if len(sys.argv) > 1 else None, model_factory=model_factory)
