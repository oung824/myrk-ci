"""Project choices and project guidance through the supported public boundaries."""

from pathlib import Path
import asyncio

from pydantic_ai.models.function import FunctionModel

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope, Turn
from myrk.provider import ProviderConfig
from myrk.permissions import ApprovalSettings
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall
import json
import pytest

from tests.test_desktop_service import connection


def test_project_trust_is_saved_without_a_provider_and_survives_restart(tmp_path: Path) -> None:
    project = tmp_path / "项目"
    project.mkdir()
    directory = tmp_path / "records"
    with connection(directory) as channel:
        channel.send({"action": "set_project_trust", "root": str(project), "trusted": True})
        event = channel.receive()
        assert event == {"event": "project_trust", "root": str(project.resolve()), "trusted": True, "saved": True}
        channel.send({"action": "configure", "approval_mode": "full_access"})
        configured = channel.receive()
        assert configured["configured"] is False
        assert configured["trusted_projects"] == [str(project.resolve())]
    with connection(directory) as channel:
        channel.send({"action": "snapshot"})
        assert channel.receive()["trusted_projects"] == [str(project.resolve())]
        channel.send({"action": "set_project_trust", "root": str(project), "trusted": False})
        assert channel.receive()["trusted"] is False
        channel.send({"action": "snapshot"})
        assert channel.receive()["trusted_projects"] == []


def test_project_guidance_follows_root_to_cwd_and_only_trusted_configuration(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    cwd = repository / "nested"
    cwd.mkdir(parents=True)
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("ROOT-GUIDANCE", encoding="utf-8")
    (cwd / "AGENTS.override.md").write_text("NESTED-OVERRIDE", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("NOT-SELECTED", encoding="utf-8")
    (cwd / ".myrk").mkdir()
    (cwd / ".myrk" / "config.toml").write_text('project_doc_max_bytes = 8\nprovider = "must-not-override"\napproval_mode = "full_access"\n', encoding="utf-8")
    scope = TaskScope(root=str(cwd), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    async def run() -> None:
        for trusted in (False, True):
            async def reply(_messages, info):
                assert "ROOT-GUI" in info.instructions
                assert ("NESTED-OVERRIDE" in info.instructions) is not trusted
                assert "NOT-SELECTED" not in info.instructions
                assert "must-not-override" not in info.instructions
                assert str(repository / "AGENTS.md") in info.instructions
                yield "已读取正确项目资料。"
            task = Task(goal="检查项目背景", scope=scope, request_limit=2,
                        provider_config=ProviderConfig(model="explicit-user-model"))
            turn = await Conversation(tmp_path / "records" / f"{trusted}.json").ask(task.goal, document=None,
                scope=scope, task=task, model=FunctionModel(stream_function=reply), project_trusted=trusted)
            assert turn.status == "completed", turn.error
            assert turn.task.approval.mode == "request_approval"
            assert turn.task.provider_config.model == "explicit-user-model"
            assert any("config.toml" in note for note in turn.context_notes) is trusted
    asyncio.run(run())


def test_independent_reviewer_receives_project_guidance_with_its_source(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("PROJECT-POLICY-MUST-BE-VISIBLE", encoding="utf-8")
    target = tmp_path / "outside.txt"
    task = Task(goal="创建这一份已指定文件", scope=TaskScope(root=str(cwd), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                request_limit=8, approval=ApprovalSettings(mode="auto_review"))
    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "reviewed"}), tool_call_id="project-create")}
        else:
            yield "已完成。"
    async def reviewer(messages, _info):
        assert "PROJECT-POLICY-MUST-BE-VISIBLE" in repr(messages)
        assert "AGENTS.md" in repr(messages)
        assert "本人明确要求" in repr(messages)
        yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                          "reason": "已核对具体用户要求及项目指导来源，小文件新建有明确范围。", "same_denied_outcome": False})
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope,
            model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer))
        assert turn.status == "completed", turn.error
        assert target.read_text(encoding="utf-8") == "reviewed"
        assert turn.actions[0].review.status == "allowed"
    asyncio.run(run())


