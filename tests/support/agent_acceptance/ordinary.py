"""Ordinary conversation, selected attachment, and bounded input cases."""

import json
import re
from hashlib import sha256

from myrk.conversation import Conversation, Task
from myrk.provider import ProviderConfig
from myrk.permissions import AccessSettings, ApprovalSettings
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.question_contracts import FactStatus, question_fact, threshold_fact


async def question(context: CaseContext) -> CaseOutcome:
    session = context.session([Reply(text="The result is 42.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="What is six times seven? State the result clearly.", scope=None, provider_config=session.config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model)
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in turn.request_attempts],
                            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    fact, reason = question_fact(turn.answer)
    completed = turn.status == "completed"
    return CaseOutcome("passed" if completed and fact == "passed" else "inconclusive" if completed and fact == "unknown" else "failed",
        {"answer": turn.answer, "turn_status": turn.status, "error": turn.error, "product_attempts": turn.requests,
            "mock_http_attempts": len(session.exchanges), "goal_program_contract_passed": completed,
            "fact_contract_status": fact, "fact_contract_reason": reason},
        {"conversation": conversation.path, "exchange": evidence},
        target_status="passed" if completed and fact == "passed" else "pending_review" if completed and fact == "unknown" else "failed",
        deterministic_status="not_measured" if fact == "unknown" else fact)



async def attachment(context: CaseContext) -> CaseOutcome:
    document = context.root / "purchase.md"
    document.write_text("Purchase threshold: 800.\n", encoding="utf-8")
    original = document.read_bytes()
    session = context.session([Reply(tool="read_authorized_text"), Reply(text="Purchase threshold: 800. [purchase.md:1]")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read the selected purchase rule, state the threshold, and cite [purchase.md:1].", scope=None,
                document=str(document), provider_config=session.config,
                approval=ApprovalSettings(mode="full_access", access=AccessSettings(read_only=True)))
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=document, task=task, model=model)
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in turn.request_attempts],
                            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    source_inputs = [item["sdk_input_id"] for item in session.sdk_inputs[1:] if "Purchase threshold: 800." in json.dumps(item)]
    unchanged = document.read_bytes() == original
    fact, reason = threshold_fact(turn.answer)
    program_passed = turn.status == "completed" and bool(source_inputs) and unchanged and not turn.actions
    return CaseOutcome("passed" if program_passed and fact == "passed" else "inconclusive" if program_passed and fact == "unknown" else "failed", {"answer": turn.answer, "turn_status": turn.status,
        "error": turn.error, "product_attempts": turn.requests, "mock_http_attempts": len(session.exchanges),
        "goal_program_contract_passed": bool(program_passed), "fact_contract_status": fact, "fact_contract_reason": reason,
        "source": {"path": str(document), "sha256": sha256(original).hexdigest(), "unchanged": unchanged,
            "citation": "purchase.md:1", "native_input_ids": source_inputs}},
        {"conversation": conversation.path, "exchange": evidence, "attachment": document},
        target_status="passed" if program_passed and fact == "passed" else "pending_review" if program_passed and fact == "unknown" else "failed",
        deterministic_status="not_measured" if fact == "unknown" else fact)


async def encoded_long(context: CaseContext) -> CaseOutcome:
    original = b"\xef\xbb\xbf" + "Source: archive-7319\r\n编码依据：UTF-8 BOM；原件使用 CRLF。\r\nCount: 120\r\n".encode("utf-8")
    document = context.root / "encoding.txt"
    document.write_bytes(original)
    fixture_answer = "UTF-8 with BOM; CRLF line endings. [encoding.txt:1-3]\n" + "\n".join(
        f"Line {number:03}: preserve the original source and full response for review." for number in range(1, 121)) + "\nEND-ORIGINAL-7319"
    session = context.session([Reply(tool="read_authorized_text"), Reply(text=fixture_answer)])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read the selected source. Explain its declared encoding and line endings, cite it, then provide all 120 numbered explanation lines totaling more than 4096 characters and end with END-ORIGINAL-7319. Preserve the full response.",
        scope=None, document=str(document), provider_config=session.config,
        approval=ApprovalSettings(mode="full_access", access=AccessSettings(read_only=True)))
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=document, task=task, model=model)
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in turn.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    normalized = original.decode("utf-8-sig").replace("\r\n", "\n")
    source_inputs = [item["sdk_input_id"] for item in session.sdk_inputs[1:] if any(
        part["part_kind"] == "tool-return" and part.get("tool_name") == "read_authorized_text" and part.get("content") == normalized
        for message in item["messages"] for part in message["parts"])]
    unchanged = document.read_bytes() == original
    lines = [line.strip() for line in turn.answer.splitlines() if line.strip()]
    numbered = re.compile(r"(?:Line\s+)?(\d{1,3})[:.)]\s+(\S.*)", re.IGNORECASE)
    first = next((index for index, line in enumerate(lines) if numbered.fullmatch(line)), len(lines))
    body = lines[first:-1]
    matches = [numbered.fullmatch(line) for line in body]
    numbers = [int(match[1]) for match in matches if match is not None]
    observation = turn.request_attempts[-1].observation if turn.request_attempts else None
    truncated = observation is not None and observation.finish_reason in {"length", "MAX_TOKENS"}
    complete = (turn.status == "completed" and not truncated and bool(lines) and lines[-1] == "END-ORIGINAL-7319"
        and len(matches) == 120 and all(matches) and numbers == list(range(1, 121)) and len("\n".join(body)) > 4096)
    citation_pattern = r"encoding\.txt(?::(\d+)(?:-(\d+))?)?(?![\w.:-])"
    citations = re.findall(citation_pattern, turn.answer, re.IGNORECASE)
    located = bool(citations) and all(not start or 1 <= int(start) <= int(end or start) <= 3 for start, end in citations)
    header = re.sub(citation_pattern, "", " ".join(lines[:first]), flags=re.IGNORECASE)
    header = header.casefold().replace("‑", "-").replace("–", "-").replace("[", "").replace("]", "").replace("**", "").strip(" .!。")
    declaration = re.fullmatch(r"(?:encoding\s*:\s*)?(utf[\s-]*(?:8|16|32)|ascii)\s+((?:with\s+(?:a\s+)?)?(?:bom|byte order mark)|without\s+(?:a\s+)?bom|no\s+bom)\s*(?:;|,|and)\s*(crlf|lf-only|lf)\s+(?:line endings|newlines)(?:;\s*prefix\s+([a-f0-9 ]+))?", header)
    fact: FactStatus
    if not complete:
        fact, reason = "failed", "The response lacks the complete ordered 120-line long body or final marker, or was truncated."
    elif not located:
        fact, reason = "failed", "The citation does not locate the preserved original encoding.txt."
    elif declaration:
        correct = (re.sub(r"[\s-]", "", declaration[1]) == "utf8" and not declaration[2].startswith(("without", "no "))
            and declaration[3] == "crlf" and (not declaration[4] or declaration[4].replace(" ", "") == "efbbbf"))
        fact, reason = ("passed", "Required encoding facts, source, ordered long structure and ending are present; wording is not compared byte-for-byte.") if correct else ("failed", "The explicit encoding, BOM, newline or byte-prefix declaration contradicts the preserved original.")
    else:
        fact, reason = "unknown", "The complete response is preserved; this encoding explanation needs human interpretation."
    program_passed = complete and bool(source_inputs) and unchanged
    return CaseOutcome("passed" if program_passed and fact == "passed" else "inconclusive" if program_passed and fact == "unknown" else "failed",
        {"answer": turn.answer, "turn_status": turn.status, "error": turn.error,
        "goal_program_contract_passed": bool(program_passed), "fact_contract_status": fact, "fact_contract_reason": reason,
        "source": {"path": str(document), "sha256": sha256(original).hexdigest(),
            "encoding": "UTF-8 with BOM; CRLF line endings", "basis": "EF BB BF prefix and preserved 0D 0A bytes",
            "unchanged": unchanged, "citation": "encoding.txt:1-3", "native_input_ids": source_inputs},
        "response_complete": complete, "numbered_lines": len(numbers), "response_characters": len(turn.answer),
        "product_attempts": turn.requests, "mock_http_attempts": len(session.exchanges)},
        {"conversation": conversation.path, "exchange": evidence, "attachment": document},
        target_status="passed" if program_passed and fact == "passed" else "pending_review" if program_passed and fact == "unknown" else "failed",
        deterministic_status="not_measured" if fact == "unknown" else fact)



