"""One reviewable scope package; preparation never supplies sending authority."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED

from pydantic import BaseModel, ConfigDict

from tests.support.agent_acceptance.core import CaseRegistry
from tests.support.agent_acceptance.catalogue import validate_builtin_catalogue
from tests.support.agent_acceptance.frozen import full_plan_proposal
from tests.support.agent_acceptance.eligibility import preparation_eligibility
from tests.support.agent_acceptance.quality_packet import QualityPacketRef

MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/integrated-package"
GROUPS = {
    "C01": "conversation", "C02": "context-tools", "C03": "actions",
    "C04": "review", "C05": "delegation", "C06": "memory",
    "C07": "queue-parallel", "C08": "recovery", "C09": "lifecycle",
    "C10": "providers", "C11": "failures", "C12": "request-layout",
    "C13": "compaction", "C14": "honest-reporting", "C15": "deliverables",
    "C16": "text-diff-image-preview", "C17": "document-preview",
}


def integrated_execution_package(*, registry: CaseRegistry, quality_packet: QualityPacketRef | None = None,
                                 review_package: Path | None = None, historical_groups: tuple[Path, ...] = (),
                                 quality_review_package: Path | None = None, baseline: Path | None = None) -> dict[str, Any]:
    """Map every maintained branch and original pair before counting scope."""
    from tests.support.agent_acceptance.historical_reuse import historical_group_reports
    from tests.support.agent_acceptance.output_review import read_package, review_report

    current_review = review_report(review_package) if review_package is not None else None
    original = read_package(review_package) if review_package is not None else None
    histories = historical_group_reports(historical_groups)
    human_count = sum(len(row["history"]) for row in current_review["outputs"]) if current_review else 0
    outputs = current_review["outputs"] if current_review else []
    observations = {"current_review": current_review,
        "original_planned_results": original["original_cases"] if original else [], "historical_groups": histories,
        "layers": {"offline": "provided_originals" if any(row["provenance"] == "fixed_fixture" for row in outputs) else "not_measured",
            "local": "scoped_historical_facts" if any(row["historical_source_provenance"] == "independently_reviewed_local" for row in histories) else "not_measured",
            "current_real": "provided_originals" if outputs and all(row["provenance"] == "current_real" for row in outputs) else "not_measured",
            "historical_real": "scoped_facts" if any(row["layers"]["historical_real"] == "scoped_facts" for row in histories) else "not_measured",
            "historical_reuse": "scoped_facts" if any(row["layers"]["historical_reuse"] == "scoped_facts" for row in histories) else "pending_review",
            "untested": "Required real success chains, semantic content, human baseline and route qualification remain distinct."}}
    quality_info = None
    quality_assessment = None
    if quality_packet is not None:
        from tests.support.agent_acceptance import builtin_registry
        from tests.support.agent_acceptance.quality import quality_material_report
        from tests.support.agent_acceptance.quality_packet import read_quality_packet
        from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
        from tests.support.agent_acceptance.frozen import source_material_path

        canonical = builtin_registry(quality_packet=quality_packet)
        if registry.catalogue() != canonical.catalogue():
            raise ValueError("The execution package requires the canonical quality registry, not caller bindings.")
        value = read_quality_packet(quality_packet)
        tasks = []
        for task in value.tasks:
            visible = QualityVisibleInput.model_validate_json(source_material_path(task.input_path).read_bytes())
            tasks.append({"task_id": task.task_id, "purpose": task.purpose, "material_family_id": task.material_family_id,
                "source_id": visible.parameters.get("source_id", task.material_family_id), "goal": task.objective,
                "runtime_adapter": visible.adapter, "prior_uses": list(task.prior_uses),
                "expected_missing_sources": visible.parameters.get("expected_missing", []),
                "sources": {name: sha256(source.text.encode(source.encoding)).hexdigest() for name, source in visible.sources.items()},
                "input": {"path": task.input_path.as_posix(), "sha256": value.files[task.input_path.as_posix()]},
                "review_only_gold": {"path": task.gold_path.as_posix(), "sha256": value.files[task.gold_path.as_posix()]},
                "allowed_paths": list(task.allowed_paths), "forbidden_effects": list(task.forbidden_effects),
                "domain_standards": {kind: standard.model_dump(mode="json") for kind, standard in task.domain_standards.items()},
                "formal_adapter_implemented": canonical.cases["quality." + task.task_id.lower()].formal_live})
        material_report = quality_material_report(source_material_path(quality_packet.path))
        quality_info = {"reference": quality_packet.model_dump(mode="json"), "tasks": tasks,
            "independent_task_count": len(tasks), "product_execution_count": len(tasks) + 12,
            "google_compatibility_tasks": [f"QG{group}.formal.{slot}" for group in range(1, 7) for slot in (3, 9)],
            "intended_use": value.intended_use, "material_report": material_report,
            "missing_domain_standards": [task["task_id"] for task in tasks if not task["domain_standards"]],
            "missing_runtime_adapters": [task["task_id"] for task in tasks if not task["formal_adapter_implemented"]],
            "unseen_quality_ready": material_report["real_materials_ready"],
            "actual_human_review": "not_performed", "actual_quality": "not_measured",
            "actual_baseline": "not_established", "judge_qualification": "not_established"}
        if quality_review_package is not None:
            from tests.support.agent_acceptance.quality import quality_report
            quality_assessment = quality_report(source_material_path(quality_packet.path), quality_review_package, baseline=baseline)
        registry = builtin_registry()
    elif quality_review_package is not None or baseline is not None:
        raise ValueError("Quality review or baseline requires its exact canonical quality packet.")
    elif registry.quality_packet is not None:
        raise ValueError("The quality registry requires its explicit frozen packet reference.")
    validate_builtin_catalogue(registry)
    original = json.loads((MATERIALS / "legacy-required.json").read_bytes())
    protocols = {"three_protocols": ["deepseek", "openai-chat", "google"],
        "deepseek_only": ["deepseek"], "deepseek_openai": ["deepseek", "openai-chat"],
        "openai_only": ["openai-chat"]}
    legacy = []
    for applicability, names in original.items():
        for name in names:
            case = registry.cases[name]
            for protocol in protocols[applicability]:
                if protocol not in case.protocols:
                    raise ValueError(f"Legacy applicability disappeared: {name}/{protocol}")
                legacy.append({"case_id": name, "protocol": protocol,
                    "current_case_id": name, "current_protocol": protocol})
    group_ids = {name: identity for identity, name in GROUPS.items()}
    combinations = [{"case_id": case.case_id, "protocol": protocol, "group": group_ids[case.group],
        "expectation": case.expectation, "applicability": case.applicability,
        "observation_layer": case.observation_layer, "measurement": "not_measured"}
        for case in registry.cases.values() for protocol in case.protocols]
    proposal = full_plan_proposal()
    rows = proposal.executions
    routes = [{**route.model_dump(mode="json"), "sending_authorized": False} for route in preparation_eligibility()]
    plan = {"screening": {"deepseek": sum(row.stage == "screening" and row.protocol == "deepseek" for row in rows),
                "google": sum(row.stage == "screening" and row.protocol == "google" for row in rows),
                "total": sum(row.stage == "screening" for row in rows)},
        "quality": {"formal_tasks": len({row.scenario_id for row in rows if row.stage == "quality" and ".formal." in row.scenario_id}),
            "tuning_tasks": len({row.scenario_id for row in rows if row.stage == "quality" and ".tuning." in row.scenario_id}),
            "google_compatibility_executions": sum(row.stage == "quality" and row.protocol == "google" for row in rows),
            "independent_tasks": len({row.scenario_id for row in rows if row.stage == "quality"}),
            "product_executions": sum(row.stage == "quality" for row in rows), "maximum_predeclared_shared": 12},
        "product_execution_cap_without_sharing": sum(row.role == "product" for row in rows),
        "requests": {"deepseek_product": sum(row.request_cap for row in rows if row.protocol == "deepseek" and row.role == "product"),
            "google_product": sum(row.request_cap for row in rows if row.protocol == "google" and row.role == "product"),
            "google_judge": sum(row.request_cap for row in rows if row.role == "judge"),
            "parent_cap": sum(row.request_cap for row in rows)},
        "money": {"currency": "USD", "proposed_total_microusd": 180000000,
            "phase_microusd": {"screening": 70000000, "quality": 40000000, "judge": 60000000}, "reserve_microusd": 10000000},
        "executions": [row.model_dump(mode="json") for row in rows],
        "sharing": "Only predeclared identical screening/quality bindings; savings create no additional tasks or executions.",
        "accounting": "One original parent/accounting_root/ledger owns all roles, retries, inflight and fully occupied unknown costs."}
    source = Path(__file__).resolve().parents[3]
    evidence_root = ".scratch/retro-agent-live-acceptance-20261116/implementation/"
    def evidence(name: str) -> dict[str, str]:
        relative = evidence_root + name
        return {"path": relative, "sha256": sha256((source / relative).read_bytes()).hexdigest(),
            "scope": "Ticket 24 actual local originals; independent integration/applicability remain pending." if name.startswith("t24/") else "Retained gap register; source changes cannot erase known failures." if name == "known-product-gaps.md" else "Previously accepted ticket evidence; current independent applicability review remains separate."}
    local: dict[str, Any] = {}
    for identities, case_ids, owner in (
        (("B01",), ["desktop.lifecycle", "desktop.connection-basics"], "16"),
        (("B02",), ["desktop.connection-basics", "desktop.handoff"], "16"),
        (("B03",), ["formal.restart", "formal.recovery-unknown", "formal.recovery-family"], "13"),
        (("B04",), ["preview.text-diff-image", "preview.document-content", "preview.document-refusal", "preview.document-stop"], "19"),
        (tuple(f"L{number:02}" for number in range(1, 7)), ["formal.deliverables", "deliverables.command-failure", "deliverables.command-stop", "deliverables.unverified"], "17"),
        (tuple(f"L{number:02}" for number in range(7, 14)), ["preview.text-diff-image"], "18"),
        (tuple(f"L{number:02}" for number in range(14, 18)), ["preview.document-content"], "19"),
        (tuple(f"L{number:02}" for number in range(18, 22)), ["preview.document-refusal"], "19"),
        (("L22", "L23"), ["preview.document-stop"], "19"),
    ):
        if not set(case_ids) <= registry.cases.keys():
            raise ValueError("A required local obligation lost its maintained public case.")
        for identity in identities:
            local[identity] = {"case_ids": case_ids, "evidence": [evidence(f"t{owner}/handoff.md")],
                "current_measurement": "not_measured", "independent_sample_increment": 0,
                "source_and_resource_contract": "Actual content/UI and source hashes; cancel/timeout where triggered; only owned processes and materials may be cleaned."}
    failures = [
        {"id": "R14-source-change", "product_status": "failed", "tool_status": "passed",
            "critical_contracts": {"K1": "failed", "K2": "failed"},
            "actual_effect": "Approved source changed 7319 to 8842 without new approval_id/prepared_action_sha256/approval_context_sha256; changed bytes entered project B SDK and terminal input.",
            "evidence": [evidence("t11/handoff.md"), evidence("known-product-gaps.md")],
            "unresolved_related_fact": "Normal R14 exact-content approval binding remains unknown; an auxiliary memory_list read does not establish product approval."},
        {"id": "D16-off", "product_status": "failed", "tool_status": "passed", "critical_contracts": {},
            "actual_effect": "DeepSeek thinking=false became high on a new chat; both actual native Mock requests failed to preserve OFF.",
            "evidence": [evidence("t16/product-observations.json"), evidence("known-product-gaps.md")]},
        {"id": "C09-startup-failure-quit", "product_status": "failed", "tool_status": "passed", "critical_contracts": {"K5": "failed"},
            "actual_effect": "Actual missing-Python startup failed truthfully, then explicit product quit emitted shutdown_blocked; driver cleanup is not normal product exit.",
            "evidence": [evidence("t16/product-observations.json"), evidence("known-product-gaps.md")]},
        {"id": "new-chat-permission-reset", "product_status": "failed", "tool_status": "passed", "critical_contracts": {"K1": "failed"},
            "actual_effect": "The initial Main Task retains canonical tool restrictions, but temporary configuration replaces defaults; an actual public new chat loses denied_paths and revision. The pre-send gate rejects its first native request before reservation or terminal HTTP.",
            "new_chat_terminal_http_attempts": 0, "original_stop_and_reservations_preserved": True,
            "blocks": ["Main:new-chat-after-temporary-configure", "quality-version-admission"],
            "observed_scope": ["quality.qg4.formal.2/deepseek", "quality.qg4.formal.6/deepseek", "quality.qg5.formal.4/deepseek"],
            "unresolved_related_fact": "Other new-chat/protocol branches remain unqualified; shared source does not establish their acceptance. No production configuration behavior was repaired.",
            "evidence": [evidence("t24/new-chat-permission/handoff.md"), evidence("t24/qg5-main/handoff.md"), evidence("known-product-gaps.md"),
                {"path": "desktop/src/main/connection-manager.ts", "sha256": sha256((source / "desktop/src/main/connection-manager.ts").read_bytes()).hexdigest(),
                    "scope": "Actual temporary configuration source; the observed defaults are a product failure, not a broadened tool grant."}]},
        {"id": "QG6-candidate-save-failure", "product_status": "failed", "tool_status": "failed", "critical_contracts": {"K5": "failed"},
            "actual_effect": "The original-parent active candidate completed attendance and retained the cancelled roster Task, but the primary Task saved only two of sixteen sources and one normal summary before Path.replace raised PermissionError/WinError5. The backend exited; the combined success/report validation did not finish.",
            "completed_primary_sources": 2, "required_primary_sources": 16, "normal_summaries": 1,
            "cause": "unknown", "remaining_execution": "not_measured", "actual_model_semantics": "not_measured",
            "original_result": "failed raw verdict retained; complete probe report was not generated",
            "tool_status_basis": "The complete combined validation did not finish. Original save diagnostics, reliable/pending bytes and ledger were preserved; this does not turn the task or tool into a success.",
            "observed_scope": ["quality.qg6.formal.9/deepseek/active-candidate"],
            "blocks": ["QG6:active-candidate-full-source-continuation", "quality-version-admission"],
            "evidence": [evidence("t24/compaction-correction/main5-fatal-originals/" + name) for name in
                ("originals-index.json", "save-diagnostics.original.json", "at-fatal-ledger.original.json",
                 "conversation-before-failed-save.original.json", "conversation-failed-save-pending.original.json")]
                + [evidence("known-product-gaps.md")]},
    ]
    return {"schema": "integrated-execution-package-v1", "sending_authorized": False,
        "product_admission": "not_established", "proposal": plan, "routes": routes, "quality_packet": quality_info,
        "local_obligations": local, "known_product_failures": failures, "observations": observations,
        "quality_assessment": quality_assessment,
        "critical_contracts": {"K1": "failed", "K2": "failed", "K3": "unknown", "K4": "unknown", "K5": "failed"},
        "critical_contract_basis": {"K1": "R14 changed-source transfer and actual Main new-chat loss of canonical tool restrictions remain failed.",
            "K2": "R14 old approval authorized changed exact content without a new decision.",
            "K3": "No current integrated stop/recovery evidence is supplied; individual scoped historical observations remain distinct.",
            "K4": "Current full-scope saved/sent/checked/delivered/accepted state review is absent.",
            "K5": "C09 explicit exit after actual startup failure and the observed QG6 candidate record-save failure remain failed; the save failure cause is unknown."},
        "labels": {"all_passed": False, "version_admission": "not_established", "user_acceptance": "pending"},
        "human_path": {"required_tickets": ["20", "21"], "optional_judge_tickets": ["22", "23"],
            "actual_human_reviews": human_count, "actual_human_status": "provided_reviews" if human_count else "not_measured", "semantic_quality": "not_measured",
            "first_human_baseline": "not_established", "judge_qualification": "not_established", "user_acceptance": "pending"},
        "coverage": {"legacy_source_commit": "c2f67079abe1ef5e91973bfeacc852f90c5602c2",
            "legacy_case_count": sum(map(len, original.values())), "legacy_combination_count": len(legacy),
            "legacy_mapping": legacy, "current_case_count": len(registry.cases),
            "current_combination_count": len(combinations), "combinations": combinations,
            "groups": {identity: {"name": name, "cases": [case.case_id for case in registry.cases.values() if case.group == name]}
                for identity, name in GROUPS.items()},
            "crosscutting_groups": ["C11", "C12", "C14"],
            "crosscutting": [{"group": identity, "independent_sample_increment": 0,
                "applies_to": "every actual triggering execution; dedicated fault controls remain offline"}
                for identity in ("C11", "C12", "C14")]}}


class FrozenExecutionPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_root: Path
    package: dict[str, Any]
    source_files: dict[str, str]
    environment: dict[str, Any]
    source_archive_sha256: str
    binding: str


def freeze_integrated_execution_package(destination: Path, *, registry: CaseRegistry, quality_packet: QualityPacketRef | None = None) -> Path:
    """Save the complete closed proposal and exact source originals for review."""
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import AcceptancePlan
    from tests.support.agent_acceptance.preparation import SOURCE, bound_files, digest
    from tests.support.agent_acceptance.reporting import environment_facts

    destination = destination.resolve()
    if not destination.is_relative_to(project_root(SOURCE)):
        raise ValueError("Execution package must remain inside the common project root.")
    package = integrated_execution_package(registry=registry, quality_packet=quality_packet)
    cases = ["integration.compaction-restart"] + (["quality." + row["task_id"].lower() for row in package["quality_packet"]["tasks"]] if quality_packet else [])
    files = bound_files(AcceptancePlan(cases=cases, quality_packet=quality_packet), registry)
    for row in package["known_product_failures"]:
        for original in row["evidence"]:
            files[original["path"]] = original["sha256"]
    for row in package["local_obligations"].values():
        for original in row["evidence"]:
            files[original["path"]] = original["sha256"]
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination / "sources.zip"
    with ZipFile(archive_path, "x", ZIP_DEFLATED) as archive:
        for name, expected in files.items():
            content = (SOURCE / name).read_bytes()
            if sha256(content).hexdigest() != expected:
                raise ValueError("Execution package source drifted while freezing: " + name)
            archive.writestr(name, content)
    frozen = FrozenExecutionPackage(source_root=SOURCE, package=package, source_files=files,
        environment=environment_facts(), source_archive_sha256=sha256(archive_path.read_bytes()).hexdigest(), binding="")
    frozen.binding = digest(frozen.model_dump(mode="json", exclude={"binding"}))
    manifest = destination / "package.json"
    with manifest.open("x", encoding="utf-8") as stream:
        stream.write(frozen.model_dump_json(indent=2) + "\n")
    return manifest


def verify_integrated_execution_package(root: Path, *, registry: CaseRegistry) -> dict[str, Any]:
    """Check saved sources and current bytes; a digest grants no sending authority."""
    from tests.support.agent_acceptance import AcceptancePlan
    from tests.support.agent_acceptance.preparation import SOURCE, bound_files, digest
    from tests.support.agent_acceptance.reporting import environment_facts

    frozen = FrozenExecutionPackage.model_validate_json((root / "package.json").read_bytes())
    if (frozen.binding != digest(frozen.model_dump(mode="json", exclude={"binding"}))
            or frozen.source_root != SOURCE or frozen.environment != environment_facts()):
        raise ValueError("Execution package binding changed.")
    quality_packet = None
    cases = ["integration.compaction-restart"]
    if frozen.package.get("quality_packet") is not None:
        from tests.support.agent_acceptance import builtin_registry
        quality_packet = QualityPacketRef.model_validate(frozen.package["quality_packet"]["reference"])
        canonical = builtin_registry(quality_packet=quality_packet)
        if registry.quality_packet is not None and registry.catalogue() != canonical.catalogue():
            raise ValueError("Execution package canonical registry binding changed.")
        if registry.quality_packet is None:
            validate_builtin_catalogue(registry)
        registry = canonical
        cases.extend("quality." + row["task_id"].lower() for row in frozen.package["quality_packet"]["tasks"])
    current = bound_files(AcceptancePlan(cases=cases, quality_packet=quality_packet), registry)
    if any(frozen.source_files.get(name) != expected for name, expected in current.items()):
        raise ValueError("Execution package source binding changed.")
    archive_path = root / "sources.zip"
    if sha256(archive_path.read_bytes()).hexdigest() != frozen.source_archive_sha256:
        raise ValueError("Execution package source archive binding changed.")
    with ZipFile(archive_path) as archive:
        if set(archive.namelist()) != frozen.source_files.keys():
            raise ValueError("Execution package source member binding changed.")
        for name, expected in frozen.source_files.items():
            path = SOURCE / name
            if (not path.resolve().is_relative_to(SOURCE) or not path.is_file()
                    or sha256(path.read_bytes()).hexdigest() != expected
                    or sha256(archive.read(name)).hexdigest() != expected):
                raise ValueError("Execution package source original binding changed: " + name)
    return {"binding": frozen.binding, "package": str(root / "package.json"),
        "source_files": len(frozen.source_files), "sending_authorized": False,
        "product_admission": frozen.package["product_admission"]}