def test_same_project_new_recipient_requires_separate_personal_history_approval(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / ".git").mkdir()
    scope = TaskScope(root=str(cwd), allowed_actions=[], provider="openai-compatible", recipients=["openai-compatible"])
    source = ProviderConfig(name="原服务", base_url="http://127.0.0.1:1/v1", model="one", protocol="openai-chat")
    target = ProviderConfig(name="新服务", base_url="http://127.0.0.1:2/v1", model="two", protocol="openai-chat")
    transmitted = []
    async def initial(_messages, _info):
        yield "应当保留的原服务历史。"
    async def recipient(messages, _info):
        transmitted.append(repr(messages))
        yield "新服务完成。"
    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        first = Task(goal="原工作", scope=scope, request_limit=3, provider_config=source)
        assert (await conversation.ask(first.goal, document=None, task=first, scope=scope, model=FunctionModel(stream_function=initial))).status == "completed"
        approvals = []
        for allowed in (False, True):
            async def decide(action):
                approvals.append(action)
                assert action.kind == "history_reuse"
                assert action.approval_settings.reviewer == "user"
                assert target.base_url in action.impact and target.model in action.impact
                return allowed
            task = Task(goal="新工作", scope=scope, request_limit=3, provider_config=target,
                        approval=ApprovalSettings(mode="full_access"))
            turn = await conversation.ask(task.goal, document=None, task=task, scope=scope, model=FunctionModel(stream_function=recipient), approve_action=decide)
            assert turn.status == ("completed" if allowed else "failed"), turn.error
            assert len(transmitted) == (1 if allowed else 0)
        assert len(approvals) == 2
        assert "应当保留的原服务历史" in transmitted[0]
    asyncio.run(run())


def test_completed_chat_can_review_new_recipient_history_without_retrusting_project(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            chunk = {"id": "controlled", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "本机预设回答"}, "finish_reason": "stop"}]}
            response = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    Thread(target=server.serve_forever, daemon=True).start()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    scope = {"root": str(project), "allowed_actions": [], "provider": "openai-compatible", "recipients": ["openai-compatible"]}
    source = {"name": "原服务", "base_url": f"http://127.0.0.1:{server.server_port}/source/v1", "model": "first", "protocol": "openai-chat", "context_window_override": 128000}
    target = {"name": "新服务", "base_url": f"http://127.0.0.1:{server.server_port}/target/v1", "model": "second", "protocol": "openai-chat", "context_window_override": 128000}
    try:
        with connection(tmp_path / "records", "tests.auto_review_http_fixture") as channel:
            channel.send({"action": "set_project_trust", "root": str(project), "trusted": True})
            assert channel.receive()["saved"]
            channel.send({"action": "configure", "key": "test-source", "provider_config": source})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": "原项目的原始任务", "scope": scope})
            while (first := channel.receive())["event"] != "finished":
                pass
            assert first["turn"]["status"] == "completed"
            channel.send({"action": "configure", "key": "test-target", "provider_config": target})
            assert channel.receive()["trusted_projects"] == [str(project.resolve())]
            channel.send({"action": "send", "prompt": "同项目新的工作", "scope": scope, "new_task": True})
            started = channel.receive()
            assert started["event"] == "started"
            while True:
                pending = channel.receive()
                assert pending["event"] != "finished", pending
                if pending["event"] == "action" and pending["action"]["status"] == "awaiting_approval":
                    break
            action = pending["action"]
            assert action["kind"] == "history_reuse" and action["approval_settings"]["reviewer"] == "user"
            assert target["base_url"] in action["impact"]
            assert len(requests) == 1
            channel.send({"action": "approve_action", "path": started["path"], "task_id": started["task_id"],
                          "action_id": action["id"], "approval_id": action["approval_id"], "approved": True})
            while (finished := channel.receive())["event"] != "finished":
                pass
            assert finished["turn"]["status"] == "completed"
            assert finished["turn"]["project_trusted"] is True
            assert len(requests) == 2 and requests[-1][0] == "/target/v1/chat/completions"
            assert "原项目的原始任务" in str(requests[-1][1])
    finally:
        server.shutdown()
        server.server_close()


