"""Line-oriented connection owned by the Electron desktop process."""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
import json
import os
import shutil
import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

import httpx2
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded, ModelRetry
from pydantic_ai.models import Model

from myrk.conversation import Action, Conversation, QueuedTask, Supplement, Task, TaskScope, Turn, Update
from myrk.task_control import TaskBudget
from myrk.provider import GoogleCatalogConnection, ModelFactory, ProviderConfig, configured_model, deepseek_model, fetch_model_catalog, fetch_review_catalog
from myrk.model_controls import ModelSelection, chat_models, connection_identity, fetch_chat_models, resolve_selection, validate_selection
from myrk.memory import MemoryChange, MemoryStore
from myrk.permissions import ApprovalMode, ApprovalSettings
from myrk.recovery import RecoveryReport

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Configure(Command):
    action: Literal["configure"]
    request_id: NonBlank | None = None
    key: NonBlank | None = None
    request_limit: int | None = Field(default=None, ge=1, strict=True)
    delegation_max_turns: int = Field(default=250, ge=1, strict=True)
    thinking: bool = Field(default=False, strict=True)
    active_seconds_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    provider_config: ProviderConfig | None = None
    approval_mode: ApprovalMode | None = None


class ConnectionCredential(Command):
    provider_config: ProviderConfig
    key: NonBlank


class RestoreConnections(Command):
    action: Literal["restore_connections"]
    request_id: NonBlank
    connections: list[ConnectionCredential]
    selected_provider: ProviderConfig | None


class ForgetConnection(Command):
    action: Literal["forget_connection"]
    request_id: NonBlank
    provider_config: ProviderConfig


class FetchModelCatalog(Command):
    action: Literal["fetch_model_catalog"]
    request_id: NonBlank
    key: NonBlank | None = None
    provider_config: GoogleCatalogConnection


class GetModelControls(Command):
    action: Literal["get_model_controls"]
    path: NonBlank | None = None
    refresh: bool = Field(default=False, strict=True)
    request_id: NonBlank | None = None


class SelectModel(Command):
    action: Literal["select_model"]
    path: NonBlank | None = None
    model: NonBlank
    reasoning: NonBlank
    request_id: NonBlank | None = None


class PreviewProvider(Command):
    action: Literal["preview_provider"]
    path: NonBlank | None = None
    task_id: NonBlank
    provider_config: ProviderConfig


class DecideProvider(Command):
    action: Literal["decide_provider"]
    path: NonBlank | None = None
    change_id: NonBlank
    approved: bool = Field(strict=True)


class Send(Command):
    action: Literal["send"]
    path: NonBlank | None = None
    new_task: bool = Field(default=False, strict=True)
    prompt: NonBlank
    document: NonBlank | None = None
    scope: TaskScope | None = None
    approval_mode: ApprovalMode | None = None
    model_selection: ModelSelection | None = None


class SupplementCommand(Command):
    path: NonBlank
    task_id: NonBlank
    supplement_id: NonBlank


class SubmitSupplement(SupplementCommand):
    action: Literal["submit_supplement"]
    prompt: str = Field(strict=True, pattern=r"\S")


class RevokeSupplement(SupplementCommand):
    action: Literal["revoke_supplement"]


class SteerQueued(Command):
    action: Literal["steer_queued"]
    path: NonBlank
    task_id: NonBlank
    queued_task_id: NonBlank


class SetConversationApproval(Command):
    action: Literal["set_conversation_approval"]
    path: NonBlank | None = None
    approval_mode: ApprovalMode


class SetTurnReviewer(Command):
    action: Literal["set_turn_reviewer"]
    path: NonBlank | None = None
    task_id: NonBlank
    reviewer: Literal["user", "assistant"]


class New(Command):
    action: Literal["new"]


class SetProjectTrust(Command):
    action: Literal["set_project_trust"]
    root: NonBlank
    trusted: bool = Field(strict=True)


class ListConversations(Command):
    action: Literal["list"]
    query: str = ""


class Open(Command):
    action: Literal["open"]
    path: NonBlank


class ImportConversation(Command):
    action: Literal["import"]
    path: NonBlank


class Stop(Command):
    action: Literal["stop"]
    path: NonBlank | None = None


class Snapshot(Command):
    action: Literal["snapshot"]


class GetCompactionEvidence(Command):
    action: Literal["get_compaction_evidence"]
    path: NonBlank
    summary_id: NonBlank
    message_index: int = Field(ge=0, strict=True)
    part_index: int = Field(ge=0, strict=True)
    start: int = Field(default=0, ge=0, strict=True)


class StartNext(Command):
    action: Literal["start_next"]
    path: NonBlank | None = None


class ContinueTask(Command):
    action: Literal["continue_task"]
    path: NonBlank | None = None
    task_id: NonBlank
    request_limit: int = Field(ge=1, strict=True)
    active_seconds_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class Shutdown(Command):
    action: Literal["shutdown"]


class RecoveryCommand(Command):
    action: Literal["verify_recovery", "resume_task"]
    path: NonBlank | None = None
    task_id: NonBlank


class RetryDeniedAction(Command):
    action: Literal["retry_denied_action"]
    path: NonBlank
    task_id: NonBlank
    action_id: NonBlank


class ApproveAction(Command):
    action: Literal["approve_action"]
    path: NonBlank
    task_id: NonBlank
    action_id: NonBlank
    approval_id: NonBlank
    approved: bool = Field(strict=True)
    active_seconds_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class AcceptDelivery(Command):
    action: Literal["accept_delivery"]
    path: NonBlank
    task_id: NonBlank


class ListMemory(Command):
    action: Literal["memory_list"]


class MemorySettings(Command):
    action: Literal["memory_settings"]
    approve_writes: bool = Field(strict=True)


class ChangeMemory(Command):
    action: Literal["memory_change"]
    path: NonBlank | None = None
    changes: list[MemoryChange] = Field(min_length=1)
    project: NonBlank | None = None


commands = TypeAdapter(Annotated[Configure | RestoreConnections | ForgetConnection | FetchModelCatalog | GetModelControls | SelectModel | GetCompactionEvidence | PreviewProvider | DecideProvider | Send | SubmitSupplement | RevokeSupplement | SteerQueued | SetConversationApproval | SetTurnReviewer | SetProjectTrust | New | ListConversations | Open | ImportConversation | Stop | Snapshot | StartNext | ContinueTask | RecoveryCommand | RetryDeniedAction | ApproveAction | AcceptDelivery | ListMemory | MemorySettings | ChangeMemory | Shutdown, Field(discriminator="action")])


