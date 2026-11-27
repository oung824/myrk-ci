"""Only the external HTTP/model boundary is replaced; SDKs and Myrk remain real."""

from collections.abc import AsyncIterator, Callable
import asyncio
from base64 import b64encode
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol as TypingProtocol

if TYPE_CHECKING:
    from tests.support.agent_acceptance.preparation import LiveGate
from uuid import uuid4

import httpx2
from pydantic_core import to_json
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from myrk.provider import ProviderConfig, configured_model
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.recorded_response import capture_response_metadata

active_sdk_input: ContextVar[str | None] = ContextVar("acceptance_sdk_input", default=None)


@dataclass(frozen=True)
class JudgeCall:
    call_id: str
    execution_id: str
    purpose: str

    @property
    def request_identity(self) -> dict[str, Any]:
        return {"number":None,"actor":"judge","purpose":self.purpose,"delegation_id":None,"judge_call_id":self.call_id}


active_judge_call: ContextVar[JudgeCall | None] = ContextVar("acceptance_judge_call", default=None)


@dataclass(frozen=True)
class Reply:
    text: str = ""
    tool: str | None = None
    arguments: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] = field(default_factory=dict)
    status: int = 200
    usage: bool = True
    interrupt: bool = False
    finish_reason: Literal["stop", "length", "content_filter"] = "stop"
    window_error: bool = False
    not_sent: bool = False
    timeout: bool = False
    cancel: bool = False


class AcceptanceSession(TypingProtocol):
    config: ProviderConfig
    exchanges: list[dict[str, Any]]
    sdk_inputs: list[dict[str, Any]]

    def model(self) -> AbstractAsyncContextManager[Model]: ...

    def save(self, path: Path, *, attempts: list[dict[str, Any]], run_id: str, case_id: str, task_id: str) -> Path: ...


class SessionFactory(TypingProtocol):
    def __call__(self, protocol: Protocol, replies: list[Reply], *, config: ProviderConfig | None = None) -> AcceptanceSession: ...


class SDKCapture(WrapperModel):
    def __init__(self, model: Model, inputs: list[dict[str, Any]], *, judge_purpose: str | None = None,
                 execution_id: str | None = None) -> None:
        super().__init__(model)
        self.inputs, self.judge_purpose, self.execution_id = inputs, judge_purpose, execution_id

    def judge_call(self, identity: str) -> JudgeCall | None:
        if self.judge_purpose is None or self.execution_id is None:
            return None
        return JudgeCall(identity,self.execution_id,self.judge_purpose)

    def capture(self, messages: list[ModelMessage], settings: ModelSettings | None, parameters: ModelRequestParameters) -> str:
        attempt = current_request_attempt()
        identity = uuid4().hex
        judge = self.judge_call(identity)
        self.inputs.append({"messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            "settings": settings, "parameters": json.loads(to_json(parameters)),
            "sdk_input_id": identity,
            "request_identity": {name: getattr(attempt, name) for name in
                ("number", "actor", "purpose", "delegation_id")} if attempt is not None else judge.request_identity if judge is not None else None})
        return identity

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters) -> ModelResponse:
        previous = active_sdk_input.get()
        identity = self.capture(messages, model_settings, model_request_parameters)
        active_sdk_input.set(identity)
        judge_token = active_judge_call.set(self.judge_call(identity))
        try:
            return await self.wrapped.request(messages, model_settings, model_request_parameters)
        finally:
            active_sdk_input.set(previous)
            active_judge_call.reset(judge_token)

    @asynccontextmanager
    async def request_stream(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                             model_request_parameters: ModelRequestParameters, run_context: RunContext[Any] | None = None) -> AsyncIterator[StreamedResponse]:
        previous = active_sdk_input.get()
        identity = self.capture(messages, model_settings, model_request_parameters)
        active_sdk_input.set(identity)
        judge_token = active_judge_call.set(self.judge_call(identity))
        try:
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response:
                yield response
        finally:
            active_sdk_input.set(previous)
            active_judge_call.reset(judge_token)