async def request_failure(context: CaseContext, *, scenario: str | None = None) -> CaseOutcome:
    scenario = scenario or context.case_id
    scripts = {
        "ordinary.retry": [Reply(status=503), Reply(text="Recovered answer.")],
        "ordinary.interrupted": [Reply(text="Partial answer.", interrupt=True)],
        "ordinary.missing-usage": [Reply(text="No usage was reported.", usage=False)],
    }
    session = context.session(scripts[scenario])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Give one short answer.", scope=None, provider_config=session.config)
    updates: list[str] = []
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model,
            on_update=lambda update: updates.append(update.text) if update.kind == "text" else None)
    attempts = [item.model_dump(mode="json") for item in turn.request_attempts]
    evidence = session.save(context.root / "exchange.json", attempts=attempts,
                            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    if scenario == "ordinary.retry":
        passed = turn.status == "completed" and turn.answer == "Recovered answer." and turn.requests == len(session.exchanges) == 2
    elif scenario == "ordinary.interrupted":
        passed = turn.status == "failed" and turn.answer == "Partial answer." and "Partial answer." in "".join(updates) and turn.requests == 1
    else:
        observation = turn.request_attempts[0].observation if turn.request_attempts else None
        passed = turn.status == "completed" and observation is not None and observation.input_tokens is None and observation.cache_read_tokens is None and observation.usage_status == "unknown"
    return CaseOutcome("passed" if passed else "failed", {"answer": turn.answer, "turn_status": turn.status,
        "error": turn.error, "product_attempts": turn.requests, "mock_http_attempts": len(session.exchanges), "updates": updates},
        {"conversation": conversation.path, "exchange": evidence})


async def input_budget(context: CaseContext) -> CaseOutcome:
    config = ProviderConfig(auto_compaction=False, input_token_limit_override=1,
                            output_token_reserve=16, context_safety_margin=0)
    session = context.session([], config=config)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="This request must exceed a one-token input budget.", scope=None, provider_config=config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model)
    record = json.loads(conversation.path.read_text(encoding="utf-8"))
    prepared = record["prepared_contexts"]
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for item in turn.request_attempts],
                            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    passed = not session.exchanges and bool(prepared) and not prepared[0]["accepted"] and prepared[0]["input_tokens"] > 1
    return CaseOutcome("passed" if passed else "failed", {"turn_status": turn.status, "error": turn.error, "prepared": prepared},
                       {"conversation": conversation.path, "exchange": evidence})


