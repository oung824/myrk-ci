"""Interrupted optional diagnostics remain originals, not conversation output."""
import asyncio
from hashlib import sha256
import json
import pytest


@pytest.mark.parametrize("role", ["receiver", "owner"])
def test_optional_empty_save_diagnostic_does_not_replace_or_break_output_originals(acceptance_public_root, role):
    from myrk.conversation import Conversation, Task
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    from tests.support.agent_acceptance.core import AcceptanceCase, CaseOutcome, CaseRegistry
    from tests.support.agent_acceptance.replay import Reply

    async def run(context):
        session = context.session([Reply(text="This controlled native reply is retained. No semantic acceptance or product readiness is claimed.")])
        task = Task(goal="Return a controlled diagnostic observation.", scope=None, provider_config=session.config)
        conversation = Conversation(context.root / "conversation.json")
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[row.model_dump(mode="json") for row in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        diagnostic = context.root / "work" / role / "save-diagnostics.json"
        diagnostic.parent.mkdir(parents=True)
        diagnostic.write_bytes(b"")
        return CaseOutcome("passed", {"real_http_attempts": 0}, {"conversation": conversation.path,
            "exchange": exchange, f"work/{role}/save-diagnostics.json": diagnostic})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("diagnostic.empty-original", "reporting", ("deepseek",), run))
    result = asyncio.run(run_plan(AcceptancePlan(cases=["diagnostic.empty-original"]), root=acceptance_public_root, registry=registry))
    outcome = result.cases[0]
    assert outcome.product_status == outcome.tool_status == "passed"
    diagnostic = outcome.evidence[f"work/{role}/save-diagnostics.json"]
    assert diagnostic.read_bytes() == b""
    review = json.loads(outcome.evidence["semantic_review"].read_bytes())
    assert len(review["outputs"]) == 1
    assert review["semantic_status"] == "not_measured"
    evidence = json.loads(outcome.evidence["evidence_index"].read_bytes())
    assert evidence["links"][f"work/{role}/save-diagnostics.json"]["sha256"] == sha256(b"").hexdigest()
    assert evidence["links"][f"work/{role}/save-diagnostics.json"]["bytes"] == 0


def test_empty_json_cannot_be_relabelled_as_the_product_conversation(acceptance_public_root):
    import pytest
    from myrk.conversation import Conversation, Task
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    from tests.support.agent_acceptance.core import AcceptanceCase, CaseOutcome, CaseRegistry, SharedControlError
    from tests.support.agent_acceptance.replay import Reply

    async def run(context):
        session = context.session([Reply(text="The mandatory product original must still be validated.")])
        task = Task(goal="Return a controlled mandatory-original observation.", scope=None, provider_config=session.config)
        conversation = Conversation(context.root / "actual-conversation.json")
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[row.model_dump(mode="json") for row in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        malformed = context.root / "save-diagnostics.json"
        malformed.write_bytes(b"")
        return CaseOutcome("passed", {"real_http_attempts": 0}, {"conversation": malformed, "exchange": exchange})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("diagnostic.missing-product-original", "reporting", ("deepseek",), run))
    with pytest.raises(SharedControlError):
        asyncio.run(run_plan(AcceptancePlan(cases=["diagnostic.missing-product-original"]), root=acceptance_public_root, registry=registry))
