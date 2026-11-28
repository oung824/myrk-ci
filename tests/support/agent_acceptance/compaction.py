"""Executable compaction scenarios through the public Conversation interface."""

import json
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from myrk.conversation import Conversation, ConversationRecord, Task, Update
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.compaction_review import write_review
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, Protocol
from tests.support.agent_acceptance.replay import AcceptanceSession, Reply


PROTOCOLS: tuple[Protocol, ...] = ("deepseek", "openai-chat", "google")
SUMMARY = "Recorded goal: verify 937 read only. Original sources remain available; verification is pending, not completed. Do not write or contact other services."
MATERIALS = Path(__file__).resolve().parents[2] / "acceptance" / "materials" / "compaction"


def history(goal: str = "Verify 937 from the original source; never invent evidence.") -> list[ModelMessage]:
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(goal)])]
    for number in range(12):
        messages.extend([ModelResponse(parts=[TextPart(f"Evidence {number}: " + "historical information " * 180)]),
                         ModelRequest(parts=[UserPromptPart(f"Continue evidence {number}.")])])
    return messages


def configured(context: CaseContext, *, window: int = 15000) -> ProviderConfig:
    return ProviderConfig(name="Compaction acceptance synthetic", protocol=context.protocol,
        base_url="https://generativelanguage.googleapis.com" if context.protocol == "google" else
            "https://api.openai.com/v1" if context.protocol == "openai-chat" else "https://api.deepseek.com",
        model="gemini-2.5-flash" if context.protocol == "google" else "gpt-4.1" if context.protocol == "openai-chat" else "deepseek-flash",
        context_window_override=window, output_token_reserve=1024, context_safety_margin=512)


def open_history(context: CaseContext, source: list[ModelMessage]) -> Conversation:
    path = context.root / "conversation.json"
    path.write_text(ConversationRecord(version=9, messages=source, turns=[]).model_dump_json(), encoding="utf-8")
    return Conversation.open(path)


def finish(context: CaseContext, conversation: Conversation, session: AcceptanceSession, source: list[ModelMessage],
           *, passed: bool, facts: dict[str, Any] | None = None) -> CaseOutcome:
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
    attempts = [attempt for turn in record.turns for attempt in turn.request_attempts]
    unchanged = ModelMessagesTypeAdapter.dump_json(record.messages[:len(source)]) == ModelMessagesTypeAdapter.dump_json(source)
    last = record.turns[-1]
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=last.task.id if last.task else "")
    review = write_review(context, record, session)
    return CaseOutcome("passed" if passed and unchanged else "failed", {
        "turn_status": last.status, "error": last.error, "originals_unchanged": unchanged,
        "attempt_purposes": [item.purpose for item in attempts], "product_attempts": len(attempts),
        "mock_http_attempts": len(session.exchanges),
        "prepared": [{"input_tokens": item.input_tokens, "input_budget_tokens": item.input_budget_tokens,
                      "accepted": item.accepted, "request_attempt": item.request_attempt,
                      "measurement_source": item.measurement_source} for item in record.prepared_contexts], **(facts or {})},
        {"conversation": conversation.path, "exchange": evidence, "review": review})


async def compact_and_reopen(context: CaseContext) -> CaseOutcome:
    source = history()
    conversation = open_history(context, source)
    session = context.session([Reply(text=SUMMARY), Reply(text="Verification remains pending."), Reply(text="Read-only continuation completed.")], config=configured(context))
    task = Task(goal="Verify the original source read only.", scope=None, provider_config=session.config)
    async with session.model() as model:
        first = await conversation.ask("Keep the latest read-only correction.", document=None, task=task, model=model)
        reopened = Conversation.open(conversation.path)
        opened_without_send = len(session.exchanges) == 2
        again = await reopened.ask("Continue after reopening the saved summary.", document=None, task=reopened.turns[-1].task, model=model)
    passed = first.status == again.status == "completed" and len(first.compactions) == 1 and first.compactions[0].status == "completed" and not again.compactions
    return finish(context, reopened, session, source, passed=passed and opened_without_send,
                  facts={"open_without_send": opened_without_send, "turns_used": task.turns_used})


