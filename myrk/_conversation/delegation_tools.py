import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.models import Model

from myrk._conversation.read_tools import TEXT_SUFFIXES
from myrk._conversation.records import Delegation, TaskScope, Update
from myrk.actions import Action, folder_write_lock

if TYPE_CHECKING:
    from myrk._conversation.project_tools import ProjectToolContext
    from myrk.conversation import Conversation


def register_delegation_tools(
    agent: Agent[None, str],
    tools: "ProjectToolContext",
    *,
    child_factory: "type[Conversation]",
    model: Model,
    thinking: bool,
    reasoning: str | None,
    approve_action: Callable[[Action], Awaitable[bool]] | None,
    review_model: Model | None,
    review_model_resolver: Callable[[], Awaitable[Model]] | None,
) -> None:
    @agent.tool_plain
    async def update_plan(steps: list[str]) -> str:
        """Show or revise necessary task steps. A plan does not execute actions or grant permissions."""
        tools.budget.checkpoint(applied_supplements=tools.seen_supplements)
        tools.turn.plan = steps
        tools.conversation.save()
        if tools.on_update:
            tools.on_update(Update("plan", plan=steps))
        return "当前办事步骤：" + "；".join(steps)

    @agent.tool_plain(sequential=True)
    async def delegate_work(goal: str, context: str, evidence_requirements: str,
                            expected_files: list[str], scope: TaskScope | None = None) -> str:
        """Delegate one separable job sequentially, with goal, necessary context, inherited scope and expected real files. Main must verify its report."""
        child_scope = scope or tools.task.scope
        async def delegate(action: Action) -> str:
            if child_scope is None or tools.task.scope is None:
                raise PermissionError("没有可继承的任务允许范围，未委派。")
            child_root = Path(child_scope.root).resolve(strict=True)
            if (not child_root.is_relative_to(tools.root)
                    or not set(child_scope.allowed_actions).issubset(tools.task.scope.allowed_actions)
                    or child_scope.provider != tools.task.scope.provider
                    or child_scope.recipients != tools.task.scope.recipients
                    or any(check not in tools.task.scope.checks or (child_root / check.cwd).resolve() != (tools.root / check.cwd).resolve() for check in child_scope.checks)):
                raise PermissionError("分工只能继承主任务允许范围或其子集，不能增加动作、检查或资料接收方。")
            if not expected_files or any(not (tools.root / path).resolve().is_relative_to(child_root) for path in expected_files):
                raise PermissionError("请指定分工范围内可由主助理核对的真实文件；未委派。")
            work = Delegation(goal=goal, context=context, scope=child_scope, max_turns=tools.task.delegation_max_turns,
                              evidence_requirements=evidence_requirements, expected_files=expected_files, record_path="")
            work.record_path = str(tools.conversation.path.parent / f"{tools.conversation.path.stem[:8]}-delegations" / f"{work.id[:8]}.json")
            tools.turn.delegations.append(work)
            tools.conversation.save()
            if tools.on_update:
                tools.on_update(Update("delegation", delegation=work.model_copy(deep=True)))
            def progress(event: Update) -> None:
                if event.action is not None:
                    tools.turn.actions[:] = [item for item in tools.turn.actions if item.id != event.action.id]
                    tools.turn.actions.append(event.action)
                if event.kind == 'artifacts':
                    for artifact in event.artifacts or []:
                        source = next((item for item in tools.turn.actions if item.id == artifact.source_action_id), None)
                        if (artifact.task_id != tools.task.id or artifact.delegation_id != work.id
                                or artifact.source_record_path != work.record_path
                                or not Path(artifact.path).is_relative_to(child_root)
                                or source is None or source.delegation_id != work.id
                                or source.status != 'completed' or source.effect != 'completed'
                                or not any(change.id == artifact.file_change_id and change.path == artifact.path
                                    and change.after_sha256 == artifact.delivered_sha256 for change in source.file_changes)):
                            raise ValueError('分工成果缺少属于本次真实动作与范围的证据，未登记到主会话。')
                    known = {item.id for item in tools.turn.artifacts}
                    tools.turn.artifacts.extend(item.model_copy(deep=True) for item in event.artifacts or [] if item.id not in known)
                tools.conversation.save()
                if tools.on_update:
                    if event.kind == 'artifacts':
                        tools.on_update(Update('artifacts', artifacts=[item.model_copy(deep=True) for item in tools.turn.artifacts]))
                    elif event.kind in {"action", "task"}:
                        tools.on_update(event)
                        if event.kind == "task":
                            tools.on_update(Update("delegation", delegation=work.model_copy(deep=True)))
                    elif event.text:
                        tools.on_update(Update("status", f"分工{work.id[:8]}：{event.text}"))
            child = child_factory(Path(work.record_path))
            child_run = asyncio.create_task(child.ask(
                f"受委派工作：{goal}\n必要资料：{context}\n证据要求：{evidence_requirements}\n"
                f"你的工具路径从你自己的任务文件夹{child_root}起算；主助理声明路径到你本地工具路径的对照：{json.dumps({path: (tools.root / path).resolve().relative_to(child_root).as_posix() for path in expected_files}, ensure_ascii=False)}。\n"
                + (f"你的独立办事轮数上限：{work.max_turns}；每一轮是取得回答并决定下一步的一步，同一步的连接重试另记真实请求次数。没有整项自动时间截止。" if tools.task.request_policy == "independent" else f"这是旧记录，沿用父子共用的{tools.task.request_limit}次请求上限，当前已用{tools.task.requests}次。没有整项自动时间截止。")
                + "不得增加权限。只按必要资料办理，报告真实文件、动作与失败；完成后交回主助理核验，不代表用户已接受。",
                document=None, model=model, on_update=progress, thinking=thinking, reasoning=reasoning,
                scope=child_scope, task=tools.task, budget=tools.budget.fork_requests(work), approve_action=approve_action, review_model=review_model, review_model_resolver=review_model_resolver,
                _delegation_id=work.id, _approval_settings=action.approval_settings,
                _save_supplement_owner=tools.conversation.save,
                project_trusted=tools.turn.project_trusted,
            ))
            try:
                try:
                    await asyncio.shield(child_run)
                finally:
                    if not child_run.done():
                        await asyncio.shield(child_run)
            finally:
                if child.turns:
                    child_turn = child.turns[-1]
                    work.status, work.report, work.error = child_turn.status, child_turn.answer, child_turn.error
                    if tools.task.request_policy == "shared":
                        work.requests = child_turn.requests
                    work.input_tokens, work.output_tokens = child_turn.input_tokens, child_turn.output_tokens
                    if work.status == "running":
                        work.status, work.error = "failed", "分工未能可靠结束，请核对已保存资料与未完成事项。"
                else:
                    work.status, work.error = "failed", "分工未能开始，未收到可靠结果。"
                tools.conversation.save()
                if tools.on_update:
                    tools.on_update(Update("delegation", delegation=work.model_copy(deep=True)))
            return work.model_dump_json()
        return await tools.executor.run("delegate", goal, f"按需顺序委派：{goal}；必要资料：{context}；证据：{evidence_requirements}；同一任务额度与停止规则。", delegate)

    @agent.tool_plain(sequential=True)
    async def verify_delegation(delegation_id: str, path: str, expected_text: str) -> str:
        """Main reads a declared artifact and compares its full real text with independently expected content, not the delegate report."""
        async def verify(action: Action) -> str:
            work = next((item for item in tools.turn.delegations if item.id == delegation_id), None)
            if work is None:
                raise ValueError("没有对应分工记录，未核验。")
            target = (tools.root / path).resolve()
            declared = {(tools.root / item).resolve(): item for item in work.expected_files}
            if (tools.task.approval.access.legacy_scope and "read" not in tools.scope.allowed_actions) or target not in declared:
                raise PermissionError("只能核验分工交接中已声明且获准读取的真实文件。")
            if target.suffix.lower() not in TEXT_SUFFIXES:
                raise PermissionError("只核验本次已允许的普通文字文件，未读取其他格式。")
            if not target.is_relative_to(tools.root) or not target.is_relative_to(Path(work.scope.root).resolve()):
                raise PermissionError("真实文件位置超出分工允许范围，未读取。")
            if not target.is_file():
                work.verification_status = "gaps"
                raise ValueError("分工漏交真实文件，不能把口头成功当成检查通过。")
            assert action.approval_settings is not None
            actual = (await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            action.sandbox_type = tools.file_executor.last_sandbox_type
            if actual != expected_text:
                work.verification_status = "gaps"
                raise ValueError(f"真实原文与独立预期不一致，尚有缺口。来源：{target}\n真实原文：\n{actual}")
            if declared[target] not in work.verified_files:
                work.verified_files.append(declared[target])
            action_gaps = any(item.delegation_id == work.id and item.status != "completed"
                              for previous in tools.conversation.turns if previous.task is not None and previous.task.id == tools.task.id
                              for item in previous.actions)
            work.verification_status = "passed" if {(tools.root / item).resolve() for item in work.verified_files} == set(declared) and work.status == "completed" and not action_gaps else "gaps"
            if tools.on_update:
                tools.on_update(Update("delegation", delegation=work.model_copy(deep=True)))
            return (f"主助理读回：真实原文一致。来源：{target}\n{actual}\n"
                    + ("当前声明文件与期望原文一致，但分工过程仍有此前失败、拒绝或未完成动作；原错误保留，请继续核对。\n" if action_gaps else "")
                    + "仅证明所列全文与预期相符；必要计算和指定检查仍须真实办理。")
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("verify", path, f"主助理核对分工{delegation_id}的真实文件：{path}。", verify)
