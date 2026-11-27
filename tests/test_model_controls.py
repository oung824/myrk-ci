import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent, models

from myrk.model_controls import ModelSelection, chat_models, fetch_chat_models, reasoning_settings, resolve_selection, validate_selection
from myrk.provider import ProviderConfig, configured_model


def test_deepseek_controls_select_the_provider_default_without_default_or_off_stops() -> None:
    descriptor = chat_models(ProviderConfig())[0]

    assert descriptor.default_reasoning == "high"
    assert [(option.id, option.label) for option in descriptor.reasoning_options] == [("low", "低"), ("high", "高"), ("max", "最高")]


@pytest.mark.parametrize("protocol,endpoint,model_name,expected", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash", "high"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash", "dynamic"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-pro", "dynamic"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash-lite", "low"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3-flash-preview", "high"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3-pro-preview", "high"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3.1-pro-preview", "high"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3.5-flash-lite", "minimal"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3.6-flash", "medium"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3.8-flash", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5.4", "low"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5.5", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5-mini", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5-nano", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "o3", "medium"),
    ("openai-chat", "https://api.openai.com/v1", "o4-mini", "medium"),
])
def test_new_selection_uses_the_confirmed_initial_reasoning(protocol: str, endpoint: str, model_name: str, expected: str) -> None:
    config = ProviderConfig(name="Offline", base_url=endpoint, model=model_name, protocol=protocol)
    selection = validate_selection(ModelSelection(provider_config=config))
    descriptor = chat_models(config)[0]

    assert descriptor.default_reasoning == selection.reasoning == expected
    assert selection.reasoning in {option.id for option in descriptor.reasoning_options}
    assert not {"default", "none"} & {option.id for option in descriptor.reasoning_options}


@pytest.mark.parametrize("model_name", ["gemini-3.1-flash-lite-preview", "gemini-3.5-flash", "gemini-3.7-flash"])
def test_unconfirmed_google_default_keeps_the_actual_service_behavior_without_inventing_a_level(model_name: str) -> None:
    config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model=model_name, protocol="google")
    descriptor = chat_models(config)[0]

    assert descriptor.default_reasoning == "default"
    assert descriptor.reasoning_note and "默认档位尚未确认" in descriptor.reasoning_note
    assert descriptor.reasoning_options
    assert not {"default", "none"} & {option.id for option in descriptor.reasoning_options}
    assert validate_selection(ModelSelection(provider_config=config)).reasoning == "default"
    assert reasoning_settings(config, "default") == {}


@pytest.mark.parametrize("model_name", ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-mini"])
def test_non_reasoning_models_have_no_slider_levels(model_name: str) -> None:
    config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model=model_name, protocol="openai-chat")
    descriptor = chat_models(config)[0]

    assert descriptor.reasoning_options == []
    assert descriptor.reasoning_note == "此型号没有可调节的推理强度。"
    assert validate_selection(ModelSelection(provider_config=config)).reasoning == "default"
    assert reasoning_settings(config, "default") == {}


@pytest.mark.parametrize("protocol,endpoint,model_name,expected", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash", "low"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash", "low"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash-lite", "low"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-3.5-flash", "minimal"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5.4", "low"),
])
def test_saved_off_preference_moves_to_the_lowest_enabled_level_without_changing_the_saved_value(protocol: str, endpoint: str, model_name: str, expected: str) -> None:
    config = ProviderConfig(name="Offline", base_url=endpoint, model=model_name, protocol=protocol)
    saved = ModelSelection(provider_config=config, reasoning="none")

    assert resolve_selection(saved).reasoning == expected
    assert saved.reasoning == "none"
    with pytest.raises(ValueError, match="不支持"):
        validate_selection(saved)


@pytest.mark.parametrize("protocol,endpoint,model_name", [
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash-lite"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-5.4"),
])
def test_initial_low_level_explains_the_user_selected_override_of_the_off_default(protocol: str, endpoint: str, model_name: str) -> None:
    config = ProviderConfig(name="Offline", base_url=endpoint, model=model_name, protocol=protocol)
    descriptor = chat_models(config)[0]

    assert descriptor.default_reasoning == "low"
    assert descriptor.reasoning_note == "服务默认不启用推理；初始选择低档。"


@pytest.mark.parametrize("effort,expected", [("default", None), ("none", "none"), ("low", "low"), ("high", "high"), ("max", "max")])
def test_saved_deepseek_selection_sends_its_original_native_effort(effort: str, expected: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "收到。"}, "finish_reason": "stop"}]})

    async def run() -> None:
        config = ProviderConfig()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with asyncio.timeout(20):
                    await Agent(model, model_settings=reasoning_settings(config, effort)).run("请打招呼。")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert len(requests) == 1
    if expected is None:
        assert "reasoning_effort" not in requests[0]
    else:
        assert requests[0]["reasoning_effort"] == expected


