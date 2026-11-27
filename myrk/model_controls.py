"""Model capabilities and exact provider settings for the message composer."""

from typing import Literal

import httpx2
from google.genai.types import ThinkingLevel
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.settings import ModelSettings

from myrk.provider import GoogleCatalogConnection, ProviderConfig, fetch_model_catalog


class ReasoningOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    capability: Literal["chat", "unknown"]
    reasoning_options: list[ReasoningOption]
    default_reasoning: str = "default"
    reasoning_note: str | None = None
    tools_supported: bool | None = None
    unavailable_reason: str | None = None


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_config: ProviderConfig
    reasoning: str = "default"


_LABELS = {"dynamic": "动态", "minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高"}
_OPENAI_SETTINGS: dict[str, OpenAIChatModelSettings] = {
    "none": {"openai_reasoning_effort": "none"},
    "minimal": {"openai_reasoning_effort": "minimal"},
    "low": {"openai_reasoning_effort": "low"},
    "medium": {"openai_reasoning_effort": "medium"},
    "high": {"openai_reasoning_effort": "high"},
    "xhigh": {"openai_reasoning_effort": "xhigh"},
    "max": {"openai_reasoning_effort": "max"},
}
_GOOGLE_EFFORTS = {
    "gemini-2.5-flash": ("dynamic", "none", "low", "medium", "high"),
    "gemini-2.5-flash-lite": ("none", "low", "medium", "high"),
    "gemini-2.5-pro": ("dynamic", "low", "medium", "high", "max"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    "gemini-3-pro-preview": ("low", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3.1-flash-lite-preview": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.8-flash": ("low", "medium", "high"),
}
_GOOGLE_LEVELS = {"minimal": ThinkingLevel.MINIMAL, "low": ThinkingLevel.LOW, "medium": ThinkingLevel.MEDIUM, "high": ThinkingLevel.HIGH}
_GOOGLE_BUDGETS = {"dynamic": -1, "none": 0, "low": 2048, "medium": 8192, "high": 24576, "max": 32768}
_GOOGLE_DEFAULTS = {
    "gemini-2.5-flash": "dynamic",
    "gemini-2.5-pro": "dynamic",
    "gemini-2.5-flash-lite": "low",
    "gemini-3-flash-preview": "high",
    "gemini-3-pro-preview": "high",
    "gemini-3.1-pro-preview": "high",
    "gemini-3.5-flash-lite": "minimal",
    "gemini-3.6-flash": "medium",
    "gemini-3.8-flash": "medium",
}
_OPENAI_EFFORTS = {
    "gpt-5.4": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.5": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5": ("minimal", "low", "medium", "high"),
    "gpt-5-mini": ("minimal", "low", "medium", "high"),
    "gpt-5-nano": ("minimal", "low", "medium", "high"),
    "o3": ("low", "medium", "high"),
    "o4-mini": ("low", "medium", "high"),
    "gpt-4.1": (),
    "gpt-4.1-mini": (),
    "gpt-4.1-nano": (),
    "gpt-4o": (),
    "gpt-4o-mini": (),
}
_OPENAI_DEFAULTS = {
    "gpt-5.4": "low",
    "gpt-5.5": "medium",
    "gpt-5": "medium",
    "gpt-5-mini": "medium",
    "gpt-5-nano": "medium",
    "o3": "medium",
    "o4-mini": "medium",
}
_OPENAI_CHAT_TOOL_LIMITS = {
    "gpt-6-astra": "当前连接方式下，此型号不能使用办事工具，请选择其他型号。",
    "gpt-6.1-sol": "当前连接方式下，此型号不能使用办事工具，请选择其他型号。",
    "gpt-6.1-terra": "当前连接方式下，此型号不能使用办事工具，请选择其他型号。",
    "gpt-6-sol": "此型号在当前连接下只能关闭推理使用工具，请选择其他型号。",
    "gpt-6-luna": "此型号在当前连接下只能关闭推理使用工具，请选择其他型号。",
}
_OPENAI_ENDPOINTS = {"https://api.openai.com", "https://api.openai.com/v1"}
_DEEPSEEK_ENDPOINTS = {"https://api.deepseek.com", "https://api.deepseek.com/v1"}


def connection_identity(config: ProviderConfig) -> tuple[str, str, str]:
    return config.name, config.base_url.rstrip("/"), config.protocol


def _native_efforts(config: ProviderConfig) -> tuple[str, ...]:
    if config.base_url in _DEEPSEEK_ENDPOINTS and config.model in {"deepseek-flash", "deepseek-v4-pro"}:
        return ("none", "low", "high", "max")
    if config.protocol == "google":
        return _GOOGLE_EFFORTS.get(config.model, ())
    if config.protocol == "openai-chat" and config.base_url in _OPENAI_ENDPOINTS:
        return _OPENAI_EFFORTS.get(config.model, ())
    return ()


def _descriptor(config: ProviderConfig) -> ChatModel:
    efforts = _native_efforts(config)
    default_reasoning = "default"
    note: str | None = "当前连接的推理档位尚未确认。"
    unavailable: str | None = None
    known = config.model in _GOOGLE_EFFORTS or config.model in _OPENAI_EFFORTS or config.model in _OPENAI_CHAT_TOOL_LIMITS or config.model in {"deepseek-flash", "deepseek-v4-pro"}
    tools: bool | None = None
    if config.base_url in _DEEPSEEK_ENDPOINTS and config.model in {"deepseek-flash", "deepseek-v4-pro"}:
        tools = True
        default_reasoning, note = "high", None
    elif config.protocol == "google" and config.model in _GOOGLE_EFFORTS:
        tools = True
        default_reasoning = _GOOGLE_DEFAULTS.get(config.model, "default")
        note = "服务默认档位尚未确认，可手动选择推理强度。" if default_reasoning == "default" else None
        if config.model == "gemini-2.5-flash-lite":
            note = "服务默认不启用推理；初始选择低档。"
    elif config.protocol == "openai-chat" and config.base_url in _OPENAI_ENDPOINTS:
        if config.model in _OPENAI_EFFORTS:
            tools = True
            default_reasoning = _OPENAI_DEFAULTS.get(config.model, "default")
            note = None if efforts else "此型号没有可调节的推理强度。"
            if config.model == "gpt-5.4":
                note = "服务默认不启用推理；初始选择低档。"
        elif config.model in _OPENAI_CHAT_TOOL_LIMITS:
            tools = False
            unavailable = _OPENAI_CHAT_TOOL_LIMITS[config.model]
    return ChatModel(id=config.model, name=config.model, capability="chat" if known else "unknown",
                     reasoning_options=[ReasoningOption(id=effort, label=_LABELS[effort]) for effort in efforts if effort not in {"default", "none"}],
                     default_reasoning=default_reasoning, reasoning_note=note,
                     tools_supported=tools, unavailable_reason=unavailable)


def chat_models(config: ProviderConfig) -> list[ChatModel]:
    names = [config.model]
    if config.base_url in _DEEPSEEK_ENDPOINTS:
        names += ["deepseek-flash", "deepseek-v4-pro"]
    elif config.protocol == "google":
        names += [name for name in _GOOGLE_EFFORTS if name != "gemini-3-pro-preview"]
    elif config.protocol == "openai-chat" and config.base_url in _OPENAI_ENDPOINTS:
        names += list(_OPENAI_EFFORTS)
    return [_descriptor(config.model_copy(update={"model": name})) for name in dict.fromkeys(names)]


def resolve_selection(selection: ModelSelection) -> ModelSelection:
    descriptor = _descriptor(selection.provider_config)
    if selection.reasoning == "default":
        return selection.model_copy(update={"reasoning": descriptor.default_reasoning})
    if selection.reasoning == "none" and descriptor.reasoning_options:
        lowest = next(option.id for option in descriptor.reasoning_options if option.id != "dynamic")
        return selection.model_copy(update={"reasoning": lowest})
    return selection


def validate_selection(selection: ModelSelection) -> ModelSelection:
    if selection.reasoning == "none":
        raise ValueError("当前型号不支持所选推理强度，请选择可用档位。")
    selection = resolve_selection(selection)
    descriptor = _descriptor(selection.provider_config)
    if descriptor.unavailable_reason:
        raise ValueError(descriptor.unavailable_reason)
    if selection.reasoning != descriptor.default_reasoning and selection.reasoning not in {option.id for option in descriptor.reasoning_options}:
        raise ValueError("当前型号不支持所选推理强度，请选择可用档位。")
    return selection


def reasoning_settings(config: ProviderConfig, reasoning: str | None, *, legacy_thinking: bool = False) -> ModelSettings:
    if reasoning is None:
        return {"thinking": legacy_thinking}
    descriptor = _descriptor(config)
    if descriptor.unavailable_reason:
        raise ValueError(descriptor.unavailable_reason)
    if reasoning == "default":
        return {}
    if reasoning not in _native_efforts(config):
        raise ValueError("当前型号不支持所选推理强度，请选择可用档位。")
    if config.protocol == "google":
        if config.model.startswith("gemini-2.5-"):
            return GoogleModelSettings(google_thinking_config={"thinking_budget": _GOOGLE_BUDGETS[reasoning]})
        return GoogleModelSettings(google_thinking_config={"thinking_level": _GOOGLE_LEVELS[reasoning]})
    return _OPENAI_SETTINGS[reasoning].copy()


async def fetch_chat_models(key: str, config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None) -> list[ChatModel]:
    if config.protocol == "google":
        catalog = await fetch_model_catalog(key, GoogleCatalogConnection(name=config.name, base_url="https://generativelanguage.googleapis.com", protocol="google"), http_client=http_client)
        entries = [(item.id, item.name) for item in catalog]
    else:
        try:
            async with AsyncOpenAI(api_key=key, base_url=config.base_url, http_client=http_client, max_retries=0, timeout=30) as client:
                page = await client.models.list()
                entries = [(item.id, item.id) for item in page.data]
        except OpenAIError as error:
            raise ValueError("模型目录读取失败：" + str(error).replace(key, "[连接密码已隐藏]")) from None
    models = {config.model: _descriptor(config)}
    for model_id, name in entries:
        descriptor = _descriptor(config.model_copy(update={"model": model_id}))
        if descriptor.capability == "chat" or model_id == config.model:
            models[model_id] = descriptor.model_copy(update={"name": name})
    return list(models.values())
