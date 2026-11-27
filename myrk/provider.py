from collections.abc import AsyncIterator, Callable, Hashable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from google.genai import errors
from google.genai.types import ContentUnionDict, GenerateContentConfigDict, GenerateContentResponse, HttpRetryOptions
from openai import AsyncOpenAI, OpenAIError
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.exceptions import ModelAPIError, ModelConnectionError, ModelHTTPError, ModelQuotaExceededError
from pydantic_ai.messages import Citation, ModelMessage, ModelResponse, ModelResponsePart, ModelResponseStreamEvent, ProviderDetailsDelta, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.profiles.openai import OpenAIModelProfile

ModelFactory = Callable[[str], AbstractAsyncContextManager[Model]]

_DEEPSEEK_CHAT_PROFILE: OpenAIModelProfile = {"openai_chat_supports_max_completion_tokens": False}


def is_temporary_model_error(error: Exception) -> bool:
    return isinstance(error, ModelConnectionError) or isinstance(error, ModelHTTPError) and not isinstance(error, ModelQuotaExceededError) and error.status_code in {408, 429, 500, 502, 503, 504}


class IncompleteGoogleResponse(ModelAPIError):
    """A Google response ended without a successful native finish reason."""


def google_response_error(response: ModelResponse) -> str | None:
    reason = (response.provider_details or {}).get("finish_reason")
    if reason == "STOP" and response.finish_reason in {"stop", "tool_call"}:
        return None
    return f"Gemini 回应未完整结束（结束原因：{reason or '缺少结束原因'}），已保留收到的内容和用量；没有自动续接。"


@dataclass
class _SignedGoogleParts(ModelResponsePartsManager):
    _google_part_ids: dict[Hashable, Hashable] = field(default_factory=dict)

    def _signature_part_id(self, vendor_part_id: Hashable | None, signed: bool) -> Hashable | None:
        routed = self._google_part_ids.get(vendor_part_id, vendor_part_id) if vendor_part_id is not None else None
        index = self._vendor_id_to_part_index.get(routed) if routed is not None else len(self._parts) - 1 if self._parts else None
        previous = self._parts[index] if index is not None else None
        if signed or previous is not None and previous.provider_details and previous.provider_details.get("thought_signature"):
            routed = uuid4()
            if vendor_part_id is not None:
                self._google_part_ids[vendor_part_id] = routed
        return routed

    def handle_thinking_delta(self, *, vendor_part_id: Hashable | None, content: str | None = None,
                              id: str | None = None, signature: str | None = None, provider_name: str | None = None,
                              provider_details: ProviderDetailsDelta = None) -> Iterator[ModelResponseStreamEvent]:
        routed = self._signature_part_id(vendor_part_id, isinstance(provider_details, dict) and bool(provider_details.get("thought_signature")))
        yield from super().handle_thinking_delta(vendor_part_id=routed, content=content, id=id, signature=signature,
                                                 provider_name=provider_name, provider_details=provider_details)

    def handle_text_delta(self, *, vendor_part_id: Hashable | None, content: str, id: str | None = None,
                          provider_name: str | None = None, provider_details: dict[str, Any] | None = None,
                          citations: list[Citation] | None = None, thinking_tags: tuple[str, str] | None = None,
                          ignore_leading_whitespace: bool = False) -> Iterator[ModelResponseStreamEvent]:
        routed = self._signature_part_id(vendor_part_id, bool(provider_details and provider_details.get("thought_signature")))
        yield from super().handle_text_delta(vendor_part_id=routed, content=content, id=id, provider_name=provider_name,
                                             provider_details=provider_details, citations=citations, thinking_tags=thinking_tags,
                                             ignore_leading_whitespace=ignore_leading_whitespace)


