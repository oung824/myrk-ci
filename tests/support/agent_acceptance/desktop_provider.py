"""Native provider settings and the managed formal desktop handoff."""

import json

import httpx2

from myrk.conversation import Conversation, Task
from myrk.model_controls import ModelSelection, fetch_chat_models, validate_selection
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, Protocol
from tests.support.agent_acceptance.replay import Reply


async def settings(context: CaseContext) -> CaseOutcome:
    config = ProviderConfig(name="Synthetic settings", protocol=context.protocol,
        base_url={"deepseek": "https://api.deepseek.com", "openai-chat": "https://api.openai.com/v1",
                  "google": "https://generativelanguage.googleapis.com"}[context.protocol],
        model={"deepseek": "deepseek-flash", "openai-chat": "gpt-5.4", "google": "gemini-2.5-flash"}[context.protocol], input_token_limit_override=100000)
    catalogs = []

    def catalog(request: httpx2.Request) -> httpx2.Response:
        body = {"models": [{"name": "models/gemini-2.5-pro", "displayName": "Gemini Pro", "supportedGenerationMethods": ["generateContent"]}]} if context.protocol == "google" else {
            "object": "list", "data": [{"id": "deepseek-v4-pro" if context.protocol == "deepseek" else "gpt-5.5", "object": "model", "created": 1, "owned_by": "synthetic"}]}
        response = httpx2.Response(200, json=body)
        catalogs.append({"method": request.method, "url": str(request.url), "response_utf8": response.content.decode()})
        return response

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(catalog), trust_env=False) as client:
        models = await fetch_chat_models("synthetic-settings-only", config, http_client=client)
    selection = validate_selection(ModelSelection(provider_config=config, reasoning="high"))
    conversation = Conversation(context.root / "conversation.json")
    conversation.set_model_selection(selection)
    selection = validate_selection(ModelSelection(provider_config=config.model_copy(update={"model": models[-1].id}), reasoning="high"))
    conversation.set_model_selection(selection)
    restored = Conversation.open(conversation.path)
    session = context.session([Reply(text="Settings retained.")], config=selection.provider_config)
    task = Task(goal="Confirm selected settings.", scope=None, provider_config=selection.provider_config, reasoning=selection.reasoning)
    async with session.model() as model:
        turn = await restored.ask(task.goal, document=None, task=task, model=model)
    exchange = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in turn.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    catalog_path = context.root / "catalog.json"
    catalog_path.write_text(json.dumps(catalogs, indent=2) + "\n", encoding="utf-8")
    body = json.loads(session.exchanges[0]["request_utf8"]) if session.exchanges else {}
    native_reasoning = (body.get("reasoning_effort") == "high" if context.protocol != "google" else
                        body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinking_budget") == 24576)
    passed = (turn.status == "completed" and restored.model_selection == selection and len(models) >= 2
              and selection.provider_config.model != config.model and native_reasoning and turn.request_attempts[0].observation is not None)
    return CaseOutcome("passed" if passed else "failed", {"selected": selection.model_dump(mode="json"),
        "models": [item.id for item in models], "native_reasoning": native_reasoning, "error": turn.error, "previous_model": config.model,
        "real_http_attempts": 0, "normal_profile_accesses": 0, "catalog_mock_attempts": len(catalogs)},
        {"conversation": restored.path, "exchange": exchange, "catalog": catalog_path})


async def protocol_failure(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance.ordinary import request_failure

    translated = {"provider.retry": "ordinary.retry", "provider.interrupted": "ordinary.interrupted",
                  "provider.missing-usage": "ordinary.missing-usage"}[context.case_id]
    outcome = await request_failure(context, scenario=translated)
    outcome.facts.update(real_http_attempts=0, normal_profile_accesses=0)
    return outcome


async def recipient_switch(context: CaseContext) -> CaseOutcome:
    from dataclasses import replace
    from tests.support.agent_acceptance.formal import provider_config

    source_protocol: Protocol = "openai-chat" if context.protocol == "deepseek" else "deepseek"
    source = provider_config(source_protocol)
    session = replace(context, protocol=source_protocol).session([Reply(status=400)], config=source)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Keep original task while changing only the explicitly approved receiver.", scope=None, provider_config=source)
    async with session.model() as model:
        failed = await conversation.ask(task.goal, document=None, task=task, model=model)
    original_attempts = [item.model_dump(mode="json") for item in failed.request_attempts]
    session.save(context.root / "original-exchange.json", attempts=original_attempts,
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    restored = Conversation.open(conversation.path)
    await restored.verify_recovery_async(task.id)
    target = provider_config(context.protocol)
    before_preview = len(session.exchanges)
    rejected = restored.preview_provider_change(task.id, target)
    after_preview = len(session.exchanges)
    assert restored.decide_provider_change(rejected.id, False) is None
    after_decline = len(session.exchanges)
    preview = restored.preview_provider_change(task.id, target)
    before = len(session.exchanges)
    preview_sends = after_preview - before_preview + before - after_decline
    decline_sends = after_decline - after_preview
    approved = restored.decide_provider_change(preview.id, True)
    assert approved is not None
    resumed = context.session([Reply(text="Receiver changed once.")], config=target)
    async with resumed.model() as model:
        finished = await restored.resume_task(task.id, model=model)
    exchange = resumed.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in finished.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    preview_path = context.root / "receiver-preview.json"
    preview_path.write_text(preview.model_dump_json(indent=2) + "\n", encoding="utf-8")
    saved = Conversation.open(restored.path)
    original_task, final_task = saved.turns[0].task, saved.turns[-1].task
    assert original_task is not None and final_task is not None
    original_preserved = [item.model_dump(mode="json") for item in saved.turns[0].request_attempts] == original_attempts
    passed = (failed.service_failure and preview.compatible and finished.status == "completed" and approved.id == final_task.id == task.id
        and final_task.requests == final_task.total_requests == 2 and original_task.requests == 1 and original_preserved
        and final_task.request_limit == task.request_limit and final_task.provider_config == target
        and final_task.goal == task.goal and final_task.scope == task.scope
        and before == 1 and preview_sends == decline_sends == 0
        and len(session.sdk_inputs) == len(resumed.sdk_inputs) == len(resumed.exchanges) == 1)
    return CaseOutcome("passed" if passed else "failed", {"source": source.model_dump(mode="json"), "target": target.model_dump(mode="json"),
        "original_task_id": task.id, "resumed_task_id": final_task.id, "product_attempts": final_task.requests,
        "original_saved_requests": original_task.requests, "request_cap": final_task.request_limit,
        "original_request_attempts_preserved": original_preserved, "decline_sends": decline_sends, "preview_sends": preview_sends, "real_http_attempts": 0, "normal_profile_accesses": 0,
        "error": finished.error}, {"conversation": restored.path, "exchange": exchange, "preview": preview_path,
        "original_exchange": context.root / "original-exchange.json"})


def register(registry: CaseRegistry) -> None:
    from tests.support.agent_acceptance.formal import bindings, handoff, lifecycle, prepare
    from tests.support.agent_acceptance.connection_desktop_lifecycle import material_bindings as connection_materials, basics

    protocols: tuple[Protocol, ...] = ("deepseek", "openai-chat", "google")
    registry.register(AcceptanceCase("provider.settings", "providers", protocols, settings))
    registry.register(AcceptanceCase("provider.recipient-switch", "providers", protocols, recipient_switch))
    for name in ("provider.retry", "provider.interrupted", "provider.missing-usage"):
        registry.register(AcceptanceCase(name, "provider-failures", protocols, protocol_failure))
    registry.register(AcceptanceCase("desktop.handoff", "desktop", protocols, handoff,
        bindings=(*bindings(), *connection_materials()), prepare=prepare, formal_live=True, owner_ticket="16", frozen_material_input=True))
    registry.register(AcceptanceCase("desktop.connection-basics", "desktop-lifecycle", ("deepseek",), basics, bindings=(*bindings(), *connection_materials()), owner_ticket="16"))
    registry.register(AcceptanceCase("desktop.lifecycle", "desktop-lifecycle", ("deepseek",), lifecycle,
        bindings=bindings(), owner_ticket="16"))
