"""Canonical quality packet identity and trusted task registration."""

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tests.support.agent_acceptance.frozen import SOURCE, relative_material_path, source_material_path

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.quality import QualityMaterials

PACKETS = Path(".scratch/retro-agent-live-acceptance-20261116/quality-packets")


class QualityPacketRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def controlled_path(cls, value: Path) -> Path:
        value = relative_material_path(value)
        if not value.is_relative_to(PACKETS) or value.name != "freeze.json" or len(value.parts) != len(PACKETS.parts) + 2:
            raise ValueError("A canonical quality packet requires its controlled packet-id/freeze.json path.")
        return value


def read_quality_packet(reference: QualityPacketRef) -> "QualityMaterials":
    from tests.support.agent_acceptance.quality import read_quality

    path = source_material_path(reference.path)
    if path.resolve() != path.absolute() or sha256(path.read_bytes()).hexdigest() != reference.sha256:
        raise ValueError("Canonical quality packet source identity changed or follows a link.")
    value = read_quality(path)
    if value.source_path != reference.path:
        raise ValueError("Canonical quality packet was relocated or substituted.")
    for name in value.files:
        member = source_material_path(Path(name))
        if not member.is_relative_to(path.parent) or member.resolve() != member.absolute():
            raise ValueError("Canonical quality member escapes its controlled packet or follows a link.")
    return value


def register_quality_packet(registry: "CaseRegistry", reference: QualityPacketRef) -> None:
    from tests.support.agent_acceptance.core import AcceptanceCase
    from tests.support.agent_acceptance.formal import bindings, handoff, prepare

    value = read_quality_packet(reference)
    capabilities = {"QG1": "ordinary", "QG2": "file-effects", "QG3": "independent-review",
        "QG4": "project-memory", "QG5": "recovery", "QG6": "compaction-semantics"}
    from tests.support.agent_acceptance.quality_runtime import supported_input, QualityVisibleInput, output_standards, required_outputs, handler
    from tests.support.agent_acceptance.quality import verify_task_output_standards
    desktop_sources = bindings()
    packet_sources = (source_material_path(reference.path), *(source_material_path(Path(name)) for name in value.files))
    for task in value.tasks:
        product_stop_scope = "task" if task.group == "QG5" else "execution"
        actual_standards: dict[str, dict[str, Any]] = {kind: standard.model_dump(mode="json") for kind, standard in task.domain_standards.items()}
        raw = source_material_path(task.input_path).read_text(encoding="utf-8").lstrip()
        if raw.startswith("{"):
            visible = QualityVisibleInput.model_validate_json(raw)
            expected_adapter = task.group.lower() + ("-" + str(task.slot) if task.purpose == "formal" else "-tuning-" + str(task.slot))
            if visible.task_id != task.task_id or visible.goal != task.objective or visible.adapter != expected_adapter:
                raise ValueError("Canonical quality task goal, template adapter or identity differs from its frozen source.")
            module = handler(visible)
            if module is not None and hasattr(module, "product_stop_scope"):
                product_stop_scope = module.product_stop_scope(visible)
                if product_stop_scope not in {"task", "execution"}:
                    raise ValueError("Invalid owner-controlled product stop scope.")
            frozen_minima: dict[str, int] = {kind: count for kind, count in task.required_outputs.items()}
            if any(frozen_minima.get(kind, 0) < minimum for kind, minimum in required_outputs(visible).items()):
                raise ValueError("Canonical quality packet removed outputs required by its actual workflow.")
            declared = output_standards(visible)
            if declared is not None:
                verify_task_output_standards(task, declared)
                actual_standards = declared
        registry.register(AcceptanceCase("quality." + task.task_id.lower(), capabilities[task.group],
            ("deepseek", "openai-chat", "google"), handoff,
            bindings=(*desktop_sources, *packet_sources),
            prepare=prepare, formal_live=supported_input(source_material_path(task.input_path)), frozen_material_input=True, owner_ticket="24",
            product_stop_scope="task" if product_stop_scope == "task" else "execution",
            output_standards=actual_standards,
            applicability="Canonical task-specific packet; current implementation materials are regression, not unseen quality evidence."))
        case_id = "quality." + task.task_id.lower()
        registry.cases[case_id] = replace(registry.cases[case_id], observation_layer="electron-main-service-sdk")
    registry.quality_packet = reference
