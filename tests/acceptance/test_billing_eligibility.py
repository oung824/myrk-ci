"""Billing eligibility is frozen by preparation and never grants spending authority."""

from datetime import datetime, timezone
import json
from pathlib import Path

import httpx2
import pytest

from tests.support.agent_acceptance import AcceptancePlan, LiveGate, builtin_registry, prepare_live
from tests.support.agent_acceptance.connection_desktop_lifecycle import score


def test_preparation_records_unqualified_routes_without_account_or_network_access(prepared_root: Path) -> None:
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=prepared_root, registry=builtin_registry())
    routes = prepared.eligibility
    assert [(item.role, item.model, item.actual_model) for item in routes] == [
        ("product", "deepseek-flash", "DeepSeek-V4.1-Flash-1017"),
        ("product", "gemini-2.5-flash", "gemini-2.5-flash"),
        ("judge", "gemini-2.5-pro", "gemini-2.5-pro"),
    ]
    assert all(item.status == "unqualified" and not item.sending_enabled for item in routes)
    assert all({"input_billing_bound", "total_output_billing_bound", "account_usd_settlement", "surcharge_bound"}
               <= {gap.code for gap in item.unresolved} for item in routes)
    assert [item.standard_version for item in routes] == ["2026-11-26", "2026-11-16", "2026-11-16"]
    assert routes[0].thinking == {"reasoning_effort": "high"}
    assert routes[0].model_dump()["entry_point"] == "ordinary_new_chat"
    assert all("entry_point" not in item.model_dump() for item in routes[1:])
    assert all(item.observed_at.tzinfo is not None for item in routes)
    assert [item.reservation_microusd for item in routes] == [14746, 20071, 122880]


def test_real_authorization_with_unknown_billing_bounds_cannot_reserve_a_send(prepared_root):
    from tests.support.agent_acceptance import LiveAuthorization, SharedControlError

    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=prepared_root, registry=builtin_registry())
    gate = LiveGate(prepared_root, builtin_registry(), LiveAuthorization(authorization_id="offline-example",
        binding=prepared.binding, total_limit=1, case_limits={"ordinary.question": 1}))
    with pytest.raises(SharedControlError, match="unqualified.*input_billing_bound"):
        gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work")
    assert json.loads((prepared_root / "ledger.json").read_bytes())["attempts"] == []