@pytest.mark.parametrize("model_name,effort,expected", [
    ("gemini-2.5-flash", "default", None),
    ("gemini-2.5-flash", "dynamic", {"thinking_budget": -1}),
    ("gemini-2.5-flash", "none", {"thinking_budget": 0}),
    ("gemini-2.5-flash", "low", {"thinking_budget": 2048}),
    ("gemini-2.5-flash", "medium", {"thinking_budget": 8192}),
    ("gemini-2.5-flash", "high", {"thinking_budget": 24576}),
    ("gemini-2.5-pro", "low", {"thinking_budget": 2048}),
    ("gemini-2.5-pro", "dynamic", {"thinking_budget": -1}),
    ("gemini-2.5-pro", "medium", {"thinking_budget": 8192}),
    ("gemini-2.5-pro", "high", {"thinking_budget": 24576}),
    ("gemini-2.5-pro", "max", {"thinking_budget": 32768}),
    ("gemini-2.5-flash-lite", "none", {"thinking_budget": 0}),
    ("gemini-2.5-flash-lite", "low", {"thinking_budget": 2048}),
    ("gemini-3-flash-preview", "minimal", {"thinking_level": "MINIMAL"}),
    ("gemini-3-flash-preview", "medium", {"thinking_level": "MEDIUM"}),
    ("gemini-3.1-pro-preview", "medium", {"thinking_level": "MEDIUM"}),
    ("gemini-3.8-flash", "low", {"thinking_level": "LOW"}),
])
def test_gemini_slider_serializes_model_specific_levels_or_budgets(model_name: str, effort: str, expected: dict | None, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "收到。"}]}, "finishReason": "STOP"}]})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model=model_name, protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with asyncio.timeout(20):
                    await Agent(model, model_settings=reasoning_settings(config, effort)).run("请打招呼。")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert len(requests) == 1
    if expected is None:
        assert "thinkingConfig" not in requests[0].get("generationConfig", {})
    else:
        assert requests[0]["generationConfig"]["thinkingConfig"] == expected


@pytest.mark.parametrize("model_name,effort", [("gpt-5.4", "none"), ("gpt-5.4", "xhigh"), ("gpt-5.5", "low"), ("gpt-5.5", "medium"), ("gpt-5.5", "high")])
def test_official_openai_chat_effort_is_sent_without_sdk_approximation(model_name: str, effort: str, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1, "model": model_name,
                                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "收到。"}, "finish_reason": "stop"}]})

    async def run() -> None:
        config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model=model_name, protocol="openai-chat")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with asyncio.timeout(20):
                    await Agent(model, model_settings=reasoning_settings(config, effort)).run("请打招呼。")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert requests[0]["reasoning_effort"] == effort


def test_unknown_compatible_endpoints_do_not_inherit_official_capabilities() -> None:
    config = ProviderConfig(name="Gateway", base_url="https://example.invalid/v1", model="gpt-5.5", protocol="openai-chat")
    assert reasoning_settings(config, "default") == {}
    descriptor = chat_models(config)[0]
    assert descriptor.capability == "chat" and descriptor.tools_supported is None
    assert descriptor.reasoning_options == []
    assert descriptor.default_reasoning == "default"
    assert descriptor.reasoning_note == "当前连接的推理档位尚未确认。"
    with pytest.raises(ValueError, match="不支持"):
        validate_selection(ModelSelection(provider_config=config, reasoning="high"))


@pytest.mark.parametrize("endpoint", ["http://api.openai.com/v1", "https://api.openai.com:9443/v1", "https://api.openai.com/another-api"])
def test_official_host_alone_does_not_authorize_its_capability_table(endpoint: str) -> None:
    config = ProviderConfig(name="Other endpoint", base_url=endpoint, model="gpt-5.5", protocol="openai-chat")
    assert chat_models(config)[0].reasoning_options == []
    with pytest.raises(ValueError, match="不支持"):
        validate_selection(ModelSelection(provider_config=config, reasoning="high"))


@pytest.mark.parametrize("model_name", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna"])
def test_chat_tool_incompatible_models_are_rejected_before_sending(model_name: str) -> None:
    config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model=model_name, protocol="openai-chat")
    with pytest.raises(ValueError, match="工具"):
        validate_selection(ModelSelection(provider_config=config))