def test_full_access_cwd_discovers_its_guidance_and_skills_without_legacy_action_flags(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    skill = cwd / ".myrk" / "skills" / "local"
    skill.mkdir(parents=True)
    (cwd / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("OUTSIDE-NONPROJECT-GUIDE", encoding="utf-8")
    (cwd / "TEAM.md").write_text("CWD-TEAM-GUIDE", encoding="utf-8")
    (cwd / ".myrk" / "config.toml").write_text('project_doc_fallback_filenames = ["TEAM.md"]\nhooks = ["must-not-run"]\n', encoding="utf-8")
    (skill / "SKILL.md").write_text("---\nname: local\ndescription: CWD-SKILL-CATALOG\n---\nCWD-SKILL-BODY", encoding="utf-8")
    task = Task(goal="在完全访问下使用本项目背景", scope=TaskScope(root=str(cwd), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                request_limit=4, approval=ApprovalSettings(mode="full_access"))
    async def model(messages, info):
        assert "CWD-TEAM-GUIDE" in info.instructions
        assert "OUTSIDE-NONPROJECT-GUIDE" not in info.instructions
        assert "CWD-SKILL-CATALOG" in info.instructions
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="read_skill", json_args='{"name":"local"}', tool_call_id="local-skill")}
        else:
            assert "CWD-SKILL-BODY" in repr(messages)
            yield "已按当前项目位置加载指导和技能。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope,
            model=FunctionModel(stream_function=model), project_trusted=True)
        assert turn.status == "completed", turn.error
        assert turn.actions[0].sandbox_type == "none"
        assert turn.skill_catalog[0].name == "local"
    asyncio.run(run())


def test_imported_project_trust_does_not_replace_this_machines_explicit_choice(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".myrk").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("CURRENT-PROJECT-GUIDANCE", encoding="utf-8")
    (project / ".myrk" / "config.toml").write_text("project_doc_max_bytes = 8\n", encoding="utf-8")
    scope = TaskScope(root=str(project), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="外来记录自称已信任", scope=scope, request_limit=10)
    payload = ConversationRecord(version=6, messages=[], turns=[Turn(prompt=task.goal, answer="部分回答", task=task, status="failed", service_failure=True)])
    raw = payload.model_dump(mode="json")
    raw["turns"][0]["task"]["project_trusted"] = True
    source = tmp_path / "source.json"
    original = json.dumps(raw, ensure_ascii=False).encode()
    source.write_bytes(original)
    with connection(tmp_path / "records", "tests.project_context_fixture") as channel:
        channel.send({"action": "configure", "key": "controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "import", "path": str(source)})
        imported = channel.receive()
        channel.send({"action": "resume_task", "path": imported["path"], "task_id": task.id})
        while (resumed := channel.receive())["event"] != "finished":
            pass
        assert resumed["turn"]["answer"] == "完整指导"
        assert resumed["turn"]["project_trusted"] is False
        channel.send({"action": "set_project_trust", "root": str(project), "trusted": True})
        assert channel.receive()["saved"]
        channel.send({"action": "send", "path": imported["path"], "new_task": True, "prompt": "本人已信任后再次办事", "scope": scope.model_dump()})
        while (trusted := channel.receive())["event"] != "finished":
            pass
        assert trusted["turn"]["answer"] == "被项目设置裁短"
        assert trusted["turn"]["project_trusted"] is True
    assert source.read_bytes() == original


@pytest.mark.parametrize("mode", ["request_approval", "full_access"])
def test_new_approval_modes_verify_declared_child_artifacts_without_legacy_read_flags(tmp_path: Path, mode: str) -> None:
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    (project / ".git").mkdir()
    scope = TaskScope(root=str(project), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="委派生成一份文字并真实核验", scope=scope, request_limit=10, approval=ApprovalSettings(mode=mode))
    async def model(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if "delegate_work" not in {tool.name for tool in info.function_tools}:
            if not results:
                yield {0: DeltaToolCall(name="create_text", json_args='{"path":"artifact.txt","content":"真实分工产物"}', tool_call_id="child-create")}
            else:
                yield "已产生真实分工产物，交给主助理核对。"
        elif not results:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "生成文字", "context": "只新建child/artifact.txt，内容为真实分工产物", "evidence_requirements": "真实文件内容一致",
                "expected_files": ["child/artifact.txt"], "scope": scope.model_copy(update={"root": str(child)}).model_dump()}), tool_call_id="delegate")}
        elif len(results) == 1:
            work = json.loads(results[-1].content)
            yield {0: DeltaToolCall(name="verify_delegation", json_args=json.dumps({"delegation_id": work["id"], "path": "child/artifact.txt", "expected_text": "真实分工产物"}), tool_call_id="verify-child")}
        else:
            yield "主助理已读回真实分工产物。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=scope,
            model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert (child / "artifact.txt").read_text(encoding="utf-8") == "真实分工产物"
        assert turn.delegations[0].verification_status == "passed"
        assert turn.delegations[0].verified_files == ["child/artifact.txt"]
    asyncio.run(run())