def test_legacy_deepseek_observations_apply_effective_thinking_and_frozen_scope(
        prepared_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.agent_acceptance import preparation
    from tests.support.agent_acceptance.eligibility import preparation_eligibility

    registry = builtin_registry()
    with monkeypatch.context() as patch:
        patch.setattr(preparation, "preparation_eligibility",
                      lambda: preparation_eligibility(deepseek_standard="2026-11-16"))
        prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                                root=prepared_root, registry=registry)
    route = next(route for route in prepared.eligibility if route.protocol == "deepseek")
    assert route.standard_version == "2026-11-16" and route.thinking == {"reasoning_effort": "none"}
    gate = LiveGate(prepared_root, registry)
    ledger = prepared_root / "ledger.json"
    original_ledger = ledger.read_bytes()
    scenario = tmp_path / "work/scenario.json"
    scenario.parent.mkdir()
    scenario.write_text('{"protocol":"deepseek"}', encoding="utf-8")

    local_function = {"type": "function", "function": {"name": "read_text"}}
    cases: list[tuple[str, dict[str, object], bool, bool]] = [
        ("legacy-off", {"reasoning_effort": "none"}, True, True),
        ("native-off", {"thinking": {"type": "disabled"}}, True, True),
        ("native-off-wins", {"thinking": {"type": "disabled"}, "reasoning_effort": "high"}, True, True),
        ("native-on-wins", {"thinking": {"type": "enabled"}, "reasoning_effort": "none"}, False, False),
        ("null-falls-back", {"thinking": None, "reasoning_effort": "none"}, True, True),
        ("defaults-to-thinking", {}, False, False),
        ("missing-type", {"thinking": {}, "reasoning_effort": "none"}, False, False),
        ("invalid-thinking", {"thinking": [], "reasoning_effort": "none"}, False, False),
        ("local-tool-and-default-safety", {"reasoning_effort": "none", "tools": [local_function],
                                           "safety_check": {"level": "low"}}, True, True),
        ("server-search", {"reasoning_effort": "none", "tools": [{"type": "deepseek_web_search"}]}, True, False),
        ("server-fetch", {"reasoning_effort": "none", "tools": [{"type": "deepseek_read_url"}]}, True, False),
        ("additional-safety", {"reasoning_effort": "none", "safety_check": {"level": "high"}}, True, False),
        ("invalid-safety", {"reasoning_effort": "none", "safety_check": {}}, True, False),
        ("unrecognized-tools", {"reasoning_effort": "none", "tools": [None]}, True, False),
    ]
    for name, settings, off, matches in cases:
        body = {"model": route.model, "max_tokens": 4096, "messages": [{"role": "user", "content": "Offline."}],
                **settings}
        request = httpx2.Request("POST", route.endpoint + "/chat/completions", json=body)
        observation = gate.inspect_native_request(request, protocol="deepseek", role="product",
            config=route.configuration, now=datetime(2026, 11, 21, tzinfo=timezone.utc))
        wire = {"url": str(request.url), "request_utf8": request.content.decode("utf-8")}
        contract = score(tmp_path, [], [{"exchanges": [wire], "product_attempts": [], "sdk_inputs": []}])["connection_contract"]
        assert contract["native_settings_per_http"] == [off], name
        assert contract["effective_native_setting"] is off, name
        assert observation.native_settings_match is matches, name
        assert observation.route_current and not observation.sending_enabled, name
        assert "input_billing_bound" in observation.unresolved, name
        assert ("non_text_or_paid_feature" in observation.unresolved) is (off and not matches), name

    assert ledger.read_bytes() == original_ledger and json.loads(original_ledger)["attempts"] == []


def test_deepseek_default_high_with_4096_reaches_final_http_without_billing_eligibility(prepared_root: Path) -> None:
    import asyncio
    from pydantic_ai import Agent
    from myrk.model_controls import ModelSelection, reasoning_settings, resolve_selection
    from myrk.provider import ProviderConfig, configured_model

    prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                 root=prepared_root, registry=builtin_registry())
    gate = LiveGate(prepared_root, builtin_registry())
    observations = []
    bodies = []
    config = ProviderConfig(output_token_reserve=4096)
    selection = resolve_selection(ModelSelection(provider_config=config))
    ledger = prepared_root / "ledger.json"
    original_ledger = ledger.read_bytes()

    def respond(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        observations.append(gate.inspect_native_request(request, protocol="deepseek", role="product", config=config,
            now=datetime(2026, 11, 26, tzinfo=timezone.utc)))
        return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture"},
            "finish_reason": "stop"}]})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                await Agent(model, model_settings={**reasoning_settings(config, selection.reasoning),
                                                   "max_tokens": config.output_token_reserve}).run("Please reply briefly.")

    asyncio.run(run())
    assert len(observations) == 1
    assert selection.reasoning == bodies[0]["reasoning_effort"] == "high"
    assert bodies[0]["model"] == "deepseek-flash"
    assert type(bodies[0]["max_tokens"]) is int and bodies[0]["max_tokens"] == 4096
    observation = observations[0]
    assert observation.native_settings_match and observation.route_current
    assert not observation.sending_enabled
    assert "input_billing_bound" in observation.unresolved
    assert "total_output_billing_bound" in observation.unresolved
    assert observation.request_sha256
    assert ledger.read_bytes() == original_ledger and json.loads(original_ledger)["attempts"] == []


