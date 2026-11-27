"""Request-scoped observations of the final model HTTP exchange, without body storage."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx2
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

if TYPE_CHECKING:
    from myrk.task_control import RequestAttempt


_GOOGLE_USAGE_FIELDS = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount", "totalTokenCount", "toolUsePromptTokenCount")


class WireRequestSummary(BaseModel):
    body_sha256: str
    body_bytes: int
    system_sha256: str
    tools_sha256: str
    messages_sha256: str
    message_count: int
    tool_count: int
    message_hashes: list[str]
    change_reasons: list[str] = Field(default_factory=list)


class RequestObservation(BaseModel):
    provider: str
    endpoint: str
    protocol: Literal["deepseek", "openai-chat", "google", "unknown"]
    model: str
    layout_version: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    input_source: str | None = None
    output_source: str | None = None
    thinking_source: str | None = None
    cache_read_source: str | None = None
    cache_write_source: str | None = None
    usage_present: bool = False
    usage_status: Literal["unknown", "partial", "complete", "conflict"] = "unknown"
    raw_usage: dict[str, int] = Field(default_factory=dict)
    field_presence: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys((
        "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens", "cache_write_tokens", "prompt_tokens_details.cached_tokens", "prompt_tokens_details.cache_write_tokens",
        "completion_tokens_details.reasoning_tokens",
    ), False))
    sdk_usage: dict[str, int] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    elapsed_seconds: float | None = None
    first_output_seconds: float | None = None
    finish_reason: str | None = None
    wire: WireRequestSummary | None = None


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class _Capture:
    def __init__(self, attempt: "RequestAttempt", change_reasons: list[str]) -> None:
        self.attempt = attempt
        assert attempt.observation is not None
        self.observation = attempt.observation
        self.change_reasons = change_reasons
        self.started = monotonic()

    def receive(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        observation = self.observation
        if observation.protocol == "google":
            candidates = value.get("candidates")
            if isinstance(candidates, list):
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    reason = candidate.get("finishReason")
                    if isinstance(reason, str):
                        observation.finish_reason = reason
                    content = candidate.get("content")
                    parts = content.get("parts") if isinstance(content, dict) else None
                    if isinstance(parts, list) and observation.first_output_seconds is None:
                        if any(isinstance(part, dict) and (part.get("text") or part.get("functionCall")) for part in parts):
                            observation.first_output_seconds = monotonic() - self.started
            usage = value.get("usageMetadata")
            if not isinstance(usage, dict):
                return
            observation.usage_present = True
            for key in _GOOGLE_USAGE_FIELDS:
                observation.field_presence[key] = observation.field_presence.get(key, False) or key in usage
            for key, count in usage.items():
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    observation.raw_usage[key] = count
            for target, source in (("input", "promptTokenCount"), ("thinking", "thoughtsTokenCount"), ("cache_read", "cachedContentTokenCount")):
                count = observation.raw_usage.get(source)
                setattr(observation, target + "_tokens", count)
                setattr(observation, target + "_source", "usageMetadata." + source if count is not None else None)
            candidates_count = observation.raw_usage.get("candidatesTokenCount")
            if candidates_count is not None and observation.thinking_tokens is not None:
                observation.output_tokens = candidates_count + observation.thinking_tokens
                observation.output_source = "usageMetadata.candidatesTokenCount + usageMetadata.thoughtsTokenCount"
            observation.conflicts = []
            if observation.cache_read_tokens is not None and observation.input_tokens is not None and observation.cache_read_tokens > observation.input_tokens:
                observation.conflicts.append("缓存读取超过完整输入，来源计数不一致。")
                observation.cache_read_tokens = None
                observation.cache_read_source = None
            observation.usage_status = "conflict" if observation.conflicts else "partial"
            self.attempt.input_tokens = observation.input_tokens
            self.attempt.output_tokens = observation.output_tokens
            return
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                reason = choice.get("finish_reason")
                if isinstance(reason, str):
                    observation.finish_reason = reason
                delta = choice.get("delta", choice.get("message"))
                if observation.first_output_seconds is None and isinstance(delta, dict) and any(delta.get(key) for key in ("content", "reasoning_content", "reasoning", "tool_calls", "function_call")):
                    observation.first_output_seconds = monotonic() - self.started
        usage = value.get("usage")
        if not isinstance(usage, dict):
            return
        observation.usage_present = True
        nested = usage.get("prompt_tokens_details")
        counters = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_write_tokens")}
        for key in ("cached_tokens", "cache_write_tokens"):
            counters["prompt_tokens_details." + key] = nested.get(key) if isinstance(nested, dict) else None
        for key, count in counters.items():
            present = key in usage if "." not in key else isinstance(nested, dict) and key.split(".")[1] in nested
            observation.field_presence[key] |= present
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                observation.raw_usage[key] = count
        reasoning = usage.get("completion_tokens_details")
        if isinstance(reasoning, dict):
            observation.field_presence["completion_tokens_details.reasoning_tokens"] |= "reasoning_tokens" in reasoning
            count = reasoning.get("reasoning_tokens")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                observation.raw_usage["completion_tokens_details.reasoning_tokens"] = count
        for target, source in (("input", "prompt_tokens"), ("output", "completion_tokens"), ("thinking", "completion_tokens_details.reasoning_tokens"), ("cache_read", "prompt_tokens_details.cached_tokens"), ("cache_write", "prompt_tokens_details.cache_write_tokens")):
            count = observation.raw_usage.get(source)
            setattr(observation, target + "_tokens", count)
            setattr(observation, target + "_source", "usage." + source if count is not None else None)
        conflicts = []
        if observation.protocol == "deepseek" and urlsplit(observation.endpoint).hostname == "api.deepseek.com":
            hit, miss = observation.raw_usage.get("prompt_cache_hit_tokens"), observation.raw_usage.get("prompt_cache_miss_tokens")
            if not observation.field_presence["prompt_tokens_details.cached_tokens"]:
                observation.cache_read_tokens = hit
                if hit is not None:
                    observation.cache_read_source = "usage.prompt_cache_hit_tokens"
            if hit is not None and observation.cache_read_tokens is not None and hit != observation.cache_read_tokens:
                conflicts.append("缓存读取的两个原始字段不一致。")
            if hit is not None and miss is not None and observation.input_tokens is not None and hit + miss != observation.input_tokens:
                conflicts.append("缓存命中与未命中之和不等于完整输入。")
        if observation.cache_read_tokens is not None and observation.input_tokens is not None and observation.cache_read_tokens > observation.input_tokens:
            conflicts.append("缓存读取超过完整输入，来源计数不一致。")
        observation.conflicts = conflicts
        if conflicts:
            observation.cache_read_tokens = None
            observation.cache_read_source = None
        observation.usage_status = "complete" if all(getattr(observation, key) is not None for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")) else "partial"
        if observation.conflicts:
            observation.usage_status = "conflict"
        self.attempt.input_tokens = observation.input_tokens
        self.attempt.output_tokens = observation.output_tokens

    def finish(self, usage: RequestUsage | None, *, completed: bool) -> None:
        self.observation.elapsed_seconds = monotonic() - self.started
        if not completed and self.observation.usage_status == "complete":
            self.observation.usage_status = "partial"
        if usage is not None:
            self.observation.sdk_usage = {key: getattr(usage, key) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}


_active_capture: ContextVar[_Capture | None] = ContextVar("myrk_request_observation", default=None)


def current_request_attempt() -> "RequestAttempt | None":
    """Return the product-owned attempt active at the model/HTTP boundary."""
    capture = _active_capture.get()
    return capture.attempt if capture is not None else None


def record_response_usage(attempt: "RequestAttempt", usage: RequestUsage) -> None:
    """Use original wire counters; unobserved models retain SDK usage semantics."""
    if attempt.observation is not None and attempt.observation.protocol != "unknown":
        attempt.input_tokens = attempt.observation.input_tokens
        attempt.output_tokens = attempt.observation.output_tokens
    elif usage.has_values():
        attempt.input_tokens = usage.input_tokens
        attempt.output_tokens = usage.output_tokens


async def _observe_http_request(request: httpx2.Request) -> None:
    capture = _active_capture.get()
    if capture is None:
        return
    body = request.content
    payload = json.loads(body)
    google = capture.observation.protocol == "google"
    messages = payload.get("contents" if google else "messages", [])
    tools = payload.get("tools", [])
    system = payload.get("systemInstruction", []) if google else [message for message in messages if message.get("role") in {"system", "developer"}]
    capture.observation.wire = WireRequestSummary(
        body_sha256=sha256(body).hexdigest(), body_bytes=len(body),
        system_sha256=_digest(system),
        tools_sha256=_digest(tools), messages_sha256=_digest(messages),
        message_count=len(messages), tool_count=sum(len(tool.get("functionDeclarations", [])) for tool in tools) if google else len(tools), message_hashes=[_digest(message) for message in messages],
        change_reasons=capture.change_reasons,
    )


class _ResponseBody:
    def __init__(self, capture: _Capture, *, sse: bool) -> None:
        self.capture, self.sse = capture, sse
        self.buffer = b""

    def receive_json(self, data: bytes) -> None:
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        self.capture.receive(value)

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if self.sse:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                    self.receive_json(line[5:])

    def finish(self) -> None:
        if self.sse and self.buffer.startswith(b"data:"):
            self.receive_json(self.buffer[5:])
        elif not self.sse:
            self.receive_json(self.buffer)
        self.buffer = b""


async def _observe_http_response(response: httpx2.Response) -> None:
    capture = _active_capture.get()
    if capture is None:
        return
    body = _ResponseBody(capture, sse="text/event-stream" in response.headers.get("content-type", ""))
    if response.is_stream_consumed:
        body.feed(response.content)
        body.finish()
        return
    decoded_bytes = response.aiter_bytes

    async def observed_bytes(chunk_size: int | None = None) -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in decoded_bytes(chunk_size):
                body.feed(chunk)
                yield chunk
            body.finish()
        finally:
            body.buffer = b""

    response.aiter_bytes = observed_bytes  # type: ignore[method-assign]


class _ObservedModel(WrapperModel):
    def __init__(self, model: Model, attempt: "RequestAttempt", change_reasons: list[str]) -> None:
        super().__init__(model)
        self.attempt = attempt
        self.change_reasons = change_reasons

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters) -> ModelResponse:
        capture = _Capture(self.attempt, self.change_reasons)
        token = _active_capture.set(capture)
        response = None
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
            return response
        finally:
            capture.finish(response.usage if response is not None else None, completed=response is not None)
            _active_capture.reset(token)

    @asynccontextmanager
    async def request_stream(self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters, run_context: RunContext[Any] | None = None) -> AsyncIterator[StreamedResponse]:
        capture = _Capture(self.attempt, self.change_reasons)
        token = _active_capture.set(capture)
        response = None
        completed = False
        try:
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response:
                yield response
            completed = True
        finally:
            capture.finish(response.get().usage if response is not None else None, completed=completed)
            _active_capture.reset(token)


def observe_request(model: Model, attempt: "RequestAttempt", *, layout_version: int | None = None, change_reasons: list[str] | None = None) -> Model:
    """Bind one ledger attempt to one SDK request, including injected models."""
    while isinstance(model, _ObservedModel):
        model = model.wrapped
    actual = model
    while isinstance(actual, WrapperModel):
        actual = actual.wrapped
    endpoint = urlsplit(model.base_url or "")
    safe_endpoint = urlunsplit((endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", ""))
    protocol: Literal["deepseek", "openai-chat", "google", "unknown"] = "unknown"
    http = None
    if isinstance(actual, OpenAIChatModel):
        protocol = "deepseek" if actual.system == "deepseek" else "openai-chat"
        http = actual.client._client
    elif isinstance(actual, GoogleModel):
        protocol = "google"
        google_http = actual.client._api_client._async_httpx_client
        assert isinstance(google_http, httpx2.AsyncClient)
        http = google_http
    if http is not None and _observe_http_request not in http.event_hooks["request"]:
        http.event_hooks["request"].append(_observe_http_request)
        http.event_hooks["response"].append(_observe_http_response)
    attempt.observation = RequestObservation(provider=model.system, endpoint=safe_endpoint, protocol=protocol, model=model.model_name, layout_version=layout_version)
    if protocol == "google":
        attempt.observation.field_presence = dict.fromkeys(_GOOGLE_USAGE_FIELDS, False)
    return _ObservedModel(model, attempt, list(change_reasons or []))