class _SingleRequestGoogleModel(GoogleModel):
    async def _process_streamed_response(self, response: AsyncIterator[GenerateContentResponse], model_request_parameters: ModelRequestParameters) -> StreamedResponse:
        streamed = await super()._process_streamed_response(response, model_request_parameters)
        streamed._parts_manager = _SignedGoogleParts(model_request_parameters=model_request_parameters)
        return streamed

    async def _build_content_and_config(self, messages: list[ModelMessage], model_settings: GoogleModelSettings, model_request_parameters: ModelRequestParameters) -> tuple[list[ContentUnionDict], GenerateContentConfigDict]:
        marker = f"myrk-empty-thought-{uuid4().hex}"
        empty_thoughts = 0
        mapped_messages: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                parts: list[ModelResponsePart] = []
                for part in message.parts:
                    if (isinstance(part, ThinkingPart) and not part.content and part.provider_details
                            and part.provider_details.get("thought_signature") and self.accepts_stateless_artifacts(message, part)):
                        parts.append(replace(part, content=marker))
                        empty_thoughts += 1
                    else:
                        parts.append(part)
                mapped_messages.append(replace(message, parts=parts))
            else:
                mapped_messages.append(message)
        contents, config = await super()._build_content_and_config(mapped_messages, model_settings, model_request_parameters)
        restored = 0
        for content in contents:
            if isinstance(content, dict):
                content_parts = content.get("parts")
                if isinstance(content_parts, list):
                    for content_part in content_parts:
                        if isinstance(content_part, dict) and content_part.get("text") == marker and content_part.get("thought"):
                            content_part["text"] = ""
                            restored += 1
        if restored != empty_thoughts:
            raise ValueError("Gemini 带签名空思考部件未能完整恢复，未发送请求。")
        config["automatic_continuation"] = False
        return contents, config


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(default="DeepSeek", min_length=1)
    base_url: str = "https://api.deepseek.com"
    model: str = Field(default="deepseek-flash", min_length=1)
    protocol: Literal["deepseek", "openai-chat", "google"] = "deepseek"
    review_model_override: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    preferred_review_model: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    available_models: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)
    model_catalog_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    basic_session_source: bool = Field(default=False, exclude_if=lambda value: not value)
    review_reasoning_efforts: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    model_specialty: str | None = Field(default=None, exclude_if=lambda value: value is None)
    context_window_override: int | None = Field(default=None, ge=1, strict=True, exclude_if=lambda value: value is None)
    input_token_limit_override: int | None = Field(default=None, ge=1, strict=True, exclude_if=lambda value: value is None)
    output_token_reserve: int = Field(default=8192, ge=1, strict=True)
    context_safety_margin: int = Field(default=1024, ge=0, strict=True)
    auto_compaction: bool = True
    compaction_trigger: float = Field(default=0.8, gt=0, le=1)
    compaction_target: float = Field(default=0.55, gt=0, le=1)
    compaction_recent: float = Field(default=0.2, gt=0, le=1)

    @property
    def review_model_name(self) -> str:
        if self.review_model_override is not None:
            return self.review_model_override
        if self.preferred_review_model in (self.available_models or []):
            assert self.preferred_review_model is not None
            return self.preferred_review_model
        return self.model

    @field_validator("base_url")
    @classmethod
    def normalized_endpoint(cls, value: str) -> str:
        return str(AnyHttpUrl(value)).rstrip("/")

    @model_validator(mode="after")
    def public_endpoint(self) -> Self:
        endpoint = urlsplit(self.base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("请填写不含连接密码、查询参数或片段的完整服务地址。")
        if self.protocol == "deepseek" and endpoint.hostname != "api.deepseek.com":
            raise ValueError("DeepSeek（当前服务）消息格式只用于其正式地址；其他地址请明确选择普通消息格式。")
        if self.protocol == "google" and self.base_url != "https://generativelanguage.googleapis.com":
            raise ValueError("Gemini 官方消息格式只用于 Google Gemini Developer API 正式地址。")
        return self

    @property
    def recipient(self) -> Literal["deepseek", "openai-compatible", "google"]:
        return "openai-compatible" if self.protocol == "openai-chat" else self.protocol

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.name, self.base_url.rstrip("/"), self.model, self.protocol


class GoogleCatalogConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    base_url: Literal["https://generativelanguage.googleapis.com"]
    protocol: Literal["google"]


class ModelCatalogEntry(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    capability: Literal["unknown"] = "unknown"


async def fetch_model_catalog(key: str, connection: GoogleCatalogConnection, *, http_client: httpx2.AsyncClient | None = None) -> list[ModelCatalogEntry]:
    first_page_received = False
    try:
        async with GoogleProvider(api_key=key, base_url=connection.base_url, http_client=http_client, retry_options=HttpRetryOptions(attempts=1)) as provider:
            catalog = await provider.client.aio.models.list(config={"page_size": 100})
            first_page_received = True
            entries = []
            async for item in catalog:
                if item.supported_actions is not None and "generateContent" not in item.supported_actions:
                    continue
                if item.name is None:
                    raise ValueError("模型目录缺少型号 ID，未采用不完整的目录。")
                model_id = item.name.removeprefix("models/")
                entries.append(ModelCatalogEntry(id=model_id, name=item.display_name or model_id))
            return entries
    except errors.APIError as error:
        phase = "后续分页读取失败，未采用不完整的目录" if first_page_received else "模型目录读取失败"
        category = {401: "认证失败", 403: "权限不足", 429: "额度限制", 500: "服务暂不可用", 502: "服务暂不可用", 503: "服务暂不可用", 504: "服务暂不可用"}.get(error.code, "服务拒绝请求")
        message = (error.message or "未提供说明").replace(key, "[连接密码已隐藏]")
        raise ValueError(f"{phase}：{category}；HTTP {error.code}；{error.status or '未知状态'}；{message}") from None
    except (httpx2.HTTPError, TimeoutError):
        phase = "后续分页读取失败，未采用不完整的目录" if first_page_received else "模型目录读取失败"
        raise ValueError(f"{phase}：网络连接失败，请检查网络后重试。") from None


@asynccontextmanager
async def configured_model(key: str, config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[Model]:
    if config.protocol == "google":
        async with GoogleProvider(api_key=key, base_url=config.base_url, http_client=http_client, retry_options=HttpRetryOptions(attempts=1)) as google_provider:
            yield _SingleRequestGoogleModel(config.model, provider=google_provider, settings={"max_retries": 0})
        return
    async with AsyncOpenAI(api_key=key, base_url=config.base_url, http_client=http_client, max_retries=0) as client:
        provider = DeepSeekProvider(openai_client=client) if config.protocol == "deepseek" else OpenAIProvider(openai_client=client)
        yield OpenAIChatModel(config.model, provider=provider,
            profile=_DEEPSEEK_CHAT_PROFILE if config.protocol == "deepseek" else None)


@asynccontextmanager
async def deepseek_model(key: str, *, http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[Model]:
    async with AsyncOpenAI(
        api_key=key, base_url="https://api.deepseek.com", http_client=http_client, max_retries=0
    ) as client:
        yield OpenAIChatModel(
            "deepseek-flash", provider=DeepSeekProvider(openai_client=client), profile=_DEEPSEEK_CHAT_PROFILE
        )


async def fetch_review_catalog(key: str, config: ProviderConfig) -> list[str]:
    """Refresh only the current provider; a failed catalog lookup is one attempted refresh."""
    try:
        async with AsyncOpenAI(api_key=key, base_url=config.base_url, max_retries=0, timeout=30) as client:
            catalog = await client.models.list()
            return [item.id for item in catalog.data]
    except OpenAIError:
        return []
