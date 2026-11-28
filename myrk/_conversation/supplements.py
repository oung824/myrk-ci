from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from pydantic_ai.messages import InstructionBaselinePart, ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_graph import End

from myrk._conversation.records import Supplement, Task, Update, _linked_supplement, without_pending_supplement_journal
from myrk.compaction import part_hash
from myrk.context import ContextPreparationError
from myrk.task_control import TaskBudget, persist_supplement_selection

if TYPE_CHECKING:
    from pydantic_ai.capabilities import AgentNode, NodeResult

    from myrk.conversation import Conversation


class SupplementDelivery:
    def __init__(self, conversation: "Conversation", task: Task, budget: TaskBudget,
                 on_update: Callable[[Update], None] | None, delegation_id: str | None,
                 save_owner: Callable[[], None] | None) -> None:
        self.conversation = conversation
        self.task = task
        self.budget = budget
        self.on_update = on_update
        self.delegation_id = delegation_id
        self.save_owner = save_owner
        self.seen_supplements: set[str] = set()
        self.queued_supplements: set[str] = set()
        legacy_supplement_owners: dict[str, str] = {}
        supplement_journal: dict[tuple[str, str], Supplement] = {}
        for previous in conversation.turns:
            if previous.task is not None:
                for item in previous.task.supplements:
                    legacy_supplement_owners.setdefault(item.id, previous.task.id)
                    supplement_journal[previous.task.id, item.id] = item
        supplement_journal.update({(task.id, item.id): item for item in task.supplements})
        self.supplement_messages: dict[str, ModelRequest] = {}
        self.legacy_pending_sources: dict[tuple[int, int], tuple[str, Supplement]] = {}
        for message_index, message in enumerate(conversation._record.messages):
            if not isinstance(message, ModelRequest):
                continue
            if (key := _linked_supplement(message, legacy_supplement_owners)) is None:
                continue
            owner, identifier = key
            if owner == task.id:
                self.supplement_messages[identifier] = message
            row = supplement_journal.get((owner, identifier))
            if row is not None and (row.revoked_at is not None or row.applied_request is None):
                self.legacy_pending_sources.update({(message_index, part_index): (owner, row)
                    for part_index, part in enumerate(message.parts) if isinstance(part, UserPromptPart) and part.content == row.text})
        self.persisted_supplements = set(self.supplement_messages)
        previous_work_requests = {attempt.number for previous in conversation.turns if previous.task is not None and previous.task.id == task.id
            for attempt in previous.request_attempts if attempt.purpose == "work" and attempt.actor != "memory_review"}
        self.legacy_supplements = {pending.id for pending in task.supplements if pending.applied_request in previous_work_requests}

    def publish_task(self) -> None:
        on_update = self.on_update
        if on_update is not None:
            on_update(Update("task", task=self.task.model_copy(deep=True)))

    def save_supplement_state(self) -> None:
        save_owner = self.save_owner
        self.conversation.save()
        if save_owner is not None:
            save_owner()

    def material_history(self, *, preserve_source_positions: bool = False) -> list[ModelMessage]:
        """Project legacy text out while retaining original source indexes for compaction."""
        excluded_sources = {key for key, (owner, row) in self.legacy_pending_sources.items()
                            if row.revoked_at is not None or (owner != self.task.id and row.applied_request is None)}
        if preserve_source_positions and not excluded_sources:
            return self.conversation._record.messages
        materials: list[ModelMessage] = []
        for message_index, message in enumerate(self.conversation._record.messages):
            if not isinstance(message, ModelRequest):
                materials.append(message)
                continue
            parts = [part if (message_index, part_index) not in excluded_sources else InstructionBaselinePart(instructions=[])
                     for part_index, part in enumerate(message.parts)
                     if preserve_source_positions or (message_index, part_index) not in excluded_sources]
            if parts or not message.parts or preserve_source_positions:
                instructions = message.instructions if preserve_source_positions else without_pending_supplement_journal(message.instructions)
                materials.append(replace(message, parts=parts, instructions=instructions))
        return materials

    def supplement_message(self, pending: Supplement) -> ModelRequest:
        if pending.id not in self.supplement_messages:
            self.supplement_messages[pending.id] = ModelRequest(parts=[UserPromptPart(content=pending.text)],
                metadata={"myrk_supplement_id": pending.id, "myrk_supplement_task_id": self.task.id})
        return self.supplement_messages[pending.id]

    def adopt_supplements(self, ctx: RunContext[None], request: ModelRequestContext) -> ModelRequestContext:
        pending_items = self.task.active_supplements
        if persist_supplement_selection([item for item in pending_items if item.id not in self.seen_supplements], self.save_supplement_state):
            self.publish_task()
        for pending in pending_items:
            if pending.id not in self.supplement_messages and pending.id in self.legacy_supplements:
                self.seen_supplements.add(pending.id)
                continue
            message = self.supplement_message(pending)
            parts = [part for part in message.parts if isinstance(part, UserPromptPart)]
            if len(parts) != 1 or parts[0].content != pending.text:
                raise ContextPreparationError("补充的原始事件关联不符，未发送；请核对保存记录后继续。")
            fingerprint = part_hash(parts[0])
            if pending.id not in self.seen_supplements:
                if pending.id not in self.persisted_supplements and pending.id not in self.queued_supplements:
                    ctx.messages.append(deepcopy(message))
                self.seen_supplements.add(pending.id)
            recorded = sum(isinstance(part, UserPromptPart) and part_hash(part) == fingerprint for item in ctx.messages for part in item.parts)
            outgoing = sum(isinstance(part, UserPromptPart) and part_hash(part) == fingerprint for item in request.messages for part in item.parts)
            if outgoing < recorded:
                request.messages = [*request.messages, deepcopy(message)]
        return request

    async def schedule_late_supplements(self, ctx: RunContext[None], *, node: "AgentNode[None]", result: "NodeResult[None]") -> "NodeResult[None]":
        if isinstance(result, End):
            pending = [item for item in self.task.active_supplements
                       if item.id not in self.seen_supplements and item.id not in self.queued_supplements] if not self.budget.token.cancelled else []
            if persist_supplement_selection(pending, self.save_supplement_state):
                self.publish_task()
            for item in pending:
                self.queued_supplements.add(item.id)
                ctx.enqueue(deepcopy(self.supplement_message(item)))
            if not pending and self.delegation_id is None:
                self.budget.set_supplement_admission(False)
        return result