async def input_layout(context: CaseContext) -> CaseOutcome:
    session = context.session([Reply(text="First answer."), Reply(text="Second answer.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Answer the two explicit user questions.", scope=None, provider_config=session.config)
    async with session.model() as model:
        first = await conversation.ask("Give the first answer.", document=None, task=task, model=model)
        second = await conversation.ask("Give the second answer.", document=None, task=task, model=model)
    record = json.loads(conversation.path.read_text(encoding="utf-8"))
    prepared = record["prepared_contexts"]
    evidence = session.save(context.root / "exchange.json", attempts=[item.model_dump(mode="json") for turn in conversation.turns for item in turn.request_attempts],
                            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    bodies = [json.loads(item["request_utf8"]) for item in session.exchanges]
    passed = (first.status == second.status == "completed" and len(bodies) == 2
              and bodies[0]["tools"] == bodies[1]["tools"] and bodies[0]["messages"][0] == bodies[1]["messages"][0]
              and len(prepared) == 2 and all(item["accepted"] and item["input_tokens"] > 0 for item in prepared))
    return CaseOutcome("passed" if passed else "failed", {"prepared": prepared}, {"conversation": conversation.path, "exchange": evidence})


def register(registry: CaseRegistry) -> None:
    registry.register(AcceptanceCase("ordinary.question", "ordinary", ("deepseek", "openai-chat", "google"), question))
    registry.register(AcceptanceCase("ordinary.attachment", "attachments", ("deepseek", "openai-chat", "google"), attachment))
    registry.register(AcceptanceCase("ordinary.encoded-long", "attachments", ("deepseek", "openai-chat", "google"), encoded_long))
    for name in ("ordinary.retry", "ordinary.interrupted", "ordinary.missing-usage"):
        registry.register(AcceptanceCase(name, "service-failures", ("deepseek", "openai-chat", "google"), request_failure))
    registry.register(AcceptanceCase("ordinary.input-budget", "request-input", ("deepseek",), input_budget))
    registry.register(AcceptanceCase("ordinary.input-layout", "request-input", ("deepseek", "openai-chat"), input_layout))