@pytest.mark.parametrize("model_name,effort", [("gemini-2.5-pro", "none"), ("gemini-3-pro-preview", "medium"), ("gemini-3.8-flash", "minimal")])
def test_gemini_rejects_model_specific_invalid_levels(model_name: str, effort: str) -> None:
    config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model=model_name, protocol="google")
    with pytest.raises(ValueError, match="不支持"):
        validate_selection(ModelSelection(provider_config=config, reasoning=effort))


def test_google_chat_directory_filters_dedicated_outputs_and_keeps_the_current_manual_model() -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return httpx2.Response(200, json={"models": [
                {"name": "models/gemini-2.5-flash", "displayName": "Gemini Flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ], "nextPageToken": "page-two"})
        return httpx2.Response(200, json={"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/future-unclassified", "supportedGenerationMethods": ["generateContent"]},
        ]})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="my-manual-model", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            result = await fetch_chat_models("synthetic-only", config, http_client=http)
        assert [entry.id for entry in result] == ["my-manual-model", "gemini-2.5-flash", "gemini-3.8-flash"]
        assert result[0].capability == "unknown"
        assert result[1].name == "Gemini Flash"
        assert result[1].tools_supported is True
        for entry in result:
            for option in entry.reasoning_options:
                validate_selection(ModelSelection(provider_config=config.model_copy(update={"model": entry.id}), reasoning=option.id))

    asyncio.run(run())
    assert len(sends) == 2


def test_custom_directory_recognizes_chat_purpose_without_assuming_reasoning_support() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": [{"id": item, "object": "model", "created": 1, "owned_by": "offline"}
                                                                   for item in ["gpt-5.4", "gpt-4o", "deepseek-flash", "gpt-image-1", "text-embedding-3-large", "unknown-alias"]]})

    async def run() -> None:
        config = ProviderConfig(name="Gateway", base_url="https://example.invalid/v1", model="current-alias", protocol="openai-chat")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            result = await fetch_chat_models("synthetic-only", config, http_client=http)
        assert [entry.id for entry in result] == ["current-alias", "gpt-5.4", "gpt-4o", "deepseek-flash"]
        assert result[0].capability == "unknown"
        for entry in result[1:]:
            assert entry.capability == "chat" and entry.tools_supported is None
            assert entry.reasoning_options == []
            validate_selection(ModelSelection(provider_config=config.model_copy(update={"model": entry.id})))

    asyncio.run(run())


def test_local_fallback_does_not_recommend_retired_gemini_preview() -> None:
    config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
    assert "gemini-3-pro-preview" not in {entry.id for entry in chat_models(config)}


def test_openai_chat_directory_does_not_list_specialized_models() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"object": "list", "data": [{"id": item, "object": "model", "created": 1, "owned_by": "offline"}
                                                                   for item in ["gpt-5.5", "gpt-image-1", "text-embedding-3-large", "whisper-1", "tts-1", "gpt-6.1-sol"]]})

    async def run() -> None:
        config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-5.5", protocol="openai-chat")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            result = await fetch_chat_models("synthetic-only", config, http_client=http)
        assert [entry.id for entry in result] == ["gpt-5.5", "gpt-6.1-sol"]
        assert result[0].tools_supported is True
        assert result[1].unavailable_reason and "工具" in result[1].unavailable_reason

    asyncio.run(run())


@pytest.mark.parametrize("protocol,legacy_thinking,expected", [
    ("deepseek", False, "none"),
    ("deepseek", True, "medium"),
    ("google", False, {"thinking_budget": 128}),
    ("google", True, {"include_thoughts": True}),
])
def test_legacy_boolean_keeps_its_original_request_behavior(protocol: str, legacy_thinking: bool, expected: object, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if protocol == "google":
            return httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "收到。"}]}, "finishReason": "STOP"}]})
        return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "收到。"}, "finish_reason": "stop"}]})

    async def run() -> None:
        config = ProviderConfig() if protocol == "deepseek" else ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-pro", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with asyncio.timeout(20):
                    await Agent(model, model_settings=reasoning_settings(config, None, legacy_thinking=legacy_thinking)).run("请打招呼。")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    actual = requests[0]["reasoning_effort"] if protocol == "deepseek" else requests[0]["generationConfig"]["thinkingConfig"]
    assert actual == expected


def test_directory_error_does_not_expose_the_connection_password() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"message": "invalid synthetic-only password", "type": "authentication_error"}})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            with pytest.raises(ValueError) as failure:
                await fetch_chat_models("synthetic-only", ProviderConfig(), http_client=http)
        assert "synthetic-only" not in str(failure.value)
        assert "连接密码已隐藏" in str(failure.value)

    asyncio.run(run())