class _ReplyStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], record: dict[str, Any], interrupt: bool) -> None:
        self.chunks, self.record, self.interrupt = chunks, record, interrupt
        self.received = b""
        self.eof = self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            for chunk in self.chunks:
                self.received += chunk
                self.record["received_base64"] = b64encode(self.received).decode("ascii")
                self.record["received_utf8"] = self.received.decode("utf-8", errors="replace")
                yield chunk
            if self.interrupt:
                self.record["error"] = "synthetic interruption after delivered output"
                raise httpx2.ReadError(self.record["error"])
            self.eof = True
        except asyncio.CancelledError:
            self.record["response_body_state"] = "cancelled_during_body"
            raise

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.eof and "response_body_state" not in self.record:
            self.record["response_body_state"] = "closed_before_eof"


class OfflineSession:
    """Reusable SDK fixture; finite scripted responses reject unexpected sends."""

    def __init__(self, protocol: Protocol, replies: list[Reply], *, config: ProviderConfig | None = None,
                 gate: "LiveGate | None" = None, case_id: str = "", product_record: Path | None = None,
                 judge_purpose: str | None = None, execution_id: str | None = None,
                 terminal_transport: httpx2.MockTransport | None = None) -> None:
        self.gate, self.case_id, self.product_record = gate, case_id, product_record
        self.judge_purpose, self.execution_id = judge_purpose, execution_id
        if terminal_transport is not None and type(terminal_transport) is not httpx2.MockTransport:
            raise SharedControlError("Offline terminal injection requires a concrete MockTransport; no real HTTP sender is allowed.")
        self.terminal_transport = terminal_transport
        self.config = config or ProviderConfig(name="Acceptance synthetic", protocol=protocol,
            base_url="https://generativelanguage.googleapis.com" if protocol == "google" else "https://api.deepseek.com",
            model="gemini-2.5-flash" if protocol == "google" else "deepseek-flash")
        if self.config.protocol != protocol:
            raise ValueError("Fixture protocol differs from provider configuration.")
        self.protocol, self.replies = protocol, replies
        self.exchanges: list[dict[str, Any]] = []
        self.sdk_inputs: list[dict[str, Any]] = []

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        index = len(self.exchanges)
        if index >= len(self.replies):
            raise SharedControlError("Unplanned Mock HTTP attempt; fixed response script exhausted.")
        reply = self.replies[index]
        record: dict[str, Any] = {"attempt": index + 1, "method": request.method, "url": str(request.url),
            "request_utf8": request.content.decode("utf-8"), "status": reply.status, "received_utf8": "", "transport_provenance":"fixed_fixture"}
        attempt = current_request_attempt()
        record["request_identity"] = {name: getattr(attempt, name) for name in
            ("number", "actor", "purpose", "delegation_id")} if attempt is not None else None
        record["sdk_input_id"] = active_sdk_input.get()
        self.exchanges.append(record)
        if reply.cancel:
            raise asyncio.CancelledError("fixed_fixture terminal cancellation")
        if reply.timeout:
            raise httpx2.ReadTimeout("fixed_fixture terminal timeout", request=request)
        arguments = reply.arguments(json.loads(request.content)) if callable(reply.arguments) else reply.arguments
        if reply.window_error:
            body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "The input token count (40100) exceeds the maximum number of tokens allowed (40000)"}} if self.protocol == "google" else {
                "error": {"type": "invalid_request_error", "code": "context_length_exceeded", "message": "This request exceeds the context window."}}
            response = httpx2.Response(400, json=body)
            record["status"], record["received_utf8"] = 400, response.content.decode("utf-8")
            record["received_base64"] = b64encode(response.content).decode("ascii")
            record["response_metadata"] = capture_response_metadata(response, source="fixed_fixture")
            return response
        if reply.status != 200:
            body = {"error": {"code": reply.status, "status": "UNAVAILABLE", "message": "synthetic service failure"}}
            response = httpx2.Response(reply.status, json=body)
            record["received_utf8"] = response.content.decode("utf-8")
            record["received_base64"] = b64encode(response.content).decode("ascii")
            record["response_metadata"] = capture_response_metadata(response, source="fixed_fixture")
            return response
        streaming = json.loads(request.content).get("stream") is True or "streamGenerateContent" in request.url.path
        google_finish = {"stop": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY"}[reply.finish_reason]
        if not streaming:
            if self.protocol == "google":
                part = {"functionCall": {"name": reply.tool, "args": arguments}} if reply.tool else {"text": reply.text}
                data: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": google_finish}]}
                if reply.usage:
                    data["usageMetadata"] = {"promptTokenCount": 20, "candidatesTokenCount": 7, "thoughtsTokenCount": 0, "totalTokenCount": 27}
            else:
                message = {"role": "assistant", "tool_calls": [{"id": f"fixture-{index}", "type": "function", "function": {
                    "name": reply.tool, "arguments": json.dumps(arguments)}}]} if reply.tool else {"role": "assistant", "content": reply.text}
                data = {"id": f"acceptance-{index}", "object": "chat.completion", "created": 1, "model": self.config.model,
                        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if reply.tool else reply.finish_reason}]}
                if reply.usage:
                    data["usage"] = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}
            chunks = [json.dumps(data).encode("utf-8")]
            response = httpx2.Response(200, stream=_ReplyStream(chunks, record, reply.interrupt), headers={"content-type": "application/json"})
            record["response_metadata"] = capture_response_metadata(response, source="fixed_fixture")
            return response
        if self.protocol == "google":
            part = {"functionCall": {"name": reply.tool, "args": arguments}} if reply.tool else {"text": reply.text}
            events: list[dict[str, Any]] = [{"candidates": [{"content": {"role": "model", "parts": [part]}}]}]
            if not reply.interrupt:
                tail: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": google_finish}]}
                if reply.usage:
                    tail["usageMetadata"] = {"promptTokenCount": 20, "candidatesTokenCount": 7, "totalTokenCount": 27}
                events.append(tail)
        else:
            delta = {"tool_calls": [{"index": 0, "id": f"fixture-{index}", "type": "function",
                "function": {"name": reply.tool, "arguments": json.dumps(arguments)}}]} if reply.tool else {"content": reply.text}
            events = [{"choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]}]
            if not reply.interrupt:
                events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if reply.tool else reply.finish_reason}]})
                if reply.usage:
                    events.append({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}})
            events = [{"id": f"acceptance-{index}", "object": "chat.completion.chunk", "created": 1,
                       "model": self.config.model, **event} for event in events]
        chunks = [("data: " + json.dumps(event) + "\n\n").encode("utf-8") for event in events]
        if self.protocol != "google" and not reply.interrupt:
            chunks.append(b"data: [DONE]\n\n")
        response = httpx2.Response(200, stream=_ReplyStream(chunks, record, reply.interrupt), headers={"content-type": "text/event-stream"})
        record["response_metadata"] = capture_response_metadata(response, source="fixed_fixture")
        return response

    @asynccontextmanager
    async def model(self, *, observe_response: Callable[[], None] | None = None) -> AsyncIterator[Model]:
        def respond(request: httpx2.Request) -> httpx2.Response:
            response = self.respond(request)
            if response.status_code == 200 and observe_response is not None:
                observe_response()
            return response

        terminal = httpx2.MockTransport(respond if observe_response is not None else self.respond)
        transport: httpx2.AsyncBaseTransport = terminal
        if self.gate is not None:
            from tests.support.agent_acceptance.formal_service import _AuthorizedTransport
            if self.gate.authorization is None or self.gate.authorization.transport != "mock":
                raise SharedControlError("Offline sessions require an explicit Mock grant; real routes remain closed.")
            fixture = OfflineSession(self.protocol, list(self.replies), config=self.config)
            def local_failure() -> bool:
                failed = self.replies[len(self.exchanges) - 1].not_sent
                if failed:
                    fixture.replies.pop(len(fixture.exchanges))
                return failed
            def observe_stop() -> None:
                if self.product_record is not None and self.product_record.is_file():
                    assert self.gate is not None
                    self.gate.observe_product_stop(self.product_record, case_id=self.case_id, protocol=self.protocol)
            transport = _AuthorizedTransport(self.gate, self.case_id, self, "receiver",
                self.terminal_transport or httpx2.MockTransport(fixture.respond), observe_stop, local_failure,
                fault_origin="planned local pre-send failure from fixed_fixture")
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            async with configured_model("acceptance-synthetic-only", self.config, http_client=client) as model:
                yield SDKCapture(model, self.sdk_inputs,judge_purpose=self.judge_purpose,execution_id=self.execution_id)

    def save(self, path: Path, *, attempts: list[dict[str, Any]], run_id: str, case_id: str, task_id: str) -> Path:
        path.write_text(json.dumps({"run_id": run_id, "case_id": case_id, "task_id": task_id,
            "protocol": self.protocol, "semantic_status": "not_measured", "product_attempts": attempts,
            "sdk_inputs": self.sdk_inputs, "exchanges": self.exchanges}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
