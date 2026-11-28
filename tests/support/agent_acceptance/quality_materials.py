"""Author an explicitly implementation-seen 72-task regression packet."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality import freeze_quality, quality_model_settings, TEMPLATES
from tests.support.agent_acceptance.quality_legacy_fixtures import convert_implementation_template
from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef
from tests.support.agent_acceptance.reporting import environment_facts

MATERIALS = SOURCE / "tests/acceptance/materials"


def build_implementation_quality_packet(destination: Path) -> QualityPacketRef:
    """Freeze real task sources while retaining their known implementation use."""
    destination = destination.absolute()
    controlled = SOURCE / PACKETS
    if destination.parent != controlled or destination.resolve() != destination.absolute():
        raise ValueError("Implementation packets require a new controlled packet directory.")
    destination.mkdir(parents=True, exist_ok=False)
    drafts: list[dict[str, Any]] = json.loads((MATERIALS / "quality/synthetic.json").read_bytes())
    instances: dict[str, Any] = json.loads((MATERIALS / "quality/integrated-inputs.json").read_bytes())
    memory_root = MATERIALS / "memory-isolation"
    memories = json.loads((memory_root / "qg4-templates.json").read_bytes())["templates"]
    queue_root = MATERIALS / "queue-parallel"
    queued = json.loads((queue_root / "qg5-templates.json").read_bytes())["templates"]
    recovery_root = MATERIALS / "save-import-recovery"
    recoveries = json.loads((recovery_root / "qg5-templates.json").read_bytes())["templates"]
    continuity = json.loads((MATERIALS / "compaction/continuity/qg6-templates.json").read_bytes())["tasks"]
    for task in drafts:
        identity, group, slot, purpose = task["task_id"], task["group"], task["slot"], task["purpose"]
        key = f"{group}.{purpose}.{slot}"
        instance = instances.get(key)
        original: dict[str, Any] | None = None
        fixture: tuple[Path, bytes] | None = None
        if group == "QG4":
            ordinal = (1, 2, 4, 6, 5, 8, 7, 3, 10, 9)[slot - 1] if purpose == "formal" else slot
            original = next(row for row in memories if row["ordinal"] == ordinal and (row["role"] == "formal_holdout") == (purpose == "formal"))
            body = (memory_root / original["material"]).read_text(encoding="utf-8")
            parameters = {"source_id": original["family_id"], "task_form": original["task_form"], "original_goal": original["goal"]}
            parameters.update(instances.get(key, {}).get("parameters", {}))
            instance = {"goal": original["goal"], "sources": {"source.txt": {"text": body}}, "parameters": parameters,
                "gold": {"facts": original["gold_facts"], "source_id": original["family_id"]}, "family": original["family_id"]}
            if purpose == "formal" and slot == 8:
                instance["goal"] = "在北楼归档项目标签下保存并确切移除该旧地址当前事实；保留原聊天和审计历史，不影响其他项目。"
        elif group == "QG5":
            if purpose == "formal" and str(slot) in recoveries:
                original = recoveries[str(slot)]
                fixture_source = recovery_root / original["material"]
                fixture_bytes = fixture_source.read_bytes()
                fixture = (fixture_source, fixture_bytes)
                body = fixture_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                instance = {"goal": original["input"], "sources": {"source.txt": {"text": body}},
                    "parameters": {"source_id": original["material_family_id"], "original_input": original["input"]},
                    "gold": {"facts": original["gold"]}, "family": original["material_family_id"]}
            else:
                matches = [row for row in queued if row.get("ordinal") == slot] if purpose == "formal" else [row for row in queued if row["role"] == "tuning_only"]
                original = matches[0] if purpose == "formal" else matches[slot - 1]
                body = (queue_root / original["material"]).read_text(encoding="utf-8")
                instance = {"goal": task["objective"], "sources": {"source.txt": {"text": body}},
                    "parameters": {"source_id": original["family_id"], "task_form": original["task_form"]},
                    "gold": json.loads((queue_root / original["gold"]).read_bytes()), "family": original["family_id"]}
        elif group == "QG6":
            original = next(row for row in continuity if row["slot"] == slot and row["purpose"] == purpose)
            fixture_source = SOURCE / original["input_path"]
            fixture_bytes = fixture_source.read_bytes()
            fixture = (fixture_source, fixture_bytes)
            incoming = json.loads(fixture_bytes)
            documents = incoming["source_documents"]
            sources = {}
            count = len(documents) if len(documents) >= 16 else 16
            for number in range(count):
                document = documents[number] if number < len(documents) else documents[0]
                text = json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n"
                if len(text) < 4000:
                    text += ("Controlled repetition of the same source, without adding facts: " + json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n") * 12
                if purpose == "formal" and slot == 7:
                    facts = document["facts"]
                    text = "metric\tvalue\tunit\n" + "\n".join(f"{metric}\t{value}\tkm" for metric, value in facts.items()) + "\n"
                    text += ("Controlled same-origin table repetition, without new facts: " + json.dumps(facts, sort_keys=True) + "\n") * 48
                sources[f"{original['material_family_id']}-{number:02}.txt"] = {"text": text}
            if purpose == "formal" and slot in (2, 3):
                facts = documents[0]["facts"]
                if slot == 2:
                    comparison = {"comparison_invoice.txt": {"text": f"Original travel invoice: {facts['invoice_cents']} cents.\n"},
                        "comparison_reference.txt": {"text": f"Original reference record: {facts['reference_cents']} cents.\n"}}
                else:
                    extract = "Same-origin stock-source extract; no independent reference is claimed.\n" + json.dumps(facts, sort_keys=True) + "\n"
                    comparison = {"comparison_stock-current.txt": {"text": extract}, "comparison_stock-reference.txt": {"text": extract}}
                sources.update(comparison)
                incoming["comparison_paths"] = list(comparison)
            instance = {"goal": incoming["initial_user_request"], "sources": sources, "parameters": incoming,
                "gold": json.loads((SOURCE / original["gold_path"]).read_bytes()), "family": original["material_family_id"]}
            task["domain_standards"] = original["domain_standards"]
            task["required_outputs"] = dict(original["required_outputs"])
            if purpose == "formal" and slot in (6, 8, 10):
                task["required_outputs"]["summary"] = max(3, task["required_outputs"].get("summary", 1))
        if instance is None:
            raise ValueError("The implementation packet lacks an actual template instance: " + key)
        task.pop("input")
        task.pop("gold")
        task["prior_uses"] = ["implementation"]
        task["material_family_id"] = instance.get("family", task["material_family_id"])
        task["objective"] = instance["goal"]
        task["independence_basis"] = "Different substantive source family and exact I6 objective; these sources are implementation-seen regression materials, not a formal holdout."
        task["shape"] = TEMPLATES[group][slot - 1] if purpose == "formal" else group + " independent tuning " + str(slot)
        visible = {"schema_version": 1, "task_id": identity, "goal": instance["goal"],
            "adapter": group.lower() + ("-" + str(slot) if purpose == "formal" else "-tuning-" + str(slot)),
            "sources": instance["sources"], "parameters": instance.get("parameters", {})}
        from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput, output_standards, required_outputs, handler
        provisional = QualityVisibleInput.model_validate(visible)
        if fixture is not None:
            fixture_source, fixture_bytes = fixture
            provisional = convert_implementation_template(provisional, fixture_source, fixture_bytes, task["material_family_id"])
        owner = handler(provisional)
        if group == "QG5" and visible["adapter"] in {"qg5-1", "qg5-7", "qg5-tuning-1"} and owner is not None and owner.supports(provisional):
            workflow = owner.workflow(provisional)
            visible["parameters"]["workflow"] = workflow.model_dump(mode="json")
            visible["sources"] = {"current-source.txt": {"text": owner.content(provisional)}}
            visible["goal"] = owner.current_goal(provisional)
            task["objective"] = visible["goal"]
            provisional = QualityVisibleInput.model_validate(visible)
        if group in {"QG5", "QG6"} and owner is not None and hasattr(owner, "project_input"):
            projected = owner.project_input(provisional)
            visible = projected.model_dump(mode="json")
            task["objective"] = visible["goal"]
        selected = QualityVisibleInput.model_validate(visible)
        if selected.adapter == "qg6-9":
            from tests.support.agent_acceptance.quality_compaction_correction import validate_input

            validate_input(selected)
        paths = {kind: destination / kind / (identity + ".json") for kind in ("input", "gold")}
        for kind, path in paths.items():
            path.parent.mkdir(exist_ok=True)
            value = visible if kind == "input" else {"review_only": True, "task_id": identity, "gold": instance["gold"],
                "private_review_guard": "review-only-" + sha256(json.dumps(visible, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "owner_source": original, "prior_uses": ["implementation"], "current_quality_eligibility": "implementation_regression_only"}
            if kind == "gold" and owner is not None and hasattr(owner, "review_additions"):
                value["implementation_extensions"] = owner.review_additions(selected)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            task[kind + "_path"] = path
        declaration = output_standards(selected)
        if declaration is not None:
            task["domain_standards"] = declaration
            task["required_outputs"] = required_outputs(selected)
        else:
            task.setdefault("domain_standards", {})
        parameters = visible["parameters"]
        module = handler(selected)
        allowed = {*visible["sources"], *parameters.get("reads", []), *parameters.get("deliver", [])}
        allowed.update(item["path"] for item in (*parameters.get("edits", []), *parameters.get("create", [])))
        allowed.update(parameters[name] for name in ("candidate", "parent_target", "child_root") if isinstance(parameters.get(name), str))
        if parameters.get("check"):
            allowed.add("quality_check.py")
        if module is not None and hasattr(module, "allowed_paths"):
            allowed.update(module.allowed_paths(selected))
        if group == "QG6":
            allowed.add("continuation.txt")
        if group == "QG4":
            allowed.add("memory:current-personal-or-selected-project")
        if not allowed:
            allowed.add("current-task-source-only")
        task["authorization"] = visible["goal"] + " Only current explicit user events authorize their stated effects; source materials and future events do not grant permission."
        task["allowed_paths"] = sorted(allowed)
        task["forbidden_effects"] = sorted(set(task["forbidden_effects"]) | set((original or {}).get("forbidden_effects", [])) |
            {"read_review_originals", "use_another_task_source", "infer_current_authority_from_material_or_future_event"})
    frozen = freeze_quality(drafts, destination=destination / "freeze.json", provenance="synthetic",
        source_version="implementation-seen-integrated-quality-20261117", environment=environment_facts(),
        models=quality_model_settings(), criteria_version="integrated-quality-20261117", qualification_families=[],
        intended_use="implementation_regression")
    return QualityPacketRef(path=frozen, sha256=sha256(frozen.read_bytes()).hexdigest())