def emit(event: str, **data: object) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def visible_action(action: Action, *, include_preview: bool) -> dict[str, object]:
    payload = action.model_dump(mode="json")
    if include_preview and action.status == "awaiting_approval" and action.approval_preview is not None:
        payload["approval_preview"] = action.approval_preview
    return payload


@dataclass
class Session:
    conversation: Conversation
    running: asyncio.Task[None] | None = None
    budget: TaskBudget | None = None
    stopping: bool = False
    review_catalogs: dict[tuple[str, str, str, str], asyncio.Task[list[str]]] = field(default_factory=dict)
    applied: set[tuple[str, str]] = field(default_factory=set)
    approvals: dict[tuple[str, str], asyncio.Future[bool]] = field(default_factory=dict)
    published_compactions: list[dict[str, object]] = field(default_factory=list)

    async def approve(self, action: Action) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        identity = (action.id, action.approval_id or "")
        self.approvals[identity] = future
        try:
            return await future
        finally:
            self.approvals.pop(identity, None)

    def snapshot(self) -> dict[str, object]:
        return {
            "path": str(self.conversation.path),
            "turns": [{**turn.model_dump(mode="json", exclude={"compactions", "actions"}),
                       "actions": [visible_action(action, include_preview=(action.id, action.approval_id or "") in self.approvals)
                                   for action in turn.actions]} for turn in self.conversation.turns],
            "compactions": self.conversation.compaction_status(),
            "queue": [item.model_dump() for item in self.conversation.waiting_queue],
            "running": self.running is not None and not self.running.done(),
            "stopping": self.stopping,
            "recovery": self.conversation.recovery.model_dump() if self.conversation.recovery is not None and not (self.running is not None and not self.running.done()) else None,
            "earlier_recoveries": [report.model_dump() for report in self.conversation.earlier_recoveries],
            "save_failures": self.conversation.save_failures,
            "last_saved_at": self.conversation.last_saved_at,
            "approval_settings": self.conversation.approval_settings.model_dump(mode="json"),
            "model_selection": self.conversation.model_selection.model_dump(mode="json") if self.conversation.model_selection else None,
        }