async def summary_boundary(context: CaseContext) -> CaseOutcome:
    name = context.case_id.removeprefix("compaction.")
    scripts = {
        "long": [Reply(text="Factual record: verification remains pending. " + "资料" * 2800), Reply(status=400), Reply(text="Resumed using the saved long summary.")],
        "truncated": [Reply(text="Incomplete summary.", finish_reason="length")],
        "service-failure": [Reply(status=503)] * 3,
        "interrupted": [Reply(text="Partially received summary.", interrupt=True)] * 3,
        "missing-usage": [Reply(text=SUMMARY, usage=False), Reply(text="Unknown input usage.", usage=False)],
        "window-recovery": [Reply(window_error=True), Reply(text=SUMMARY), Reply(text="Recovered in the original work step.")],
    }
    session = context.session(scripts[name], config=configured(context, window=40000 if name == "window-recovery" else 15000))
    source = history()
    conversation = open_history(context, source)
    task = Task(goal="Verify originals read only.", scope=None, provider_config=session.config)
    async with session.model() as model:
        first = await conversation.ask("Preserve the original constraints.", document=None, task=task, model=model)
        if name == "long":
            conversation = Conversation.open(conversation.path)
            opened_count = len(session.exchanges)
            turn = await conversation.resume_task(task.id, model=model)
            passed = (first.service_failure and len(first.compactions) == 1 and first.compactions[0].status == "completed"
                and first.compactions[0].target_reached is False and turn.status == "completed" and not turn.compactions and opened_count == 2)
        else:
            turn = first
            purposes = [item.purpose for item in turn.request_attempts]
            if name == "truncated":
                expected_error = "Gemini 回应未完整结束（结束原因：MAX_TOKENS），已保留收到的内容和用量；没有自动续接。" if context.protocol == "google" else "摘要被截断或被服务拒绝，未替换有效记录。"
                passed = (turn.status == "failed" and len(turn.compactions) == 1 and turn.compactions[0].status == "failed"
                    and purposes == ["compaction"] and turn.error == expected_error)
            elif name in {"service-failure", "interrupted"}:
                passed = (turn.status == "failed" and len(turn.compactions) == 1 and turn.compactions[0].status == "failed"
                    and len(purposes) == len(session.exchanges) == 3 and set(purposes) == {"compaction"}
                    and all(item.status == "failed" for item in turn.request_attempts))
            elif name == "window-recovery":
                passed = turn.status == "completed" and purposes == ["work", "compaction", "work"] and task.turns_used == 1
            else:
                passed = turn.status == "completed" and turn.input_tokens is None
    observation = turn.request_attempts[-1].observation if turn.request_attempts else None
    return finish(context, conversation, session, source, passed=passed, facts={"turns_used": task.turns_used,
        "input_tokens": turn.input_tokens, "cache_read_tokens": observation.cache_read_tokens if observation else None,
        "cache_write_tokens": observation.cache_write_tokens if observation else None,
        "cache_benefit": "not_measured"})