@pytest.mark.parametrize("settings,expected_match,extra_feature", [
    pytest.param({}, True, False, id="ordinary-high"),
    pytest.param({"thinking": {"type": "enabled"}}, True, False, id="explicit-thinking-enabled"),
    pytest.param({"thinking": None}, True, False, id="default-thinking"),
    pytest.param({"thinking": {"type": "disabled"}}, False, False, id="disabled-thinking-overrides-high"),
    pytest.param({"thinking": {}}, False, False, id="invalid-thinking"),
    pytest.param({"reasoning_effort": None}, False, False, id="no-explicit-high"),
    pytest.param({"reasoning_effort": "low"}, False, False, id="low-effort"),
    pytest.param({"max_tokens": 4095}, False, False, id="under-exact-output"),
    pytest.param({"max_tokens": 4097}, False, False, id="over-exact-output"),
    pytest.param({"max_tokens": 4096.0}, False, False, id="noninteger-output"),
    pytest.param({"title": {"enabled": False}}, True, False, id="conversation-title-disabled"),
    pytest.param({"title": {"enabled": True}}, False, True, id="conversation-title-enabled"),
    pytest.param({"title": {"enabled": "false"}}, False, True, id="invalid-conversation-title"),
])
def test_deepseek_high_native_settings_require_exact_output_and_no_title_feature(
        prepared_root: Path, settings: dict[str, object], expected_match: bool, extra_feature: bool) -> None:
    registry = builtin_registry()
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=prepared_root, registry=registry)
    route = next(route for route in prepared.eligibility if route.protocol == "deepseek")
    gate = LiveGate(prepared_root, registry)
    ledger = prepared_root / "ledger.json"
    original_ledger = ledger.read_bytes()
    request = httpx2.Request("POST", route.endpoint + "/chat/completions", json={
        "model": route.model, "max_tokens": 4096, "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "Offline."}], **settings,
    })

    observation = gate.inspect_native_request(request, protocol="deepseek", role="product",
        config=route.configuration, now=datetime(2026, 11, 26, tzinfo=timezone.utc))

    assert observation.route_current and not observation.sending_enabled
    assert observation.native_settings_match is expected_match
    assert ("native_settings_mismatch" in observation.unresolved) is (not expected_match)
    assert ("non_text_or_paid_feature" in observation.unresolved) is extra_feature
    assert "total_output_billing_bound" in observation.unresolved
    assert ledger.read_bytes() == original_ledger and json.loads(original_ledger)["attempts"] == []


@pytest.mark.parametrize("role,model_name,reasoning,budget,output", [
    ("product", "gemini-2.5-flash", "low", 2048, 4096),
    ("judge", "gemini-2.5-pro", None, 128, 8192),
])
def test_google_final_http_records_soft_thinking_settings_without_granting_billing_eligibility(
        prepared_root, role, model_name, reasoning, budget, output):
    import asyncio
    from pydantic_ai import Agent
    from myrk.model_controls import reasoning_settings
    from myrk.provider import ProviderConfig, configured_model

    prepare_live(AcceptancePlan(cases=["ordinary.question"], protocols=["google"], mode="prepare-live"),
                 root=prepared_root, registry=builtin_registry())
    gate = LiveGate(prepared_root, builtin_registry())
    observations = []
    bodies = []
    config = ProviderConfig(name="Gemini", protocol="google", base_url="https://generativelanguage.googleapis.com",
                            model=model_name, output_token_reserve=output)

    def respond(request):
        bodies.append(json.loads(request.content))
        observations.append(gate.inspect_native_request(request, protocol="google", role=role, config=config,
            now=datetime(2026, 11, 16, tzinfo=timezone.utc)))
        return httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "fixture"}]},
                                                       "finishReason": "STOP"}]})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                await Agent(model, model_settings={**reasoning_settings(config, reasoning, legacy_thinking=False),
                                                   "max_tokens": output}).run("Please reply briefly.")

    asyncio.run(run())
    assert len(observations) == 1
    assert bodies[0]["generationConfig"]["thinkingConfig"] == {"thinking_budget": budget}
    observation = observations[0]
    assert observation.native_settings_match and observation.route_current
    assert not observation.sending_enabled
    assert "total_output_billing_bound" in observation.unresolved
    assert observation.manual_review_required == (role == "judge")


