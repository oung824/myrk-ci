"""Prepare request-only excerpts while keeping the library's original message history."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.main import IncEx
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import CapabilityInstructionSource, CompactionPart, InstructionBaselinePart, InstructionId, InstructionPart, ModelMessage, ModelRequest, ModelRequestPart, ModelResponse, ModelResponsePart, ToolReturnPart, UserPromptPart, repair_messages
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from myrk.compaction import (CompactionRecord, ContextLimitExceeded as ContextLimitExceeded, ContextPreparationError as ContextPreparationError, SourcePart, WindowUsage,
                             content_part, measure_request, part_hash, source_fingerprint, summarize, task_goal_instruction, transcript_sources)
from myrk.provider import ProviderConfig
from myrk.task_control import RequestAttempt, TaskBudget


CORE_TOOL_CONTRACT_VERSION = 2


class LayoutChange(BaseModel):
    version: int
    reasons: list[str]
    previous_core_version: int | None = Field(default=None, exclude_if=lambda value: value is None)
    core_version: int | None = Field(default=None, exclude_if=lambda value: value is None)
    changed_tools: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)


class PreviewSelection(BaseModel):
    content: str | None = None
    edge: int | None = None
    sent: bool = False


class RequestLayout(BaseModel):
    format_version: int = 1
    core_contract_version: int = CORE_TOOL_CONTRACT_VERSION
    version: int = 1
    task_id: str
    instructions: str
    tools: list[ToolDefinition] = Field(default_factory=list)
    tools_initialized: bool = False
    rewrite_instructions: list[InstructionPart] = Field(default_factory=list)
    observed_version: int = 0
    changes: list[LayoutChange] = Field(default_factory=list)
    boundary: dict[str, str] = Field(default_factory=dict)
    memory_block: str = ""
    previews: dict[str, PreviewSelection] = Field(default_factory=dict)
    compaction: CompactionRecord | None = None
    window_usage: WindowUsage | None = None

    def record_change(self, reasons: list[str]) -> None:
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            self.version += 1
            self.changes.append(LayoutChange(version=self.version, reasons=reasons))


class PreparedContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    turn_index: int
    task_id: str | None = None
    request_attempt: int | None = None
    messages: list[ModelMessage]
    instructions: list[str]
    tools: list[ToolDefinition]
    settings: ModelSettings | None
    omitted: list[str]
    input_bytes: int = 0
    original_bytes: int = 0
    limit_bytes: int = 128000
    accepted: bool = True
    input_tokens: int | None = None
    input_budget_tokens: int | None = None
    measurement_source: str = "unknown"
    request_key: str = ""


class RecoveryControlPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    source: SourcePart


class CurrentContext(AbstractCapability[None]):
    id = "myrk_current_context"

    def __init__(
        self,
        path: Path,
        turn_index: int,
        prepared: list[PreparedContext],
        notes: list[str],
        notify: Callable[[str], None],
        refresh_instructions: Callable[[str], str] | None = None,
        *, config: ProviderConfig | None = None, budget: TaskBudget | None = None,
        attempts: list[RequestAttempt] | None = None, persist: Callable[[], None] | None = None,
        source_messages: Callable[[], list[ModelMessage]] | None = None,
        source_task_ids: Callable[[], set[str]] | None = None,
        authorization: Callable[[], str] | None = None,
        task_goal: Callable[[], str] | None = None,
        before_commit: Callable[[RunContext[None], ModelRequestContext], None] | None = None,
        compactions: list[CompactionRecord] | None = None, delegation_id: str | None = None,
    ) -> None:
        self.path = path
        self.turn_index = turn_index
        self.prepared = prepared
        self.notes = notes
        self.notify = notify
        self.evidence: dict[str, Path] = {}
        self.refresh_instructions = refresh_instructions
        self.layout: RequestLayout | None = None
        self._previews: dict[str, PreviewSelection] = {}
        self.config = config or ProviderConfig()
        self.budget = budget
        self.attempts = attempts if attempts is not None else []
        self.persist = persist or (lambda: None)
        self.source_messages = source_messages
        self.source_task_ids = source_task_ids
        self.authorization = authorization or (lambda: "")
        self.task_goal = task_goal
        self.before_commit = before_commit
        self.compactions = compactions if compactions is not None else []
        self.delegation_id = delegation_id
        self.force_compaction = False

    def _apply_task_goal(self, request: ModelRequestContext) -> str | None:
        """Replace the application-owned data block before projection and counting."""
        goal = self.task_goal() if self.task_goal is not None else None
        parameters = request.model_request_parameters
        identity = InstructionId(CapabilityInstructionSource(self.id), name="task_goal")
        parts = [part for part in parameters.instruction_parts or [] if part.id != identity]
        if goal is not None:
            if self.layout is None:
                raise ContextPreparationError("任务目标缺少可核对的任务身份，未发送请求。")
            parts.append(InstructionPart(content=task_goal_instruction(self.layout.task_id, goal), id=identity))
        parameters.instruction_parts = parts
        return goal

    def _apply_output_reserve(self, request: ModelRequestContext) -> None:
        if request.model_settings is None:
            request.model_settings = {"max_tokens": self.config.output_token_reserve}
        elif "max_tokens" not in request.model_settings:
            request.model_settings = {**request.model_settings, "max_tokens": self.config.output_token_reserve}

    def _project(self, request_context: ModelRequestContext) -> PreparedContext:
        messages = deepcopy(list(request_context.messages))
        omitted: list[str] = []
        selections = self.layout.previews if self.layout is not None else self._previews
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            parts = list(message.parts)
            for index, part in enumerate(message.parts):
                if isinstance(part, ToolReturnPart) and part.tool_name != "read_context_evidence" and isinstance(part.content, str):
                    evidence_id = sha256(part.content.encode("utf-8")).hexdigest()
                    selection = selections.get(evidence_id)
                    if selection is not None and selection.content is not None:
                        parts[index] = replace(cast(ToolReturnPart[str], part), content=selection.content)
                        omitted.append(evidence_id)
            message.parts = parts
        return PreparedContext(
            turn_index=self.turn_index, messages=messages,
            instructions=[part.content for part in (request_context.model_request_parameters.instruction_parts or [])],
            tools=[*request_context.model_request_parameters.function_tools, *request_context.model_request_parameters.output_tools],
            settings=request_context.model_settings, omitted=omitted,
            limit_bytes=128000,
        )

    def _committed_projection(self, original: list[ModelMessage], projected: list[ModelMessage], previous: CompactionRecord) -> list[ModelMessage]:
        """Validate a durable view and apply only a verified preview replacement."""
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        from pydantic_core import to_json
        source_parts = [part for message in original for part in message.parts if content_part(part)]
        projected_parts = [part for message in projected for part in message.parts if content_part(part)]
        count = len(previous.source)
        if (previous.status != "completed" or source_fingerprint(previous.source) != previous.source_fingerprint
                or [part_hash(part) for part in source_parts[:count]] != [ref.sha256 for ref in previous.source]):
            raise ContextPreparationError("已提交摘要的原始来源指纹或顺序不符，已暂停；请恢复可靠记录。")
        if sha256(ModelMessagesTypeAdapter.dump_json(previous.view)).hexdigest() != previous.view_fingerprint:
            raise ContextPreparationError("已提交摘要的内容指纹不符，已暂停；请恢复可靠记录。")
        mapping = to_json([[ref.model_dump() for ref in previous.retained], [ref.model_dump() for ref in previous.covered]])
        source_by_key = {ref.key: ref for ref in previous.source}
        mapped = [*previous.retained, *previous.covered]
        if (sha256(mapping).hexdigest() != previous.mapping_fingerprint
                or len({ref.key for ref in mapped}) != len(mapped) or len(mapped) != count
                or any(source_by_key.get(ref.key) != ref for ref in mapped)):
            raise ContextPreparationError("已提交摘要的保留与替换来源映射不符，已暂停；请恢复可靠记录。")
        projected_by_key = {ref.key: part for ref, part in zip(previous.source, projected_parts)}
        original_by_key = {ref.key: part for ref, part in zip(previous.source, source_parts)}
        view = deepcopy(previous.view)
        retained_view = [part for message in view for part in message.parts if content_part(part) and not isinstance(part, CompactionPart)]
        if ([part.content for message in view for part in message.parts if isinstance(part, CompactionPart)] != [previous.summary]
                or len(retained_view) != len(previous.retained)):
            raise ContextPreparationError("已提交摘要与原始来源的部件对应不符，已暂停；原文仍保留。")
        selections = self.layout.previews if self.layout is not None else self._previews
        references = iter(previous.retained)
        for message in view:
            rewritten: list[ModelRequestPart | ModelResponsePart] = list(message.parts)
            for index, part in enumerate(message.parts):
                if not content_part(part) or isinstance(part, CompactionPart):
                    continue
                ref = next(references)
                expected = projected_by_key[ref.key]
                if part_hash(part) != part_hash(expected):
                    raw = original_by_key[ref.key]
                    selection = selections.get(sha256(raw.content.encode()).hexdigest()) if isinstance(raw, ToolReturnPart) and isinstance(raw.content, str) else None
                    if not (isinstance(part, ToolReturnPart) and isinstance(expected, ToolReturnPart)
                            and isinstance(part.content, str) and isinstance(expected.content, str)
                            and selection is not None and selection.content == expected.content
                            and len(expected.content) <= len(part.content) and replace(cast(ToolReturnPart[str], part), content=expected.content) == expected):
                        raise ContextPreparationError("已提交摘要与原始来源的部件对应不符，已暂停；原文仍保留。")
                rewritten[index] = expected
            message.parts = cast(list[ModelRequestPart], rewritten) if isinstance(message, ModelRequest) else cast(list[ModelResponsePart], rewritten)
        offset = 0
        for message in projected:
            parts = []
            for part in message.parts:
                if not content_part(part):
                    continue
                if offset >= count:
                    parts.append(part)
                offset += 1
            if parts:
                if isinstance(message, ModelRequest):
                    view.append(replace(message, parts=cast(list[ModelRequestPart], parts)))
                else:
                    view.append(replace(message, parts=cast(list[ModelResponsePart], parts)))
        return view

    @staticmethod
    def _input_bytes(prepared: PreparedContext) -> int:
        message_excludes: dict[int, IncEx | bool] = {}
        for index, message in enumerate(prepared.messages):
            part_excludes: dict[int, IncEx | bool] = {
                part_index: True for part_index, part in enumerate(message.parts) if isinstance(part, InstructionBaselinePart)
            }
            message_excludes[index] = {"instructions": True, "parts": part_excludes}
        unsent: IncEx = {"messages": message_excludes}
        return len(prepared.model_dump_json(exclude=unsent).encode("utf-8")) + 8192

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        self._apply_output_reserve(request_context)
        if self.layout is not None:
            parameters = request_context.model_request_parameters
            parameters.instruction_parts = [
                replace(part, content=self.layout.instructions) if str(part.id) == "agent" else part
                for part in (parameters.instruction_parts or [])
            ]
        self._apply_task_goal(request_context)
        selections = self.layout.previews if self.layout is not None else self._previews
        candidates: dict[str, str] = {}
        for message in request_context.messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name != "read_context_evidence" and isinstance(part.content, str) and len(part.content) > 3000:
                    evidence_id = sha256(part.content.encode("utf-8")).hexdigest()
                    candidates[evidence_id] = part.content
        compressed_sent = False
        for evidence_id, content in candidates.items():
            if evidence_id not in selections:
                selections[evidence_id] = PreviewSelection()
                if len(content) > 12000:
                    selections[evidence_id].edge = 4000
        prepared = self._project(request_context)
        for pressure in (False, True):
            for evidence_id, content in candidates.items():
                selection = selections[evidence_id]
                if pressure and (selection.edge is None or selection.edge > 1000):
                    compressed_sent |= selection.sent
                    selection.edge = 1000
                if selection.edge is None:
                    continue
                path = self.path.parent / f"{self.path.stem[:8]}-context" / f"{evidence_id[:24]}.txt"
                try:
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                    verified = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise ContextPreparationError(
                        f"全文证据未能保存并读回，已停止，未继续请求；原始消息仍保留。错误名称：{type(error).__name__}（本机程序报告的原因种类）。"
                    ) from error
                if verified != content:
                    raise ContextPreparationError("全文证据已变化，无法可靠回查；已停止，原始记录仍保留。")
                self.evidence[evidence_id] = path
                edge = selection.edge
                note = (
                    f"已保留开头与结尾各{edge}个字符，中间{len(content) - edge * 2}个字符等其余结果已省略；全文位置：{path}。"
                    f"全文证据编号：{evidence_id}。可用read_context_evidence（回查本会话全文的工具）按字符位置读取。"
                    "这只是原文片段，不是保留所有要点的摘要；尚未读取的中间内容不能当作已核对。"
                )
                if selection.content is None or pressure:
                    selection.content = content[:edge] + "\n" + note + "\n" + content[-edge:]
                if note not in self.notes:
                    self.notes.append(note)
                    self.notify(note)
            prepared = self._project(request_context)
            pressure_view = prepared.messages
            if self.config.auto_compaction and self.layout is not None and self.layout.compaction is not None:
                pressure_view = self._committed_projection(list(request_context.messages), pressure_view, self.layout.compaction)
            measurement = measure_request(request_context, pressure_view, self.config)
            if measurement.tokens <= measurement.budget:
                break
        reasons = ["capacity_compression"] if compressed_sent else []
        if prepared.omitted and self.refresh_instructions is not None:
            parameters = request_context.model_request_parameters
            before = [part.content for part in (parameters.instruction_parts or [])]
            previous_memory_block = self.layout.memory_block if self.layout is not None else ""
            parameters.instruction_parts = [replace(part, content=self.refresh_instructions(part.content)) for part in (parameters.instruction_parts or [])]
            if self.layout is not None:
                if previous_memory_block:
                    self.layout.instructions = self.layout.instructions.replace(previous_memory_block, self.layout.memory_block, 1)
                else:
                    for previous, instruction_part in zip(before, parameters.instruction_parts):
                        if previous == self.layout.instructions:
                            self.layout.instructions = instruction_part.content
            if before != [part.content for part in parameters.instruction_parts]:
                reasons.append("memory_refresh")
        if self.layout is not None:
            self.layout.record_change(reasons)
            if "capacity_compression" in reasons:
                self.layout.window_usage = None
        return request_context

    async def prepare_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        task_goal = self._apply_task_goal(request_context)
        self._apply_output_reserve(request_context)
        if self.layout is not None:
            self.layout.window_usage = None
        prepared = self._project(request_context)
        original = prepared.model_copy(update={"messages": request_context.messages})
        prepared.original_bytes = self._input_bytes(original)
        raw = self.source_messages() if self.source_messages is not None else list(request_context.messages)
        raw_refs = transcript_sources(raw)
        source_task_ids = self.source_task_ids() if self.source_task_ids is not None else {self.layout.task_id} if self.layout is not None else set()
        recovery_control_keys: set[tuple[int, int]] = set()
        for message_index, message in enumerate(raw):
            if not isinstance(message, ModelRequest):
                continue
            metadata = message.metadata or {}
            if "myrk_recovery_control" not in metadata:
                continue
            try:
                origin = RecoveryControlPrompt.model_validate(metadata["myrk_recovery_control"], strict=True)
            except ValidationError as error:
                raise ContextPreparationError("恢复控制提示的来源标记无效，已暂停；请核对保存记录。") from error
            source = origin.source
            if (source.message_index != message_index or source.part_index < 0 or source.part_index >= len(message.parts)
                    or not isinstance(message.parts[source.part_index], UserPromptPart)
                    or part_hash(message.parts[source.part_index]) != source.sha256):
                raise ContextPreparationError("恢复控制提示的原始部件或来源指纹不符，已暂停；原文仍保留。")
            if origin.task_id not in source_task_ids:
                raise ContextPreparationError("恢复控制提示关联的任务不存在或无法核对，已暂停；请核对保存记录。")
            if self.layout is not None and origin.task_id == self.layout.task_id:
                recovery_control_keys.add(source.key)
        incoming_refs: list[SourcePart] = []
        cursor = 0
        for message in request_context.messages:
            for part in message.parts:
                if not content_part(part):
                    continue
                digest = part_hash(part)
                if cursor >= len(raw_refs) or raw_refs[cursor].sha256 != digest:
                    raise ContextPreparationError("待发历史与完整原始部件顺序不符，未发送；请核对保存记录后继续。")
                incoming_refs.append(raw_refs[cursor])
                cursor += 1
        if cursor != len(raw_refs):
            raise ContextPreparationError("完整原始记录含未映射部件，未发送；原文保留。")
        previous = self.layout.compaction if self.config.auto_compaction and self.layout is not None else None
        active_refs = list(incoming_refs)
        new_refs: list[SourcePart] = []
        if previous is not None:
            current = {ref.key: ref for ref in raw_refs}
            if any(current.get(ref.key) != ref for ref in previous.source):
                raise ContextPreparationError("已提交摘要的原文指纹不符，已暂停；原始资料未被摘要覆盖。")
            source_keys = {ref.key for ref in previous.source}
            new_refs = [ref for ref in incoming_refs if ref.key not in source_keys]
            prepared.messages = self._committed_projection(list(request_context.messages), prepared.messages, previous)
            active_refs = [*previous.retained, *new_refs]
        measurement = measure_request(request_context, prepared.messages, self.config)
        tokens, budget_tokens, request_key = measurement.tokens, measurement.budget, measurement.key
        prepared.measurement_source = measurement.source
        trigger = self.force_compaction or tokens >= budget_tokens * self.config.compaction_trigger
        if trigger and self.config.auto_compaction and previous is not None and not any(ref.key not in recovery_control_keys for ref in new_refs):
            if self.force_compaction or tokens > budget_tokens:
                raise ContextLimitExceeded("当前有效摘要与保护区仍超过资料容量，且没有新增可压缩原文；已暂停，未反复摘要。请调整容量或缩小输入后继续。")
            trigger = False
        if trigger and self.config.auto_compaction:
            if self.layout is None or self.budget is None:
                raise ContextLimitExceeded("本次资料容量需要整理，但缺少可保存的任务与请求额度，已暂停；原话和未完成工作保留。")
            record = CompactionRecord(task_id=self.layout.task_id, authorization=self.authorization(), provider=self.config.identity,
                model=request_context.model.model_name, layout_version=self.layout.version + 1, replaced_id=previous.id if previous else None,
                source=deepcopy(raw_refs), source_fingerprint=source_fingerprint(raw_refs), before_tokens=tokens,
                input_budget_tokens=budget_tokens, measurement_source=prepared.measurement_source)
            self.compactions.append(record)
            self.notify("正在整理较早记录；完整原话与工具证据仍保留。")
            self.persist()
            try:
                summarized = await summarize(request=request_context, messages=prepared.messages, config=self.config,
                    budget=self.budget, attempts=self.attempts, record=record, persist=self.persist,
                    keep_tokens=int(budget_tokens * self.config.compaction_recent), delegation_id=self.delegation_id, task_goal=task_goal)
                self.budget.checkpoint(preparing_request=True)
                if self.before_commit is not None:
                    self.before_commit(ctx, request_context)
                if record.authorization != self.authorization():
                    raise ContextPreparationError("整理期间任务或授权已有变化，旧摘要未提交；请按新要求继续。")
                if task_goal != (self.task_goal() if self.task_goal is not None else None):
                    raise ContextPreparationError("整理期间任务目标已有变化，旧摘要未提交；请按新要求继续。")
                added: list[ModelMessage] = []
                if self.source_messages is not None:
                    latest = self.source_messages()
                    latest_refs = transcript_sources(latest)
                    if latest_refs[:len(raw_refs)] != raw_refs:
                        raise ContextPreparationError("整理期间原文已有变化，旧摘要未提交；请核对新资料后继续。")
                    new_keys = {ref.key for ref in latest_refs[len(raw_refs):]}
                    for mi, message in enumerate(latest):
                        parts = [part for pi, part in enumerate(message.parts) if (mi, pi) in new_keys]
                        if parts:
                            if isinstance(message, ModelRequest):
                                added.append(replace(message, parts=cast(list[ModelRequestPart], parts)))
                            else:
                                added.append(replace(message, parts=cast(list[ModelResponsePart], parts)))
                if summarized is None:
                    if self.force_compaction or tokens > budget_tokens:
                        raise ContextLimitExceeded("本次资料容量不足：近期完整原话与未完成事项没有可整理的较早交互，已暂停；请缩小最新输入或调整可信容量后继续。")
                    self.compactions.remove(record)
                    prepared.messages.extend(deepcopy(added))
                    unchanged = measure_request(request_context, prepared.messages, self.config)
                    tokens, budget_tokens, request_key = unchanged.tokens, unchanged.budget, unchanged.key
                    prepared.measurement_source = unchanged.source
                    self.persist()
                    self.notify("暂无可整理的较早交互；当前资料仍在容量内，保留原话继续工作。")
                else:
                    candidate, retained_positions = summarized
                    summaries = [part.content for message in candidate for part in message.parts if isinstance(part, CompactionPart)]
                    if not summaries:
                        raise ContextLimitExceeded("本次资料容量不足：近期完整原话与未完成事项没有可整理的较早交互，已暂停；请缩小最新输入或调整可信容量后继续。")
                    if len(summaries) != 1 or summaries[0] is None or not summaries[0].strip():
                        raise ContextPreparationError("没有得到唯一有效的摘要，未替换原记录。")
                    repaired = repair_messages(deepcopy(candidate))
                    if [part for message in candidate for part in message.parts if isinstance(part, ToolReturnPart)] != [part for message in repaired for part in message.parts if isinstance(part, ToolReturnPart)]:
                        raise ContextPreparationError("摘要拆散了工具请求与结果，未提交；原文保留。")
                    after = measure_request(request_context, [*candidate, *added], self.config)
                    compacted = measure_request(request_context, candidate, self.config)
                    local_before = measure_request(request_context, prepared.messages, self.config)
                    if compacted.tokens >= local_before.tokens or after.tokens > after.budget:
                        raise ContextLimitExceeded("摘要没有有效减量或仍超过资料容量，未替换原记录；请调整容量或缩小资料后继续。")
                    input_parts = [part for message in prepared.messages for part in message.parts if content_part(part)]
                    source_by_position: dict[int, SourcePart] = {}
                    refs = iter(active_refs)
                    for index, part in enumerate(input_parts):
                        if not isinstance(part, CompactionPart):
                            source_by_position[index] = next(refs)
                    record.retained = [source_by_position[index] for index in retained_positions if index in source_by_position]
                    retained_keys = {ref.key for ref in record.retained}
                    record.covered = [ref for ref in raw_refs if ref.key not in retained_keys]
                    from pydantic_core import to_json
                    record.mapping_fingerprint = sha256(to_json([[ref.model_dump() for ref in record.retained], [ref.model_dump() for ref in record.covered]])).hexdigest()
                    record.view = deepcopy(candidate)
                    from pydantic_ai.messages import ModelMessagesTypeAdapter
                    record.view_fingerprint = sha256(ModelMessagesTypeAdapter.dump_json(candidate)).hexdigest()
                    record.summary, record.after_tokens, record.target_reached = summaries[0], after.tokens, after.tokens <= after.budget * self.config.compaction_target
                    record.status = "completed"
                    old_version, old_changes, old_usage = self.layout.version, list(self.layout.changes), self.layout.window_usage
                    self.layout.compaction = record
                    self.layout.window_usage = None
                    self.layout.record_change(["capacity_compression"])
                    try:
                        self.persist()
                    except OSError:
                        self.layout.compaction = previous
                        self.layout.version, self.layout.changes, self.layout.window_usage = old_version, old_changes, old_usage
                        raise
                    prepared.messages, tokens = [*candidate, *deepcopy(added)], after.tokens
                    budget_tokens, request_key = after.budget, after.key
                    prepared.measurement_source = after.source
                    self.force_compaction = False
                    self.notify("较早记录已整理并保存；近期原话保留，继续原任务。")
            except BaseException as error:
                record.status = "cancelled" if self.budget.token.cancelled else "failed"
                record.error = str(error) if isinstance(error, (ContextLimitExceeded, ContextPreparationError, OSError)) else type(error).__name__
                self.notify("整理未提交：" + record.error + "；原始记录和旧有效摘要保留。")
                self.persist()
                raise
        prepared.input_tokens, prepared.input_budget_tokens, prepared.request_key = tokens, budget_tokens, request_key
        prepared.input_bytes = self._input_bytes(prepared)
        prepared.accepted = tokens <= budget_tokens
        self.prepared.append(prepared)
        if not prepared.accepted:
            raise ContextLimitExceeded(
                f"本次资料容量不足：保留原话、任务允许范围、失败和未完成事项后估计需{tokens}个 token，"
                f"超过当前模型可用输入预算{budget_tokens}个 token（本地估计，不是服务实际计费）。"
                "已停止，未发送这次请求；原始记录保留。请缩小本次问题或分段读取后继续，未完成工作仍需处理。"
            )
        request_context.messages = prepared.messages
        selections = self.layout.previews if self.layout is not None else self._previews
        for message in original.messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and isinstance(part.content, str):
                    evidence_id = sha256(part.content.encode("utf-8")).hexdigest()
                    if evidence_id in selections:
                        selections[evidence_id].sent = True
        return request_context

    def read_evidence(self, evidence_id: str, start: int, length: int, query: str) -> str:
        if evidence_id not in self.evidence:
            return "已拒绝：只能回查本次资料准备中由已授权原文生成的全文证据编号，不能读取任意路径。"
        if start < 0 or length < 1 or length > 8000:
            return "回查起点须从0开始，每次读取1至8000个字符。"
        path = self.evidence[evidence_id]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContextPreparationError(
                f"全文证据未能读取，已停止；原始消息仍保留。错误名称：{type(error).__name__}（本机程序报告的原因种类）。"
            ) from error
        if sha256(content.encode("utf-8")).hexdigest() != evidence_id:
            raise ContextPreparationError("全文证据已变化，无法可靠回查；已停止，原始记录仍保留。")
        if query:
            start = content.find(query, start)
            if start == -1:
                return "当前全文没有找到指定原话；没有改写或补造原文。"
        return (
            f"全文位置：{path}；原文字符[{start}:{min(start + length, len(content))}]，全文共{len(content)}个字符。\n"
            + content[start:start + length]
        )
