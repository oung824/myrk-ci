import asyncio
from collections import Counter
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from myrk.conversation import Conversation, Task, TaskScope
from myrk.actions import Action
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.review import review_action
from myrk.task_control import TaskBudget
from tests.support.observed_http import observed_mock_transport


@pytest.mark.parametrize("openai", [False, True])
def test_later_measured_reviews_preserve_earlier_unknown_totals_per_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai: bool) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "proof.txt").write_text("授权材料", encoding="utf-8")
    targets = [tmp_path / f"approved-{number}.txt" for number in range(3)]
    review_calls = 0
    review_usage = [
        {"completion_tokens": 5, "prompt_cache_hit_tokens": 1},
        {"prompt_tokens": 100, "completion_tokens": 7},
        {"prompt_tokens": 200, "prompt_tokens_details": {"cached_tokens": 1}},
        {"prompt_tokens": 100, "completion_tokens": 7},
        {"prompt_tokens": 300, "completion_tokens": 9},
    ]

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal review_calls
        messages = json.loads(request.content)["messages"]
        reviewing = "你是独立动作审核者" in str([message for message in messages if message["role"] in {"system", "developer"}])
        tool = None
        if reviewing:
            usage = review_usage[review_calls]
            review_calls += 1
            if review_calls in {1, 3}:
                tool = ("read_text", {"path": "proof.txt"})
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "已核对用户允许的确切小文件及其内容，动作符合授权。", "same_denied_outcome": False})
        else:
            usage = {"prompt_tokens": 100, "completion_tokens": 3}
            results = [message for message in messages if message["role"] == "tool"]
            if len(results) < 3:
                tool = ("create_text", {"path": str(targets[len(results)]), "content": "approved"})
            answer = "三个文件已创建，等待验收。"
        delta = {"role": "assistant"}
        if tool is not None:
            name, arguments = tool
            delta["tool_calls"] = [{"index": 0, "id": f"call-{reviewing}-{review_calls}-{len(messages)}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        else:
            delta["content"] = answer
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "controlled", "usage": usage,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                task = Task(goal="创建三个明确指定的文件", scope=scope, approval=ApprovalSettings(mode="auto_review"))
                if openai:
                    task.provider_config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-4.1", protocol="openai-chat")
                path = tmp_path / "records" / "cumulative.json"
                turn = await Conversation(path).ask(task.goal, document=None, scope=scope, task=task, model=model, review_model=model)
                assert turn.status == "completed", turn.error
                assert [target.read_text(encoding="utf-8") for target in targets] == ["approved"] * 3
                saved = Conversation.open(path).turns[0]
                reviews = [action.review for action in saved.actions if action.review is not None]
                assert [(review.input_tokens, review.output_tokens) for review in reviews] == [(None, 12), (300, None), (300, 9)]
                assert [review.unmeasured_requests for review in reviews] == [1, 1, 0]
                assert [(item.input_tokens, item.output_tokens) for review in reviews for item in review.request_attempts] == [(None, 5), (100, 7), (200, None), (100, 7), (300, 9)]
                assert (saved.task.review_input_tokens, saved.task.review_output_tokens) == (None, None)
                assert saved.task.review_unmeasured_requests == 2
                assert saved.task.review_requests == review_calls == 5
                assert saved.task.requests == saved.task.turns_used == saved.task.total_requests == 9
                assert (saved.input_tokens, saved.output_tokens) == (400, 12)
                assert [item.number for review in reviews for item in review.request_attempts] == [2, 3, 5, 6, 8]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("usages,prior,expected_records,expected_total,expected_unmeasured,attempts", [
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, None, [(100, 5), (100, 5)], (200, 10), 0, 2),
    ([{"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, 5), (100, 5)], (None, 10), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}, {"completion_tokens": 5, "prompt_cache_hit_tokens": 1}], None, [(100, 5), (None, 5)], (None, 10), 1, 2),
    ([{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(100, None), (100, 5)], (200, None), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}, {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}], None, [(100, 5), (100, None)], (200, None), 1, 2),
    ([None, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, None), (100, 5)], (None, None), 1, 2),
    ([{"prompt_tokens": 0, "completion_tokens": 0}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(0, 0), (100, 5)], (100, 5), 0, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (None, 7), [(100, 5), (100, 5)], (None, 17), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (11, None), [(100, 5), (100, 5)], (211, None), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (None, None), [(100, 5), (100, 5)], (None, None), 1, 2),
    ([503, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, None), (100, 5)], (None, None), 1, 3),
])
@pytest.mark.parametrize("openai", [False, True])
def test_concurrent_complete_reviews_keep_known_account_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usages: list[dict | int | None], prior: tuple | None, expected_records: list[tuple], expected_total: tuple, expected_unmeasured: int, attempts: int, openai: bool) -> None:
    async def run() -> None:
        ready = asyncio.Event()
        sent = 0
        task = Task(goal="审核两个互不依赖的确切文件动作", scope=None, request_limit=10, max_turns=10, approval=ApprovalSettings(mode="auto_review"))
        if prior is not None:
            task.requests = task.total_requests = task.turns_used = task.review_requests = 1
            task.review_input_tokens, task.review_output_tokens = prior
            task.review_unmeasured_requests = 1
            prior_path = tmp_path / "prior-account.json"
            prior_path.write_text(task.model_dump_json(), encoding="utf-8")
            task = Task.model_validate_json(prior_path.read_text(encoding="utf-8"))
        budget = TaskBudget(task)
        actions = [Action(task_id=task.id, kind="create", target=str(tmp_path / f"approved-{number}.txt"), impact="创建已明确授权的小文件", approval_settings=task.approval) for number in range(2)]
        path = tmp_path / "reviews.json"

        def save() -> None:
            path.write_text(json.dumps({"task": task.model_dump(mode="json"), "actions": [action.model_dump(mode="json") for action in actions]}), encoding="utf-8")

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal sent
            usage = usages[sent] if sent < len(usages) else {"prompt_tokens": 100, "completion_tokens": 5}
            sent += 1
            if sent >= 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            assert task.review_requests >= 2, "两个请求均已 prepare 后才能返回"
            if isinstance(usage, int):
                return httpx2.Response(usage, json={"error": {"message": "controlled retry", "type": "server_error"}})
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确允许确切的小文件内容，目标和影响均已核对。", "same_denied_outcome": False})
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                     }
            if usage is not None:
                chunk["usage"] = usage
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                allowed = await asyncio.gather(*(review_action(action, model=model, budget=budget, directory=tmp_path,
                    messages=[], denials=[], save=save, context=lambda: "unchanged") for action in actions))
        assert allowed == [True, True]
        record = json.loads(path.read_text(encoding="utf-8"))
        saved_task = Task.model_validate(record["task"])
        saved_actions = [Action.model_validate(action) for action in record["actions"]]
        assert Counter((action.review.input_tokens, action.review.output_tokens) for action in saved_actions) == Counter(expected_records)
        assert (saved_task.review_input_tokens, saved_task.review_output_tokens) == expected_total
        assert saved_task.review_unmeasured_requests == expected_unmeasured
        previous = 1 if prior is not None else 0
        assert saved_task.review_requests == saved_task.requests == saved_task.total_requests == previous + attempts == previous + sent
        assert saved_task.turns_used == previous + 2
        assert saved_task.request_limit == saved_task.max_turns == 10
        new_attempts = [item for action in saved_actions for item in action.review.request_attempts]
        assert sorted(item.number for item in new_attempts) == list(range(previous + 1, previous + attempts + 1))
        assert sum(item.status == "failed" for item in new_attempts) == (1 if attempts == 3 else 0)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("first_usage,finished_total,snapshot_unmeasured", [
    ({"prompt_tokens": 100, "completion_tokens": 5}, (200, 10), 1),
    ({"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, (None, 10), 2),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}, (200, None), 2),
])
@pytest.mark.parametrize("openai", [False, True])
def test_saved_inflight_review_totals_stay_unknown_after_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_usage: dict, finished_total: tuple, snapshot_unmeasured: int, openai: bool) -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        (tmp_path / "proof.txt").write_text("已授权的审核资料", encoding="utf-8")
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="保存正在等待的审核并恢复", scope=scope, approval=ApprovalSettings(mode="auto_review"))
        budget = TaskBudget(task)
        actions = [Action(task_id=task.id, kind="create", target=str(tmp_path / "approved.txt"), approval_settings=task.approval)]
        path = tmp_path / "inflight.json"
        calls = 0

        def save() -> None:
            path.write_text(json.dumps({"task": task.model_dump(mode="json"), "actions": [action.model_dump(mode="json") for action in actions]}), encoding="utf-8")

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "已核对确切文件及用户同意，动作符合明确授权。", "same_denied_outcome": False})
            delta = {"role": "assistant", "content": answer} if calls > 1 else {"role": "assistant", "tool_calls": [{"index": 0, "id": "read-proof", "type": "function", "function": {"name": "read_text", "arguments": '{"path":"proof.txt"}'}}]}
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if calls > 1 else "tool_calls"}],
                     "usage": first_usage if calls == 1 else {"prompt_tokens": 100, "completion_tokens": 5}}
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                pending = asyncio.create_task(review_action(actions[0], model=model, budget=budget, directory=tmp_path,
                    messages=[], denials=[], save=save, context=lambda: "unchanged"))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    snapshot = path.read_text(encoding="utf-8")
                    (tmp_path / "suspended-snapshot.json").write_text(snapshot, encoding="utf-8")
                    saved = json.loads(snapshot)
                    assert (saved["task"]["review_input_tokens"], saved["task"]["review_output_tokens"]) == (None, None)
                    assert (saved["actions"][0]["review"]["input_tokens"], saved["actions"][0]["review"]["output_tokens"]) == (None, None)
                    assert (saved["task"]["review_requests"], saved["task"]["review_unmeasured_requests"]) == (2, snapshot_unmeasured)
                finally:
                    release.set()
                    assert await pending
                assert (task.review_input_tokens, task.review_output_tokens) == finished_total
                restored = Task.model_validate_json(json.dumps(saved["task"]))
                restored_budget = TaskBudget(restored)
                next_action = Action(task_id=restored.id, kind="create", target=str(tmp_path / "restored.txt"), approval_settings=restored.approval)
                restored_path = tmp_path / "restored.json"
                assert await review_action(next_action, model=model, budget=restored_budget, directory=tmp_path,
                    messages=[], denials=[], save=lambda: restored_path.write_text(restored.model_dump_json(), encoding="utf-8"), context=lambda: "unchanged")
                saved_restored = Task.model_validate_json(restored_path.read_text(encoding="utf-8"))
                assert (saved_restored.review_input_tokens, saved_restored.review_output_tokens) == (None, None)
                assert saved_restored.review_unmeasured_requests == snapshot_unmeasured
                assert saved_restored.review_requests == saved_restored.requests == saved_restored.turns_used == saved_restored.total_requests == 3

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