@pytest.mark.parametrize("generation_overrides,body_overrides,expected_match", [
    pytest.param({}, {}, True, id="default-single-candidate"),
    pytest.param({"candidateCount": 1}, {}, True, id="explicit-single-candidate"),
    pytest.param({"candidate_count": 1}, {}, True, id="proto-single-candidate"),
    pytest.param({"candidateCount": 2}, {}, False, id="multiple-candidates"),
    pytest.param({"candidate_count": 2}, {}, False, id="proto-multiple-candidates"),
    pytest.param({"candidateCount": 0}, {}, False, id="zero-candidates"),
    pytest.param({"candidateCount": True}, {}, False, id="boolean-candidate-count"),
    pytest.param({"candidateCount": "2"}, {}, False, id="string-candidate-count"),
    pytest.param({"candidateCount": 1, "candidate_count": 2}, {}, False, id="conflicting-candidate-count"),
    pytest.param({"max_output_tokens": 16384}, {}, False, id="conflicting-output-limit"),
    pytest.param({"thinking_config": {"thinking_budget": 0}}, {}, False, id="conflicting-thinking-config"),
    pytest.param({"responseModalities": ["TEXT"], "response_modalities": ["AUDIO"]}, {}, False, id="conflicting-response-modality"),
    pytest.param({"responseModalities": ["TEXT"]}, {}, True, id="native-text-modality"),
    pytest.param({"response_modalities": ["TEXT"]}, {}, True, id="proto-text-modality"),
    pytest.param({}, {"generation_config": {"candidate_count": 2}}, False, id="conflicting-generation-config"),
])
def test_google_native_settings_require_single_text_candidate_without_conflicting_fields(
        prepared_root, generation_overrides, body_overrides, expected_match):
    registry = builtin_registry()
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], protocols=["google"], mode="prepare-live"),
                            root=prepared_root, registry=registry)
    gate = LiveGate(prepared_root, registry)
    ledger = prepared_root / "ledger.json"
    original_ledger = ledger.read_bytes()

    for route in prepared.eligibility:
        if route.protocol != "google":
            continue
        request = httpx2.Request("POST", f"{route.endpoint}/v1beta/models/{route.model}:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "Offline."}]}], "generationConfig": {
                "thinkingConfig": route.thinking, "maxOutputTokens": route.total_output_limit, **generation_overrides,
            }, **body_overrides})
        observation = gate.inspect_native_request(request, protocol="google", role=route.role,
            config=route.configuration, now=datetime(2026, 11, 23, tzinfo=timezone.utc))
        assert observation.route_current and not observation.sending_enabled, route.model
        assert observation.native_settings_match is expected_match, route.model
        assert ("native_settings_mismatch" in observation.unresolved) is (not expected_match), route.model
        assert "total_output_billing_bound" in observation.unresolved, route.model

    assert ledger.read_bytes() == original_ledger and json.loads(original_ledger)["attempts"] == []



def test_frozen_provider_configuration_changes_invalidate_native_eligibility(prepared_root: Path) -> None:
    import httpx2
    from myrk.provider import ProviderConfig
    from tests.support.agent_acceptance import LiveGate

    prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                 root=prepared_root, registry=builtin_registry())
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions", json={
        "model": "deepseek-flash", "reasoning_effort": "high", "max_tokens": 4096, "messages": []})
    observation = LiveGate(prepared_root, builtin_registry()).inspect_native_request(request,
        protocol="deepseek", role="product", config=ProviderConfig(output_token_reserve=8192),
        now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert not observation.route_current
    assert "configuration_changed" in observation.unresolved


@pytest.mark.parametrize("moment,current", [
    (datetime(2026, 11, 29, 15, 58, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 29, 15, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 30, tzinfo=timezone.utc), False),
])
def test_gemini_route_expires_at_the_frozen_taipei_deadline(prepared_root, moment, current):
    import httpx2
    from tests.support.agent_acceptance import LiveGate

    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], protocols=["google"], mode="prepare-live"),
                            root=prepared_root, registry=builtin_registry())
    route = prepared.eligibility[1]
    request = httpx2.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [], "generationConfig": {"thinkingConfig": {"thinking_budget": 2048}, "maxOutputTokens": 4096}})
    observation = LiveGate(prepared_root, builtin_registry()).inspect_native_request(request,
        protocol="google", role="product", config=route.configuration, now=moment)
    assert observation.route_current == current
    assert ("route_expired" in observation.unresolved) == (not current)
    assert not observation.sending_enabled