async def semantic_material(context: CaseContext) -> CaseOutcome:
    name = context.case_id.removeprefix("compaction.semantic.")
    material = json.loads((MATERIALS / "inputs.json").read_text(encoding="utf-8-sig"))[name]
    document = context.root / "fixed-material.md"
    content = material.get("material", "Fixed receipt status: unresolved; reference: unresolved.")
    if name == "explicit-adoption":
        content = material["receipt_status"]
    document.write_text(content + "\n", encoding="utf-8")
    reference = document
    if name == "explicit-adoption":
        reference = context.root / "fixed-reference.txt"
        reference.write_text(material["reference_status"] + "\n", encoding="utf-8")
    source: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(material["goal"])])]
    source.extend([ModelResponse(parts=[ToolCallPart("read_authorized_text", {}, "initial-material")]),
                   ModelRequest(parts=[ToolReturnPart("read_authorized_text", material.get("material", content), "initial-material")])])
    if "assistant_proposal" in material:
        source.append(ModelResponse(parts=[TextPart(material["assistant_proposal"])]))
    if "previous_summary" in material:
        source.append(ModelResponse(parts=[TextPart("Previous summary (historical material): " + material["previous_summary"])]))
    for field in ("later_user", "visible_correction"):
        if field in material:
            source.append(ModelRequest(parts=[UserPromptPart(material[field])]))
    if "history" in material:
        source.extend([ModelResponse(parts=[TextPart(material["history"][0])]),
                       ModelRequest(parts=[UserPromptPart(material["history"][1])]),
                       ModelRequest(parts=[UserPromptPart(material["history"][2])])])
    source.extend(history(material["goal"])[1:])
    conversation = open_history(context, source)
    summary = "Factual record of the saved user request: " + material["goal"] + (" Later user adoption: " + material["later_user"] if "later_user" in material else "")
    final_reply = "Receipt status unresolved matches reference status unresolved." if name == "explicit-adoption" else "Read-only continuation reported; no new authority inferred."
    replies = [Reply(text=summary),
               Reply(tool="read_authorized_text"), Reply(text="Reported the selected material within the saved request."),
               Reply(tool="read_authorized_text"), Reply(text=final_reply)]
    session = context.session(replies, config=configured(context))
    task = Task(goal=material["goal"], scope=None, document=str(document), provider_config=session.config)
    async with session.model() as model:
        first = await conversation.ask("Continue the saved user request within its original scope.", document=document, task=task, model=model)
        conversation = Conversation.open(conversation.path)
        second = await conversation.ask("Continue reporting within the original read-only scope.", document=reference, task=conversation.turns[-1].task, model=model)
    criteria = json.loads((MATERIALS / "criteria.json").read_text(encoding="utf-8-sig"))[name]
    criteria_path = context.root / "criteria.json"
    criteria_path.write_text(json.dumps(criteria, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actual = json.dumps(session.sdk_inputs, ensure_ascii=False)
    leaked = any(key in actual for key in ("expected_user_unfinished", "expected_material_pending", "critical_failures"))
    read_after = len(session.sdk_inputs) == 5 and content in json.dumps(session.sdk_inputs[4], ensure_ascii=False)
    compared = (material["receipt_status"] in json.dumps(session.sdk_inputs[4], ensure_ascii=False)
        and material["reference_status"] in json.dumps(session.sdk_inputs[4], ensure_ascii=False)
        and material["later_user"] in json.dumps(session.sdk_inputs[3], ensure_ascii=False)) if name == "explicit-adoption" else None
    passed = first.status == second.status == "completed" and len(first.compactions) == 1 and not second.compactions and read_after and not leaked
    outcome = finish(context, conversation, session, source, passed=passed,
        facts={"material_read_after_compaction": read_after, "criteria_absent_from_model_input": not leaked,
               "comparison_operands_seen": compared})
    outcome.evidence.update(material=document, criteria=criteria_path)
    if name == "explicit-adoption":
        outcome.evidence["reference"] = reference
    return outcome


async def repeated_compaction(context: CaseContext) -> CaseOutcome:
    source = history()
    conversation = open_history(context, source)
    second_summary = "Updated factual record: previous read-only constraints still apply; original verification remains pending."
    session = context.session([Reply(text=SUMMARY), Reply(text="First work report."), Reply(text=second_summary),
                               Reply(text="Second work report."), Reply(text="Saved continuation report.")], config=configured(context))
    task = Task(goal="Verify original evidence read only.", scope=None, provider_config=session.config)
    async with session.model() as model:
        first = await conversation.ask("Preserve the recorded constraints.", document=None, task=task, model=model)
        checkpoint = context.root / "after-first-summary.json"
        checkpoint.write_bytes(conversation.path.read_bytes())
        saved = ConversationRecord.model_validate_json(checkpoint.read_bytes())
        for number in range(8):
            saved.messages.extend([ModelResponse(parts=[TextPart(f"Further evidence {number}: " + "additional historical fact " * 170)]),
                                   ModelRequest(parts=[UserPromptPart(f"Continue reporting fixed evidence {number}.")])])
        conversation.path.write_text(saved.model_dump_json(), encoding="utf-8")
        conversation = Conversation.open(conversation.path)
        second = await conversation.ask("Keep the latest correction and continue.", document=None, task=conversation.turns[-1].task, model=model)
        conversation = Conversation.open(conversation.path)
        final = await conversation.ask("Continue using the second saved summary.", document=None, task=conversation.turns[-1].task, model=model)
    old = first.compactions[0] if first.compactions else None
    latest = second.compactions[0] if second.compactions else None
    inherited = bool(old and latest and {ref.key for ref in old.covered} <= {ref.key for ref in latest.covered} and latest.replaced_id == old.id)
    readable = bool(latest and "Evidence 0:" in conversation.read_compaction_source(latest.id, 1, 0))
    passed = (first.status == second.status == final.status == "completed" and inherited and readable and not final.compactions
        and SUMMARY in json.dumps(session.sdk_inputs[2], ensure_ascii=False))
    outcome = finish(context, conversation, session, source, passed=passed, facts={"inherited_coverage": inherited, "original_source_readable": readable})
    outcome.evidence["first_checkpoint"] = checkpoint
    return outcome


async def early_no_history(context: CaseContext) -> CaseOutcome:
    document = context.root / "early-material.md"
    material = "回执状态仍未核实。" * 650
    document.write_text(material, encoding="utf-8")
    config = configured(context).model_copy(update={"input_token_limit_override": 11240, "compaction_trigger": 0.7})
    session = context.session([Reply(tool="read_authorized_text"), Reply(text="This request should remain possible below the hard input budget.")], config=config)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read the selected file and report its status only.", document=str(document), scope=None, provider_config=config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=document, task=task, model=model)
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
    measurement = turn.compactions[0] if turn.compactions else None
    before_tokens = measurement.before_tokens if measurement else record.prepared_contexts[-1].input_tokens
    hard_budget = measurement.input_budget_tokens if measurement else record.prepared_contexts[-1].input_budget_tokens
    soft_exceeded_but_hard_fits = (before_tokens is not None and hard_budget is not None
        and config.compaction_trigger * hard_budget < before_tokens <= hard_budget)
    layout = record.request_layouts["main"]
    original_returns = [part.content for message in record.messages for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "read_authorized_text"]
    material_preserved = document.read_text(encoding="utf-8") == material and original_returns == [material]
    material_seen_after_read = len(session.sdk_inputs) == 2 and material in json.dumps(session.sdk_inputs[1], ensure_ascii=False)
    facts = {"turn_status": turn.status, "error": turn.error,
        "attempt_purposes": [item.purpose for item in turn.request_attempts], "mock_http_attempts": len(session.exchanges),
        "compaction_statuses": [item.status for item in turn.compactions],
        "summary_attempts": [number for item in turn.compactions for number in item.request_numbers],
        "soft_exceeded_but_hard_fits": soft_exceeded_but_hard_fits,
        "before_tokens": before_tokens, "hard_budget": hard_budget,
        "initial_input_accepted": bool(record.prepared_contexts and record.prepared_contexts[0].accepted),
        "layout_version": layout.version, "active_summary": layout.compaction is not None,
        "capacity_layout_changes": [item.model_dump(mode="json") for item in layout.changes if "capacity_compression" in item.reasons],
        "material_preserved": material_preserved, "material_seen_after_read": material_seen_after_read}
    passed = (turn.status == "completed" and len(session.exchanges) == 2 and not turn.compactions
        and [item.purpose for item in turn.request_attempts] == ["work", "work"] and layout.compaction is None
        and layout.version == 1 and soft_exceeded_but_hard_fits and material_preserved and material_seen_after_read)
    outcome = finish(context, conversation, session, [], passed=passed, facts=facts)
    outcome.evidence["material"] = document
    return outcome


class LocalInterruption(BaseException):
    """Planned process-local interruption after durable preparation."""


async def window_reopen(context: CaseContext) -> CaseOutcome:
    source = history()
    conversation = open_history(context, source)
    session = context.session([Reply(window_error=True), Reply(text=SUMMARY), Reply(window_error=True)], config=configured(context, window=40000))
    task = Task(goal="Verify original evidence without repeating completed work.", scope=None, provider_config=session.config)
    work_preparations = 0

    def interrupt(update: Update) -> None:
        nonlocal work_preparations
        if update.kind == "status" and update.text == "正在等待回答服务":
            work_preparations += 1
            if work_preparations == 2:
                raise LocalInterruption()

    async with session.model() as model:
        try:
            await conversation.ask("Continue the original task.", document=None, task=task, model=model, on_update=interrupt)
        except LocalInterruption:
            pass
        before = context.root / "before-reopen.json"
        before.write_bytes(conversation.path.read_bytes())
        pending = conversation.turns[-1].task
        assert pending is not None and pending.work_step is not None
        original_step, turns_before_reopen = pending.work_step.id, pending.turns_used
        previous_turn = conversation.turns[-1].model_dump(mode="json")
        conversation = Conversation.open(conversation.path)
        turn = await conversation.resume_task(task.id, model=model)
    attempts = [item for previous in conversation.turns for item in previous.request_attempts]
    work = [item for item in attempts if item.purpose == "work"]
    recovered = [item.decision for item in attempts if item.decision.startswith("服务确认输入窗口超限")]
    preserved = all(item.step_id == original_step for item in work)
    previous_unchanged = conversation.turns[-2].model_dump(mode="json") == previous_turn
    resumed = turn.task
    assert resumed is not None and resumed.work_step is not None
    facts = {"recovery_decisions": recovered,
        "compaction_statuses": [item.status for previous in conversation.turns for item in previous.compactions],
        "turns_used": resumed.turns_used, "turns_before_reopen": turns_before_reopen,
        "work_preparations_before_reopen": work_preparations, "post_reopen_status": turn.status,
        "step_id": original_step, "request_step_ids": [item.step_id for item in work],
        "capacity_recovery_used": resumed.work_step.capacity_recovery_used,
        "previous_turn_unchanged": previous_unchanged,
        "previous_task_requests": conversation.turns[-2].task.requests if conversation.turns[-2].task else None,
        "current_task_requests": resumed.requests,
        "logical_step_identity_across_reopen": "preserved" if preserved else "changed",
        "failure_classification": "deterministic_capacity_recovery_contract"}
    passed = (turn.status == "failed" and preserved and previous_unchanged and resumed.work_step.capacity_recovery_used
        and resumed.turns_used == turns_before_reopen == 1 and len(recovered) == 1
        and facts["compaction_statuses"] == ["completed"] and len(session.exchanges) == 3
        and len(work) == 3 and work_preparations == 2)
    outcome = finish(context, conversation, session, source, passed=passed, facts=facts)
    outcome.evidence["before_reopen"] = before
    return outcome


def classify_known_failure(outcome: CaseOutcome, symptom: str) -> None:
    """A changed symptom is a tool failure requiring review, never an automatic pass."""
    facts = outcome.facts
    if symptom == "window-reopen":
        matched = (facts["turn_status"] == "limited"
            and facts["error"] == "当前有效摘要与保护区仍超过资料容量，且没有新增可压缩原文；已暂停，未反复摘要。请调整容量或缩小输入后继续。"
            and facts["attempt_purposes"] == ["work", "compaction", "work", "work"]
            and facts["mock_http_attempts"] == 3 and facts["compaction_statuses"] == ["completed"]
            and facts["recovery_decisions"] == ["服务确认输入窗口超限；在原办事步骤整理一次后重试，不重放已完成动作。"] * 2
            and facts["work_preparations_before_reopen"] == 2)
    else:
        raise ValueError("Unknown compaction symptom.")
    matched = matched and facts["originals_unchanged"] is True
    outcome.tool_status = "passed" if matched else "failed"
    outcome.product_status = "inconclusive" if matched and symptom == "window-reopen" else "failed"
    outcome.facts.update(known_failure_matched=matched, fix_issue=".scratch/compaction-acceptance-defects-20261113/issues/02-window-reopen.md")


def register(registry: CaseRegistry) -> None:
    from tests.support.agent_acceptance.compaction_continuity import summary_save_obstruction
    registry.register(AcceptanceCase("compaction.summary-save-obstruction","compaction",("deepseek",),summary_save_obstruction,owner_ticket="14"))
    registry.register(AcceptanceCase("compaction.short", "compaction", PROTOCOLS, compact_and_reopen,
        bindings=(Path(__file__).with_name("compaction_review.py"),)))
    for name in ("long", "truncated", "service-failure", "interrupted", "missing-usage", "window-recovery"):
        registry.register(AcceptanceCase(f"compaction.{name}", "compaction", PROTOCOLS, summary_boundary,
            bindings=(Path(__file__).with_name("compaction_review.py"),)))
    for name in json.loads((MATERIALS / "inputs.json").read_text(encoding="utf-8-sig")):
        registry.register(AcceptanceCase(f"compaction.semantic.{name}", "compaction-semantics", PROTOCOLS, semantic_material,
            bindings=(MATERIALS / "inputs.json", MATERIALS / "criteria.json", Path(__file__).with_name("compaction_review.py"))))
    registry.register(AcceptanceCase("compaction.early-no-history", "compaction", ("deepseek",), early_no_history))
    registry.register(AcceptanceCase("compaction.window-reopen", "compaction", ("deepseek",), window_reopen))
    registry.register(AcceptanceCase("compaction.repeated", "compaction", PROTOCOLS, repeated_compaction,
        bindings=(Path(__file__).with_name("compaction_review.py"),)))
