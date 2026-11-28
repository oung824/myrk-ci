"""Scripted SDK transport for action-reporting tests."""

from contextlib import asynccontextmanager
import json

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider


def sdk_response(*, tool: str | None = None, arguments: dict | None = None,
                 text: str = "Synthetic transport completed.", call_id: str = "synthetic") -> httpx2.Response:
    delta: dict = {"role": "assistant"}
    if tool is None:
        delta["content"] = text
    else:
        delta["tool_calls"] = [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]
    chunk = {"id": call_id, "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
             "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}}
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")


@asynccontextmanager
async def synthetic_deepseek(respond):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com",
                               http_client=http, max_retries=0) as client:
            yield OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))


def supplied_context(payload: dict) -> str:
    return "\n".join(str(message["content"]) for message in payload["messages"]
                     if message["role"] not in {"assistant", "tool"})


def completed_command_response(request: httpx2.Request, *, requests: list[dict]) -> httpx2.Response:
    requests.append(json.loads(request.content))
    assert len(requests) <= 2, "The synthetic exchange has one command and one continuation."
    if len(requests) == 1:
        arguments = {
            "program": r"C:\Program Files\PowerShell\7\pwsh.exe",
            "arguments": ["-NoProfile", "-NonInteractive", "-Command",
                          "(Get-FileHash -LiteralPath 'check_report.py' -Algorithm SHA256).Hash"],
        }
        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "hash-only",
            "type": "function", "function": {"name": "run_command", "arguments": json.dumps(arguments)}}]}
        finish = "tool_calls"
    else:
        delta = {"role": "assistant", "content": "Synthetic transport completed."}
        finish = "stop"
    chunks = [
        {"id": f"facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
         "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": f"facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
         "model": "deepseek-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}},
    ]
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
        text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")


def metadata_read_response(
    request: httpx2.Request, *, requests: list[dict],
    steps: list[tuple[str, str, dict[str, str]]],
) -> httpx2.Response:
    requests.append(json.loads(request.content))
    assert len(requests) <= 4, "The synthetic exchange has three file actions and one continuation."
    if len(requests) <= len(steps):
        call_id, name, arguments = steps[len(requests) - 1]
        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id,
            "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        finish = "tool_calls"
    else:
        delta = {"role": "assistant", "content": "Synthetic transport completed."}
        finish = "stop"
    chunk = {"id": f"read-facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
             "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}}
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")


def delegation_response(request: httpx2.Request, *, requests: list[tuple[bool, dict]]) -> httpx2.Response:
    payload = json.loads(request.content)
    child = any(message["role"] == "user" and isinstance(message.get("content"), str)
                and message["content"].startswith("受委派工作：") for message in payload["messages"])
    requests.append((child, payload))
    assert len(requests) <= 6, "The synthetic delegation has four main requests and two child requests."
    results = {message["tool_call_id"]: message["content"]
               for message in payload["messages"] if message["role"] == "tool"}
    if child:
        if "child-source" not in results:
            return sdk_response(tool="read_text", arguments={"path": "result.txt"}, call_id="child-source")
        return sdk_response(text="Original synthetic child report.", call_id="child-answer")
    if "main-source" not in results:
        return sdk_response(tool="read_text", arguments={"path": "main.txt"}, call_id="main-source")
    if "delegation" not in results:
        return sdk_response(tool="delegate_work", arguments={
            "goal": "读取既有result.txt", "context": "只读既有产物，不修改文件。",
            "evidence_requirements": "引用产物原文。", "expected_files": ["result.txt"],
        }, call_id="delegation")
    if "artifact-verification" not in results:
        work = json.loads(results["delegation"])
        return sdk_response(tool="verify_delegation", arguments={"delegation_id": work["id"],
            "path": "result.txt", "expected_text": "independent-artifact-7319"}, call_id="artifact-verification")
    return sdk_response(call_id="main-answer")