async def serve(data_directory: Path, model_factory: ModelFactory, *, http_client: httpx2.AsyncClient | None = None,
                model_observer: Callable[[Model], Model] | None = None) -> None:
    default_path = data_directory / "settings" / "permissions.json"
    default_approval = ApprovalSettings.model_validate_json(default_path.read_text(encoding="utf-8")) if default_path.exists() else ApprovalSettings()
    trust_path = data_directory / "settings" / "projects.json"
    trusted_projects = TypeAdapter(list[str]).validate_json(trust_path.read_text(encoding="utf-8")) if trust_path.exists() else []
    memories = MemoryStore(data_directory / "memories")
    current = Session(Conversation(data_directory / f"{uuid4().hex}.json", approval_settings=default_approval))
    sessions = {str(current.conversation.path): current}
    key = ""
    selected_provider = ProviderConfig()
    provider_keys: dict[tuple[str, str, str], str] = {}
    delegation_max_turns = 250
    thinking = False
    shutting_down = False
    catalog_requests: set[asyncio.Task[None]] = set()

    def configured(request_id: str | None) -> None:
        emit("configured", configured=bool(key), approval_mode=default_approval.mode, trusted_projects=trusted_projects,
             provider_config=selected_provider.model_dump(mode="json"), request_limit=None, delegation_max_turns=delegation_max_turns,
             active_seconds_limit=None, thinking=thinking, **({"request_id": request_id} if request_id is not None else {}))

    def selection_for(session: Session) -> ModelSelection:
        if session.conversation.model_selection is not None:
            return resolve_selection(session.conversation.model_selection)
        previous = session.conversation.turns[-1] if session.conversation.turns else None
        config = previous.task.provider_config if previous and previous.task else selected_provider
        return resolve_selection(ModelSelection(provider_config=config))

    def controls(session: Session, selection: ModelSelection, *, request_id: str | None = None, error: str | None = None) -> None:
        emit("model_controls", path=str(session.conversation.path), selection=selection.model_dump(mode="json"),
             models=[model.model_dump(mode="json") for model in chat_models(selection.provider_config)], error=error,
             **({"request_id": request_id} if request_id is not None else {}))

    async def refresh_controls(session: Session, selection: ModelSelection, request_id: str | None) -> None:
        catalog_key = provider_keys.get(connection_identity(selection.provider_config), "")
        if not catalog_key:
            controls(session, selection, request_id=request_id, error="请先在设置中填写当前服务的连接密码，再刷新型号目录。")
            return
        try:
            models = await fetch_chat_models(catalog_key, selection.provider_config, http_client=http_client)
        except ValueError as error:
            controls(session, selection, request_id=request_id, error=str(error))
        else:
            emit("model_controls", path=str(session.conversation.path), selection=selection.model_dump(mode="json"),
                 models=[model.model_dump(mode="json") for model in models], error=None,
                 **({"request_id": request_id} if request_id is not None else {}))

    async def refresh_catalog(command: FetchModelCatalog, catalog_key: str) -> None:
        try:
            models = await fetch_model_catalog(catalog_key, command.provider_config, http_client=http_client)
        except ValueError as error:
            emit("model_catalog", request_id=command.request_id, models=[], error=str(error))
        else:
            emit("model_catalog", request_id=command.request_id, models=[model.model_dump() for model in models], error=None)

    def publish_compactions(session: Session) -> None:
        records = session.conversation.compaction_status()
        if records != session.published_compactions:
            session.published_compactions = records
            emit("compactions", compactions=records, path=str(session.conversation.path))

    def update(session: Session, event: Update) -> None:
        path = str(session.conversation.path)
        task_id = session.budget.task.id if session.budget is not None else None
        if event.kind == "action":
            assert event.action is not None
            emit("action", action=visible_action(event.action, include_preview=True), path=path, task_id=task_id)
        elif event.kind == "task":
            assert event.task is not None
            emit("task", task=event.task.model_dump(), path=path, task_id=task_id)
            for supplement in event.task.supplements:
                identity = (event.task.id, supplement.id)
                if supplement.applied_request is not None and identity not in session.applied:
                    session.applied.add(identity)
                    publish_supplement(session, event.task.id, supplement, saved=persist(session))
        elif event.kind == "delegation":
            assert event.delegation is not None
            emit("delegation", delegation=event.delegation.model_dump(), path=path, task_id=task_id)
        elif event.kind == "plan":
            emit("plan", plan=event.plan, path=path, task_id=task_id)
        elif event.kind == "skills":
            emit("skills", skills=[item.model_dump() for item in event.skills or []], path=path, task_id=task_id)
        elif event.kind == 'artifacts':
            emit('artifacts', artifacts=[item.model_dump() for item in event.artifacts or []],
                turn=session.conversation.turns[-1].model_dump(mode='json'), path=path, task_id=task_id)
        else:
            emit(event.kind, text=event.text, path=path, task_id=task_id)
        if event.kind == "status":
            publish_compactions(session)

    def persist(session: Session, **correlation: str) -> bool:
        try:
            session.conversation.save()
        except OSError:
            emit("error", path=str(session.conversation.path), message="本次进展未能保存；已有内容保留在当前窗口。" + session.conversation.save_failures[-1],
                 **correlation)
            return False
        return True

    def publish_supplement(session: Session, task_id: str, supplement: Supplement, *, saved: bool = True) -> None:
        state = "revoked" if supplement.revoked_at is not None else "applied" if supplement.applied_request is not None else "received"
        emit("supplement", path=str(session.conversation.path), task_id=task_id, supplement=supplement.model_dump(), state=state, saved=saved)

    def append_supplement(session: Session, task: Task, supplement: Supplement) -> None:
        task.supplements.append(supplement)
        if persist(session, task_id=task.id, supplement_id=supplement.id):
            publish_supplement(session, task.id, supplement)
        else:
            task.supplements.pop()

    def start(session: Session, item: QueuedTask, *, queued: bool = False, verified_recovery: RecoveryReport | None = None) -> None:
        task_key = provider_keys.get(connection_identity(item.task.provider_config))
        if not task_key:
            emit("error", path=str(session.conversation.path), message="原工作所选接收方尚无本次连接密码；请在可信设置中填写对应地址和型号，不会借用另一家密码。")
            return
        if queued:
            try:
                item = session.conversation.prepare_next_queued(verified_recovery)
            except ValueError as error:
                emit("error", path=str(session.conversation.path), message=str(error))
                return
            emit("queue", path=str(session.conversation.path), queue=[following.model_dump() for following in session.conversation.waiting_queue])
        session.stopping = False
        budget = TaskBudget(item.task)
        session.budget = budget
        emit("started", prompt=item.prompt or item.task.goal, path=str(session.conversation.path), task_id=item.task.id, task=item.task.model_dump())
        if item.task.reasoning is None and item.task.legacy_thinking is None:
            previous = next((turn for turn in reversed(session.conversation.turns) if turn.task and turn.task.id == item.task.id), None)
            item.task.legacy_thinking = previous.thinking if previous is not None else thinking
        session.running = asyncio.create_task(answer(session, item, budget, task_key, item.task.legacy_thinking or False,
            project_trusted=item.task.scope is not None and str(Path(item.task.scope.root).resolve()) in trusted_projects))

    async def verify_queue_recovery(conversation: Conversation) -> RecoveryReport | None:
        if not conversation.queue or not provider_keys.get(connection_identity(conversation.queue[0].task.provider_config)):
            return None
        recovery = conversation.recovery
        if recovery is not None and recovery.can_resume:
            original = conversation.recovery_turn(recovery.task_id)
            if original is not None and original.status == "failed" and original.service_failure:
                return await conversation.verify_recovery_async(recovery.task_id)
        return None

    async def answer(session: Session, item: QueuedTask, budget: TaskBudget, task_key: str, task_thinking: bool, *, project_trusted: bool) -> None:
        conversation = session.conversation
        before = len(conversation.turns)
        saved = False
        turn: Turn
        try:
            connection = model_factory(task_key) if item.task.provider_config == ProviderConfig() else configured_model(task_key, item.task.provider_config, http_client=http_client)
            async with AsyncExitStack() as stack:
                model = await stack.enter_async_context(connection)
                if model_observer is not None:
                    model = model_observer(model)
                async def resolve_reviewer() -> Model:
                    config = item.task.provider_config
                    if (config.preferred_review_model == "codex-auto-review" and config.review_model_override is None
                            and config.available_models is None and config.model_catalog_url is None and not config.basic_session_source):
                        if config.identity not in session.review_catalogs:
                            session.review_catalogs[config.identity] = asyncio.create_task(fetch_review_catalog(task_key, config))
                        catalog = await asyncio.shield(session.review_catalogs[config.identity])
                        config = config.model_copy(update={"available_models": catalog})
                    if config.review_model_name == item.task.provider_config.model:
                        return model
                    review_config = config.model_copy(update={"model": config.review_model_name})
                    reviewer = await stack.enter_async_context(configured_model(task_key, review_config, http_client=http_client))
                    return model_observer(reviewer) if model_observer is not None else reviewer
                turn = await conversation.ask(
                    item.prompt or item.task.goal, document=Path(item.document) if item.document else None,
                    model=model, on_update=lambda event: update(session, event),
                    cancellation_token=budget.token, thinking=task_thinking, reasoning=item.task.reasoning,
                    request_limit=item.task.request_limit, scope=item.task.scope,
                    task=item.task, budget=budget,
                    approve_action=session.approve, review_model_resolver=resolve_reviewer,
                    project_trusted=project_trusted,
                    _prompt_origin=item.prompt_origin,
                )
                saved = True
        except Exception as error:
            needs_save = saved or len(conversation.turns) == before
            if len(conversation.turns) > before:
                turn = conversation.turns[-1]
            else:
                turn = conversation.record_failure(item.task.goal, thinking=task_thinking,
                    error=f"无法建立回答服务连接；错误种类：{type(error).__name__}（程序报告的原因名称）。请检查连接密码和网络，等待你手动继续。")
            turn.task = item.task
            turn.status = "failed"
            if turn.error is None:
                turn.error = "回答服务连接未能正常结束，请检查网络后重试。"
            if needs_save:
                saved = persist(session)
                if not saved:
                    turn.error = f"{turn.error} 本次结果未能保存；已有内容保留在当前窗口，请检查记录位置。"
        if conversation.release_queued_start(item.task.id):
            emit("queue", path=str(conversation.path), queue=[following.model_dump() for following in conversation.waiting_queue])
        verified_recovery = None
        if saved and turn.status == "failed" and turn.service_failure and not (session.stopping or shutting_down):
            try:
                verified_recovery = await verify_queue_recovery(conversation)
            except (OSError, ValueError):
                emit("error", path=str(conversation.path), message="原工作的核查结果未能保存；排队工作已保留。")
        advance = saved and not (session.stopping or shutting_down) and (
            turn.status == "completed" or verified_recovery is not None and verified_recovery.can_resume)
        session.stopping = False
        session.budget = None
        publish_compactions(session)
        recovery = conversation.recovery if saved and (turn.service_failure or turn.blocked_attachment_resume) else None
        emit("finished", turn=turn.model_dump(mode="json", exclude={"compactions"}), saved=saved,
             path=str(conversation.path), task_id=item.task.id, recovery=recovery.model_dump() if recovery else None,
             **({"earlier_recoveries": [report.model_dump() for report in conversation.earlier_recoveries]} if saved else {}))
        if verified_recovery is not None:
            emit("recovery", path=str(conversation.path), recovery=verified_recovery.model_dump(), saved=True)
        if advance and conversation.queue:
            start(session, conversation.queue[0], queued=True, verified_recovery=verified_recovery)

    async def manage_memory(session: Session, command: ChangeMemory, budget: TaskBudget) -> None:
        saved = False
        try:
            turn = await session.conversation.change_memory(command.changes, project=command.project, task=budget.task, budget=budget,
                                                          approve_action=session.approve, on_update=lambda event: update(session, event))
            saved = True
        except (OSError, ValueError):
            turn = session.conversation.turns[-1]
            turn.status, turn.error = "failed", "事实笔记未能保存；已有原件保留，请检查笔记位置。"
        session.budget = None
        session.stopping = False
        publish_compactions(session)
        emit("finished", turn=turn.model_dump(mode="json", exclude={"compactions"}), saved=saved, path=str(session.conversation.path), task_id=budget.task.id)

    async def shutdown() -> bool:
        nonlocal shutting_down
        shutting_down = True
        for pending_catalog in catalog_requests:
            pending_catalog.cancel()
        await asyncio.gather(*catalog_requests, return_exceptions=True)
        active = []
        for session in sessions.values():
            if session.budget is not None:
                session.stopping = True
                emit("stopping", path=str(session.conversation.path), task_id=session.budget.task.id)
                session.budget.token.cancel()
                for future in session.approvals.values():
                    if not future.done():
                        future.set_result(False)
            if session.running is not None:
                active.append(session.running)
        await asyncio.gather(*active)
        saved = [persist(session) for session in sessions.values() if session.conversation.turns or session.conversation.queue]
        if not all(saved):
            emit("shutdown_blocked", message="退出未完成：部分进展未能保存；已有内容仍保留。请修复记录位置后再次点击退出软件。")
            shutting_down = False
            return False
        return True

    emit("ready", path=str(current.conversation.path))
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            await shutdown()
            return
        try:
            command = commands.validate_json(line)
        except ValidationError:
            emit("error", message="无法识别这条操作，请重新操作。")
            continue
        if isinstance(command, Shutdown):
            if await shutdown():
                key = ""
                emit("closed")
                return
            continue
        if isinstance(command, RestoreConnections):
            provider_keys.clear()
            provider_keys.update((connection_identity(item.provider_config), item.key) for item in command.connections)
            if command.selected_provider is not None:
                selected_provider = command.selected_provider
            key = provider_keys.get(connection_identity(selected_provider), "")
            configured(command.request_id)
            continue
        if isinstance(command, ForgetConnection):
            provider_keys.pop(connection_identity(command.provider_config), None)
            key = provider_keys.get(connection_identity(selected_provider), "")
            configured(command.request_id)
            continue
        if isinstance(command, FetchModelCatalog):
            connection = command.provider_config
            catalog_key = command.key or provider_keys.get((connection.name, connection.base_url.rstrip("/"), connection.protocol), "")
            if not catalog_key:
                emit("model_catalog", request_id=command.request_id, models=[], error="请先填写 Gemini 的连接密码，再读取模型目录。")
            else:
                pending_catalog = asyncio.create_task(refresh_catalog(command, catalog_key))
                catalog_requests.add(pending_catalog)
                pending_catalog.add_done_callback(catalog_requests.discard)
            continue
        if isinstance(command, SetProjectTrust):
            try:
                root = Path(command.root).resolve(strict=True)
                if not root.is_dir():
                    raise ValueError("请选择实际存在的任务文件夹。")
                candidate_projects = sorted({*trusted_projects, str(root)} if command.trusted else set(trusted_projects) - {str(root)})
                trust_path.parent.mkdir(parents=True, exist_ok=True)
                pending_trust = trust_path.with_suffix(".tmp")
                pending_trust.write_text(json.dumps(candidate_projects, ensure_ascii=False), encoding="utf-8")
                pending_trust.replace(trust_path)
            except (OSError, ValueError):
                emit("error", message="项目信任未能保存，原选择保持；请检查文件夹和软件资料位置。")
            else:
                trusted_projects = candidate_projects
                emit("project_trust", root=str(root), trusted=command.trusted, saved=True)
            continue
        if isinstance(command, (ListMemory, MemorySettings)):
            try:
                if isinstance(command, MemorySettings):
                    record = memories.read()
                    record.approve_writes = command.approve_writes
                    memories.save(record)
                emit("memory", **memories.state())
            except (OSError, ValueError):
                emit("error", message="长期记忆未能读取或保存；原记录保留，请检查笔记位置。")
            continue
        if isinstance(command, Snapshot):
            emit("snapshot", configured=bool(key), approval_mode=default_approval.mode, trusted_projects=trusted_projects, provider_config=selected_provider.model_dump(), request_limit=None, delegation_max_turns=delegation_max_turns, active_seconds_limit=None, thinking=thinking, active_path=str(current.conversation.path), conversations=[session.snapshot() for session in sessions.values()])
            continue
        session = current
        if isinstance(command, (Stop, Send, SupplementCommand, SteerQueued, GetModelControls, SelectModel, GetCompactionEvidence, SetConversationApproval, SetTurnReviewer, StartNext, ContinueTask, RecoveryCommand, PreviewProvider, DecideProvider, RetryDeniedAction, ApproveAction, AcceptDelivery, ChangeMemory)):
            selected_session = current if command.path is None else sessions.get(command.path)
            if selected_session is None:
                correlation = {"task_id": command.task_id, "supplement_id": command.supplement_id} if isinstance(command, SupplementCommand) else {}
                emit("error", message="会话尚未打开，请先打开对应会话。", path=command.path, **correlation)
                continue
            session = selected_session
        if isinstance(command, SteerQueued):
            correlation = {"task_id": command.task_id, "supplement_id": command.queued_task_id}
            budget = session.budget
            if session.stopping or budget is None or not budget.accepting_supplements or budget.task.id != command.task_id:
                emit("error", path=command.path, message="当前工作已变化或无法接收引导；排队消息保留。", **correlation)
                continue
            active_task = budget.task
            queue = session.conversation.queue
            waiting_queue = session.conversation.waiting_queue
            first_waiting = len(queue) - len(waiting_queue)
            entry = next(((index, item) for index, item in enumerate(waiting_queue, first_waiting) if item.task.id == command.queued_task_id), None)
            existing = next((item for item in active_task.supplements if item.id == command.queued_task_id), None)
            if entry is None:
                if existing is not None:
                    publish_supplement(session, active_task.id, existing)
                else:
                    emit("error", path=command.path, message="找不到这条排队消息，未发送引导。", **correlation)
                continue
            index, item = entry
            if (existing is not None or item.task.provider_config != active_task.provider_config or item.task.approval != active_task.approval
                    or item.task.scope is not None and item.task.scope != active_task.scope
                    or item.task.document is not None and item.task.document != active_task.document):
                emit("error", path=command.path, message="这条消息使用独立的资料、服务或权限；仍留在队列中，未发送引导。", **correlation)
                continue
            supplement = Supplement(id=item.task.id, text=item.prompt or item.task.goal)
            queue.pop(index)
            active_task.supplements.append(supplement)
            if persist(session, **correlation):
                publish_supplement(session, active_task.id, supplement)
                emit("queue", path=command.path, queue=[following.model_dump() for following in session.conversation.waiting_queue])
            else:
                active_task.supplements.pop()
                queue.insert(index, item)
            continue
        if isinstance(command, (SubmitSupplement, RevokeSupplement)):
            correlation = {"task_id": command.task_id, "supplement_id": command.supplement_id}
            budget = session.budget
            live_task = budget.task if budget is not None else None
            latest = session.conversation.turns[-1] if session.conversation.turns else None
            task = live_task if live_task is not None and live_task.id == command.task_id else next(
                (turn.task for turn in reversed(session.conversation.turns)
                 if turn.request_owner_id is None and turn.task is not None and turn.task.id == command.task_id), None)
            existing = next((item for item in task.supplements if item.id == command.supplement_id), None) if task is not None else None
            if isinstance(command, SubmitSupplement):
                if existing is not None:
                    if existing.text == command.prompt:
                        publish_supplement(session, command.task_id, existing)
                    else:
                        emit("error", path=command.path, message="该补充编号已用于另一段正文。", **correlation)
                elif not session.stopping and budget is not None and budget.accepting_supplements and budget.task.id == command.task_id:
                    append_supplement(session, budget.task, Supplement(id=command.supplement_id, text=command.prompt))
                else:
                    emit("error", path=command.path, message="这项工作当前无法接收补充，草稿保留。", **correlation)
            elif existing is None:
                emit("error", path=command.path, message="找不到这条补充。", **correlation)
            elif existing.revoked_at is not None:
                publish_supplement(session, command.task_id, existing)
            elif existing.selected or existing.applied_request is not None:
                emit("error", path=command.path, message="这条补充已进入请求准备，无法撤回。", **correlation)
            elif (session.stopping or latest is None or latest.task is not task or latest.status == "completed"
                  or (live_task is not None and live_task is not task)):
                emit("error", path=command.path, message="只能撤回当前未完工作中的补充。", **correlation)
            else:
                existing.revoked_at = datetime.now(timezone.utc).isoformat()
                if persist(session, **correlation):
                    publish_supplement(session, command.task_id, existing)
                else:
                    existing.revoked_at = None
            continue
        if isinstance(command, GetCompactionEvidence):
            try:
                text = session.conversation.read_compaction_source(command.summary_id, command.message_index, command.part_index, command.start, 8000)
                more = session.conversation.read_compaction_source(command.summary_id, command.message_index, command.part_index, command.start + len(text), 1)
            except ValueError as error:
                emit("compaction_evidence", path=command.path, summary_id=command.summary_id, message_index=command.message_index, part_index=command.part_index, start=command.start, text="", next_start=None, error=str(error))
            else:
                emit("compaction_evidence", path=command.path, summary_id=command.summary_id, message_index=command.message_index, part_index=command.part_index, start=command.start, text=text, next_start=command.start + len(text) if more else None, error=None)
            continue
        if isinstance(command, (GetModelControls, SelectModel)):
            selected = selection_for(session)
            try:
                if isinstance(command, SelectModel):
                    if session.running is not None and not session.running.done():
                        raise ValueError("当前聊天仍在运行，本轮型号与推理强度保持原设置。")
                    changes: dict[str, object] = {"model": command.model}
                    if command.model != selected.provider_config.model:
                        changes.update(context_window_override=None, input_token_limit_override=None)
                    selected = validate_selection(ModelSelection(provider_config=selected.provider_config.model_copy(update=changes), reasoning=command.reasoning))
                    session.conversation.check_model_history(selected.provider_config)
                    session.conversation.set_model_selection(selected)
                elif selected != session.conversation.model_selection:
                    session.conversation.set_model_selection(selected, persist=False)
            except (OSError, ValueError) as error:
                controls(session, selection_for(session), request_id=command.request_id, error=str(error))
                continue
            if isinstance(command, GetModelControls) and command.refresh:
                pending_controls = asyncio.create_task(refresh_controls(session, selected, command.request_id))
                catalog_requests.add(pending_controls)
                pending_controls.add_done_callback(catalog_requests.discard)
            else:
                controls(session, selected, request_id=command.request_id)
            continue
        if isinstance(command, SetConversationApproval):
            try:
                approval = session.conversation.set_approval_mode(command.approval_mode)
            except OSError:
                emit("error", path=str(session.conversation.path), message="聊天批准方式未能保存，请检查记录位置后重试。")
            else:
                emit("conversation_approval", path=str(session.conversation.path), approval_settings=approval.model_dump(mode="json"), saved=True)
            continue
        if isinstance(command, SetTurnReviewer):
            try:
                if session.running is None or session.running.done() or session.stopping:
                    raise ValueError("只能为当前仍在运行的工作修改后续新步骤的审核者。")
                approval = session.conversation.set_turn_reviewer(command.task_id, command.reviewer)
            except (OSError, ValueError) as error:
                emit("error", path=str(session.conversation.path), task_id=command.task_id, message=str(error))
            else:
                emit("turn_approval", path=str(session.conversation.path), task_id=command.task_id, approval_settings=approval.model_dump(mode="json"), saved=True)
            continue
        if isinstance(command, (PreviewProvider, DecideProvider)):
            if session.running is not None and not session.running.done():
                emit("error", path=str(session.conversation.path), message="请先等待原工作收尾，再审阅或更换资料接收方。")
                continue
            try:
                if isinstance(command, PreviewProvider):
                    change = session.conversation.preview_provider_change(command.task_id, command.provider_config)
                    emit("provider_preview", path=str(session.conversation.path), task_id=command.task_id, change=change.model_dump())
                else:
                    task = session.conversation.decide_provider_change(command.change_id, command.approved)
                    emit("provider_decided", path=str(session.conversation.path), approved=command.approved)
                    if task is not None:
                        task = session.conversation.prepare_resume(task.id)
                        start(session, QueuedTask(task=task, document=task.document, prompt="用户明确选择继续原工作并批准所列接收方；只做尚未完成的事项，不重复已完成动作。", prompt_origin="recovery_control"))
            except (OSError, ValueError) as error:
                emit("error", path=str(session.conversation.path), message=str(error))
            continue
        if isinstance(command, ChangeMemory):
            if session.running is not None and not session.running.done() or session.conversation.recovery is not None:
                emit("error", message="请等待当前工作收尾；原工作待核查时先核查，或新建聊天管理事实笔记。", path=str(session.conversation.path))
                continue
            previous = session.conversation.turns[-1] if session.conversation.turns else None
            task = Task(goal="管理所选长期事实笔记", scope=None, request_limit=None, max_turns=None, delegation_max_turns=delegation_max_turns,
                        provider_config=previous.task.provider_config if previous and previous.task else selected_provider)
            session.budget = TaskBudget(task)
            emit("started", prompt=task.goal, path=str(session.conversation.path), task_id=task.id, task=task.model_dump())
            session.running = asyncio.create_task(manage_memory(session, command, session.budget))
            continue
        if isinstance(command, RecoveryCommand):
            if session.running is not None and not session.running.done():
                emit("error", path=str(session.conversation.path), message="请先等待本项工作收尾，再核查或续办。")
                continue
            try:
                conversation = session.conversation
                if command.action == "verify_recovery":
                    report = await conversation.verify_recovery_async(command.task_id)
                    current_recovery = conversation.recovery
                    emit("recovery", path=str(conversation.path), recovery=report.model_dump(), saved=True,
                         earlier=current_recovery is None or current_recovery.task_id != report.task_id)
                else:
                    previous = conversation.recovery_turn(command.task_id)
                    if previous is not None and previous.task is not None and not provider_keys.get(connection_identity(previous.task.provider_config)):
                        emit("error", path=str(session.conversation.path), message="请先设置原工作所用连接的密码，再由你选择继续。")
                        continue
                    task = conversation.prepare_resume(command.task_id)
                    start(session, QueuedTask(task=task, document=task.document, prompt="用户明确选择继续原工作；只做尚未完成的事项，不重复已完成动作，旧批准不能用于新动作。", prompt_origin="recovery_control"))
            except (OSError, ValueError) as error:
                emit("error", path=str(session.conversation.path), message=str(error))
            continue
        if isinstance(command, ApproveAction):
            future = session.approvals.get((command.action_id, command.approval_id))
            if session.running is None or session.running.done():
                try:
                    await session.conversation.approve_memory_proposal(task_id=command.task_id, action_id=command.action_id,
                        approval_id=command.approval_id, approved=command.approved, active_seconds_limit=None, on_update=lambda event: update(session, event))
                    emit("memory", **memories.state())
                except (ValueError, OSError, UsageLimitExceeded, ModelRetry) as error:
                    emit("error", path=command.path, task_id=command.task_id, message=str(error))
            elif session.budget is None or session.budget.task.id != command.task_id or future is None or future.done() or session.stopping:
                emit("error", path=command.path, task_id=command.task_id, message="这份答复不对应当前待批准动作，未执行。")
            else:
                future.set_result(command.approved)
            continue
        if isinstance(command, AcceptDelivery):
            delivered = next((turn for turn in reversed(session.conversation.turns) if turn.task is not None and turn.task.id == command.task_id and turn.delivery_status == "awaiting_acceptance"), None)
            if delivered is None or session.running is not None and not session.running.done():
                emit("error", path=command.path, message="请先等待当前处理结束，并选择确实待验收的成果。")
            else:
                delivered.delivery_status = "accepted"
                saved = persist(session)
                emit("delivery", path=command.path, task_id=command.task_id, turn=delivered.model_dump(mode="json", exclude={"compactions"}), saved=saved)
            continue
        if isinstance(command, Stop):
            if session.budget is not None:
                session.stopping = True
                emit("stopping", path=str(session.conversation.path), task_id=session.budget.task.id)
                session.budget.token.cancel()
                for future in session.approvals.values():
                    if not future.done():
                        future.set_result(False)
            continue
        if isinstance(command, Configure):
            correlation = {"request_id": command.request_id} if command.request_id is not None else {}
            if command.approval_mode is not None:
                candidate = ApprovalSettings(mode=command.approval_mode)
                try:
                    default_path.parent.mkdir(parents=True, exist_ok=True)
                    pending = default_path.with_suffix(".tmp")
                    pending.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
                    pending.replace(default_path)
                except OSError:
                    emit("error", message="默认批准方式未能保存；原设置保持，请检查软件资料位置。", **correlation)
                    continue
                default_approval = candidate
                if command.model_fields_set <= {"action", "approval_mode", "request_id"}:
                    configured(command.request_id)
                    continue
            candidate_provider = command.provider_config or selected_provider
            candidate_key = command.key if command.key is not None else provider_keys.get(connection_identity(candidate_provider), "")
            if not candidate_key:
                emit("error", message="请先在设置中填写本次使用的连接密码。", **correlation)
                continue
            if current.running is None or current.running.done():
                next_selection = current.conversation.model_selection
                if "thinking" in command.model_fields_set:
                    next_selection = None
                elif next_selection is None or command.provider_config is not None and candidate_provider != next_selection.provider_config:
                    next_selection = resolve_selection(ModelSelection(provider_config=candidate_provider))
                elif next_selection is not None:
                    next_selection = resolve_selection(next_selection)
                if next_selection != current.conversation.model_selection:
                    try:
                        current.conversation.set_model_selection(next_selection,
                            persist=bool(current.conversation.turns or current.conversation.queue or current.conversation.path.exists()))
                    except OSError:
                        emit("error", message="连接选择未能保存；原设置保持，请检查聊天记录位置。", **correlation)
                        continue
            if command.key is not None:
                provider_keys[connection_identity(candidate_provider)] = candidate_key
            selected_provider, key = candidate_provider, candidate_key
            delegation_max_turns, thinking = command.delegation_max_turns, command.thinking
            configured(command.request_id)
        elif isinstance(command, New):
            created = Session(Conversation(data_directory / f"{uuid4().hex}.json", approval_settings=default_approval))
            created.conversation.set_model_selection(resolve_selection(ModelSelection(provider_config=selected_provider)), persist=False)
            current = created
            sessions[str(current.conversation.path)] = current
            emit("conversation", **current.snapshot())
        elif isinstance(command, ListConversations):
            items: list[dict[str, str]] = []
            unreadable = 0
            try:
                paths = set(data_directory.glob("*.json")) | {session.conversation.path for session in sessions.values() if session.conversation.turns or session.conversation.queue}
                for path in sorted(paths, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
                    try:
                        stored = sessions[str(path)].conversation if str(path) in sessions else Conversation.open(path)
                    except (OSError, ValueError):
                        unreadable += 1
                        continue
                    title = " ".join(stored.turns[0].prompt.split())[:24] if stored.turns else "新会话"
                    if command.query.strip().casefold() in title.casefold():
                        items.append({"path": str(path), "title": title})
            except OSError:
                emit("error", message="无法读取会话目录，请检查记录位置后重试。")
                continue
            emit("conversations", items=items, unreadable=unreadable)
        elif isinstance(command, (Open, ImportConversation)):
            path = Path(command.path)
            if isinstance(command, Open) and (path.resolve().parent != data_directory.resolve() or path.suffix.lower() != ".json"):
                emit("error", message="请从本机会话列表打开记录；旧记录请使用导入。")
                continue
            if isinstance(command, Open) and str(path) in sessions:
                current = sessions[str(path)]
            else:
                try:
                    opened = Conversation.open(path)
                except (OSError, ValueError):
                    emit("error", message="无法打开这份记录：文件损坏、格式不受支持或无法读取；原文件未修改。")
                    continue
                if isinstance(command, ImportConversation):
                    destination = data_directory / f"{uuid4().hex}.json"
                    try:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
                    except OSError:
                        emit("error", message="未能复制导入记录，请检查软件记录目录；原文件未修改。")
                        continue
                    opened.path = destination
                current = Session(opened)
                sessions[str(opened.path)] = current
            emit("conversation", **current.snapshot())
        elif isinstance(command, RetryDeniedAction):
            if session.running is not None and not session.running.done():
                emit("error", path=str(session.conversation.path), message="请先设置原服务连接并等待本轮结束，再选择一项拒绝重新审核。")
                continue
            previous = session.conversation.turns[-1] if session.conversation.turns else None
            if previous is not None and previous.task is not None and not provider_keys.get(connection_identity(previous.task.provider_config)):
                emit("error", path=str(session.conversation.path), message="请先设置原工作所用连接的密码，再选择重新审核。")
                continue
            try:
                task = session.conversation.authorize_denied_retry(command.task_id, command.action_id)
            except (ValueError, OSError) as error:
                emit("error", path=str(session.conversation.path), message=str(error))
                continue
            start(session, QueuedTask(task=task, document=task.document, prompt="本人已看过所选动作的具体风险，只授权原确切动作在同一背景下独立再审一次。请按已保存参数准备，不直接执行，也不扩展授权。"))
        elif isinstance(command, ContinueTask):
            if session.running is not None and not session.running.done():
                emit("error", path=str(session.conversation.path), message="请先设置连接并等待当前工作收尾。")
                continue
            conversation = session.conversation
            interrupted = conversation.recovery_turn(command.task_id)
            previous = interrupted or (conversation.turns[-1] if conversation.turns and conversation.turns[-1].status == "limited" else None)
            if previous is None or previous.task is None or previous.task.id != command.task_id:
                emit("error", path=str(session.conversation.path), message="只能增加当前已到限工作的额度；请先核对对应工作。")
                continue
            task = previous.task
            if not provider_keys.get(connection_identity(task.provider_config)):
                emit("error", path=str(session.conversation.path), message="请先设置原工作所用连接的密码，再选择续办。")
                continue
            try:
                if interrupted is not None:
                    task = conversation.prepare_resume(task.id, request_limit=command.request_limit)
                else:
                    task.validate_continuation_request_limit(command.request_limit)
                    task.request_limit = command.request_limit
            except (OSError, ValueError) as error:
                emit("error", path=str(conversation.path), message=str(error))
                continue
            task.active_seconds_limit = None
            start(session, QueuedTask(task=task, document=task.document, prompt="用户明确选择继续原工作；保留已有成果，只做尚未完成的事项。", prompt_origin="recovery_control"))
        elif isinstance(command, StartNext):
            if session.running is not None and not session.running.done():
                emit("error", path=str(session.conversation.path), message="当前工作仍在收尾，请稍后再启动排队工作。")
            elif session.conversation.queue:
                conversation = session.conversation
                following = conversation.queue[0]
                try:
                    verified_recovery = await verify_queue_recovery(conversation)
                except (OSError, ValueError):
                    emit("error", path=str(conversation.path), message="原工作的核查结果未能保存；排队工作已保留。")
                    continue
                start(session, following, queued=True, verified_recovery=verified_recovery)
        elif isinstance(command, Send):
            running = session.running is not None and not session.running.done()
            selection = session.conversation.model_selection
            if selection is not None and (not running or command.new_task):
                selection = resolve_selection(selection)
            if command.model_selection is not None:
                expected = selection_for(session).provider_config
                model_candidate = command.model_selection
                if model_candidate.provider_config != expected.model_copy(update={"model": model_candidate.provider_config.model}):
                    emit("error", path=str(session.conversation.path), message="消息中的服务连接与当前聊天不符；请先在设置中选择连接，没有发送资料。")
                    continue
                try:
                    selection = validate_selection(model_candidate)
                except ValueError as error:
                    emit("error", path=str(session.conversation.path), message=str(error))
                    continue
                if running and not command.new_task and session.budget is not None and (selection.provider_config != session.budget.task.provider_config or selection.reasoning != session.budget.task.reasoning):
                    emit("error", path=str(session.conversation.path), message="补充消息不能改变本轮型号与推理强度；请等待本轮结束后再选择。")
                    continue
            task_provider = (session.budget.task.provider_config if running and not command.new_task and session.budget is not None
                             else selection.provider_config if selection is not None else selected_provider)
            if not (running and not command.new_task) and not provider_keys.get(connection_identity(task_provider)):
                emit("error", path=str(session.conversation.path), message="请先在设置中填写当前聊天所用连接的密码。")
                continue
            previous = session.conversation.turns[-1] if session.conversation.turns else None
            if previous is not None and not command.new_task and (session.running is None or session.running.done()):
                if previous.request_owner_id is not None:
                    emit("error", path=str(session.conversation.path), message="这份子助理记录供回查；请回原主聊天继续办理，或明确另办新工作，不能把原分工变成无上限主助理。")
                    continue
                if previous.task is not None and previous.task.request_policy == "shared" and previous.status == "limited":
                    emit("error", path=str(session.conversation.path), message="旧记录已达到原共用请求上限；请明确增加旧记录次数后续办，或明确另办新工作，已用次数不会清零。")
                    continue
            if previous is not None and previous.task is not None and previous.task.provider_config != task_provider and not (command.new_task and previous.status == "completed"):
                try:
                    session.conversation.check_model_history(task_provider)
                except ValueError as error:
                    emit("error", path=str(session.conversation.path), message=str(error))
                    continue
            if command.scope is not None and command.scope.provider != task_provider.recipient:
                emit("error", path=str(session.conversation.path), message="任务接收方与当前服务设置不符；请重新审阅范围和接收地址，没有发送资料。")
                continue
            if session.conversation.recovery is not None and not (session.running is not None and not session.running.done()):
                emit("error", path=str(session.conversation.path), message="这项工作曾意外中断；请先核查进展并选择继续，原工作不会自动重做。")
                continue
            if session.running is not None and not session.running.done() and not command.new_task:
                if session.stopping or session.budget is None or not session.budget.accepting_supplements:
                    emit("error", path=str(session.conversation.path), message="这项工作当前无法接收补充，草稿保留。")
                    continue
                if command.document is not None or command.scope is not None or command.approval_mode is not None:
                    emit("error", path=str(session.conversation.path), message="补充不能变更本次允许范围；新范围请作为新工作排队。")
                    continue
                append_supplement(session, session.budget.task, Supplement(text=command.prompt))
                continue
            if command.document is not None and command.scope is not None:
                emit("error", message="请为本次任务选择单份资料或任务文件夹。")
                continue
            if command.scope is not None and not Path(command.scope.root).is_dir():
                emit("error", message="任务文件夹不存在或无法读取，请重新选择。")
                continue
            document = None
            if command.document is not None:
                try:
                    document_path = Path(command.document).resolve(strict=True)
                    supported = document_path.is_file() and document_path.suffix.lower() in {".txt", ".md"}
                except (OSError, ValueError):
                    supported = False
                if not supported:
                    emit("error", message="无法读取所选资料；请确认文件仍存在、可读取，且是 .txt（普通文本）或 .md（带简易排版标记的文本）文件。")
                    continue
                document = str(document_path)
            continuing = previous is not None and previous.task is not None and not command.new_task and previous.status != "completed"
            if command.approval_mode is not None:
                try:
                    approval = session.conversation.set_approval_mode(command.approval_mode)
                except OSError:
                    emit("error", path=str(session.conversation.path), message="新轮批准方式未能保存；没有开始新工作，请检查记录位置。")
                    continue
                emit("conversation_approval", path=str(session.conversation.path), approval_settings=approval.model_dump(mode="json"), saved=True)
            if command.model_selection is not None:
                try:
                    session.conversation.set_model_selection(selection)
                except OSError:
                    emit("error", path=str(session.conversation.path), message="型号与推理强度未能保存；草稿保留，没有开始新工作。")
                    continue
            saved_approval = session.conversation.approval_settings
            if continuing:
                assert previous is not None and previous.task is not None
                original = previous.task
                continued = original.continuation(scope=command.scope, document=document, approval=saved_approval,
                    provider_config=task_provider, reasoning=selection.reasoning if selection is not None else None,
                    legacy_thinking=thinking if selection is None else None)
                item = QueuedTask(prompt=command.prompt, task=continued, document=continued.document)
            else:
                item = QueuedTask(task=Task(goal=command.prompt, scope=command.scope, request_limit=None, max_turns=None, delegation_max_turns=delegation_max_turns, active_seconds_limit=None, document=document, provider_config=task_provider,
                    reasoning=selection.reasoning if selection is not None else None, legacy_thinking=thinking if selection is None else None,
                    approval=saved_approval.model_copy(deep=True)), document=document)
            if session.running is not None and not session.running.done() or session.conversation.queue:
                session.conversation.queue.append(item)
                saved = persist(session)
                emit("queue", path=str(session.conversation.path), queue=[queued.model_dump() for queued in session.conversation.waiting_queue], saved=saved)
            else:
                start(session, item)


def main(*, data_directory: Path | None = None, model_factory: ModelFactory = deepseek_model) -> None:
    directory = data_directory or Path(
        os.environ.get("MYRK_DATA_DIRECTORY")
        or str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "Myrk" / "conversations")
    )
    asyncio.run(serve(directory, model_factory))


if __name__ == "__main__":
    main()