def test_paid_google_features_are_not_covered_by_the_text_only_route(prepared_root):
    import httpx2
    from tests.support.agent_acceptance import LiveGate

    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], protocols=["google"], mode="prepare-live"),
                            root=prepared_root, registry=builtin_registry())
    request = httpx2.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [], "tools": [{"googleSearch": {}}], "generationConfig": {
            "thinkingConfig": {"thinking_budget": 2048}, "maxOutputTokens": 4096}})
    observation = LiveGate(prepared_root, builtin_registry()).inspect_native_request(request,
        protocol="google", role="product", config=prepared.eligibility[1].configuration,
        now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert not observation.native_settings_match
    assert "non_text_or_paid_feature" in observation.unresolved


def test_function_tool_field_names_do_not_enable_paid_native_features(prepared_root):
    import httpx2
    from tests.support.agent_acceptance import LiveGate

    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], protocols=["google"], mode="prepare-live"),
                            root=prepared_root, registry=builtin_registry())
    request = httpx2.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "Summarize the image_url column name."}]}],
              "tools": [{"functionDeclarations": [{"name": "read_text", "parameters": {"type": "object",
                       "properties": {"image_url": {"type": "string"}}}}]}], "generationConfig": {
            "thinkingConfig": {"thinking_budget": 2048}, "maxOutputTokens": 4096}})
    observation = LiveGate(prepared_root, builtin_registry()).inspect_native_request(request,
        protocol="google", role="product", config=prepared.eligibility[1].configuration,
        now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert observation.native_settings_match


@pytest.mark.parametrize("field,replacement", [("price_input_per_million", "0.01"), ("actual_model", "auto-replacement"),
                                                ("expires_at", "2030-01-01T00:00:00Z")])
def test_route_price_mapping_or_validity_changes_break_the_prepared_binding(prepared_root, field, replacement):
    import json
    from tests.support.agent_acceptance import SharedControlError, verify_prepared

    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                 root=prepared_root, registry=builtin_registry())
    assert verify_prepared(prepared_root, registry=builtin_registry()).binding == prepared.binding
    path = prepared_root / "prepared.json"
    ledger = prepared_root / "ledger.json"
    ledger_before = ledger.read_bytes()
    data = json.loads(path.read_bytes())
    data["eligibility"][1][field] = replacement
    path.write_text(json.dumps(data), encoding="utf-8")
    damaged = path.read_bytes()
    with pytest.raises(SharedControlError, match=r"^Genesis prepared original changed; no candidate can replace it\.$"):
        verify_prepared(prepared_root, registry=builtin_registry())
    assert path.read_bytes() == damaged and ledger.read_bytes() == ledger_before
    assert json.loads(ledger_before)["attempts"] == []


def test_unqualified_real_run_stops_before_output_profile_or_connection_access(prepared_root, tmp_path):
    import asyncio
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, SharedControlError, run_plan

    registry = builtin_registry()
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=prepared_root, registry=registry)
    profile = tmp_path / "account-must-remain-unread.json"
    output = tmp_path / "not-started"
    grant = LiveAuthorization(authorization_id="offline-refusal-example", binding=prepared.binding, total_limit=1,
        case_limits={"ordinary.question": 1}, connection_profile=profile)
    with pytest.raises(SharedControlError, match="unqualified"):
        asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], mode="live"), root=output,
            registry=registry, live_gate=LiveGate(prepared_root, registry, grant)))
    assert not output.exists() and not profile.exists()
