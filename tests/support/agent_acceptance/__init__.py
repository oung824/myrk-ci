"""Public acceptance plan, case, fixture and evidence interfaces."""

from tests.support.agent_acceptance.execution import configure_executor

configure_executor()

from tests.support.agent_acceptance.core import (AcceptanceCase, AcceptancePlan, CaseContext, CaseOutcome,
    CaseRegistry, Mode, Protocol, RunResult, SharedControlError, run_plan)
from tests.support.agent_acceptance.replay import OfflineSession, Reply
from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget, SharedCoverage, full_plan_proposal
from tests.support.agent_acceptance.output_review import HumanReview, export_review, import_reviews, review_report
from tests.support.agent_acceptance.quality import (initialize_quality_usage_authority, freeze_quality, quality_material_report, quality_plan,
    SyntheticQualityReview, import_quality_reviews, quality_report, save_quality_reference, record_quality_adjustment, export_quality_review, quality_candidate_status, quality_model_settings)
from tests.support.agent_acceptance.historical_reuse import HistoricalChange, HistoricalMaterialUseReview, import_historical_material_use, HistoricalApplicabilityReview, ReadOnlyHistoricalRegrade, import_historical_regrade, export_historical_report, import_historical_group, import_historical_applicability, historical_reuse_report
from tests.support.agent_acceptance.preparation import LiveAuthorization, LiveGate, PreparedBundle, prepare_live, verify_prepared
from tests.support.agent_acceptance.quality_packet import QualityPacketRef
from tests.support.agent_acceptance.integrated_package import integrated_execution_package, freeze_integrated_execution_package, verify_integrated_execution_package


def builtin_registry(*, quality_packet: "QualityPacketRef | None" = None) -> CaseRegistry:
    from tests.support.agent_acceptance import ordinary, actions, review_cases, delegation, memory, trust, desktop_provider, resume_queue, compaction, integration, files_calculation, approval_modes, independent_review
    from tests.support.agent_acceptance.catalogue import complete_catalogue, validate_builtin_catalogue
    from tests.support.agent_acceptance.formal_scenarios import SCENARIOS, material_bindings
    from tests.support.agent_acceptance.formal import bindings, handoff, prepare
    from tests.support.agent_acceptance import deliverables, guidance_skills_extensions as guidance, authorized_send, delegation_parent_check, compaction_continuity, memory_isolation, queue_parallel_stop, pdf_office_preview, save_import_recovery

    from tests.support.agent_acceptance.formal_approval import CONTROL_CASES, FORMAL_CASES
    from tests.support.agent_acceptance.independent_review import CONTROL_CASES as REVIEW_CONTROLS, FORMAL_CASES as REVIEW_CASES

    from tests.support.agent_acceptance import text_image_preview

    registry = CaseRegistry()
    text_image_preview.register(registry)
    for module in (ordinary, actions, review_cases, delegation, memory, trust, desktop_provider, resume_queue, compaction, integration, files_calculation, approval_modes, independent_review, queue_parallel_stop, deliverables, pdf_office_preview):
        module.register(registry)
    from pathlib import Path
    plans = Path(__file__).resolve().parents[2] / "acceptance/plans"
    scenario_bindings = (*bindings(), *material_bindings(), *plans.glob("*.json"))
    for name, (capability, _offline_case, _limit) in SCENARIOS.items():
        registry.register(AcceptanceCase(name, capability, ("deepseek",) if name in memory_isolation.CONTROL_CASES or name in delegation_parent_check.CONTROL_CASES or name in guidance.VARIANTS or name in REVIEW_CONTROLS or name in authorized_send.CONTROLS or name == "formal.authorized-send-absolute-path" else ("deepseek", "openai-chat", "google"), handoff,
            output_standards={kind:compaction_continuity.STANDARD for kind in ("main_answer","summary")} if name in compaction_continuity.FORMAL_CASES else None,
            owner_ticket="17" if name in deliverables.SCENARIOS else "13" if name in save_import_recovery.SCENARIOS else "16" if name == "formal.provider-settings" else "14" if name in compaction_continuity.FORMAL_CASES else "12" if name in queue_parallel_stop.SCENARIOS else "11" if name in memory_isolation.SCENARIOS else "10" if name in delegation_parent_check.FORMAL_CASES else "08" if name in authorized_send.SCENARIOS else "05" if name in guidance.SCENARIOS else "09" if name in REVIEW_CASES else "07" if name in FORMAL_CASES else "06" if name in {"formal.files", "formal.calculation-check", "formal.command"} else "", screening={**save_import_recovery.SCREENING, **compaction_continuity.SCREENING, **queue_parallel_stop.SCREENING, **memory_isolation.SCREENING, **guidance.SCREENING, **authorized_send.SCREENING}.get(name, {}),
            bindings=scenario_bindings, prepare=prepare,
            formal_live=name not in memory_isolation.CONTROL_CASES and name not in delegation_parent_check.CONTROL_CASES and name not in guidance.CONTROLS and name not in CONTROL_CASES and name not in REVIEW_CONTROLS and name not in authorized_send.CONTROLS,
            product_stop_scope="task" if name == "formal.parallel-stop" else "execution",
            frozen_material_input=name in deliverables.SCENARIOS or name in save_import_recovery.SCENARIOS or name == "formal.provider-settings" or name in compaction_continuity.FORMAL_CASES or name in queue_parallel_stop.SCENARIOS or name in memory_isolation.SCENARIOS and name not in memory_isolation.CONTROL_CASES or name in delegation_parent_check.FORMAL_CASES and name not in delegation_parent_check.CONTROL_CASES or name in REVIEW_CASES and name not in REVIEW_CONTROLS,
            expectation="controlled-negative" if name in delegation_parent_check.CONTROL_CASES or name in guidance.CONTROLS or name in REVIEW_CONTROLS or name in authorized_send.CONTROLS else "success",
            applicability="Offline controlled-negative replay only; excluded from formal live and independent quality samples." if name in delegation_parent_check.CONTROL_CASES or name in guidance.CONTROLS or name in CONTROL_CASES or name in REVIEW_CONTROLS or name in authorized_send.CONTROLS else "Native protocol and local facts; fixed replies do not measure semantics."))
    complete_catalogue(registry)
    validate_builtin_catalogue(registry)
    if quality_packet is not None:
        from tests.support.agent_acceptance.quality_packet import register_quality_packet
        register_quality_packet(registry, quality_packet)
    return registry


__all__ = ["integrated_execution_package", "freeze_integrated_execution_package", "verify_integrated_execution_package", "QualityPacketRef", "HistoricalMaterialUseReview", "import_historical_material_use", "export_historical_report", "ReadOnlyHistoricalRegrade", "import_historical_regrade", "HistoricalApplicabilityReview", "import_historical_applicability", "HistoricalChange", "import_historical_group", "historical_reuse_report", "initialize_quality_usage_authority", "quality_model_settings", "quality_candidate_status", "export_quality_review", "record_quality_adjustment", "save_quality_reference", "SyntheticQualityReview", "import_quality_reviews", "quality_report", "quality_plan", "freeze_quality", "quality_material_report", "FrozenExecution","FrozenParentBudget","SharedCoverage","full_plan_proposal","HumanReview", "export_review", "import_reviews", "review_report", "AcceptanceCase", "AcceptancePlan", "CaseContext", "CaseOutcome", "CaseRegistry", "Mode",
           "Protocol", "RunResult", "SharedControlError", "run_plan", "OfflineSession", "Reply", "builtin_registry",
           "LiveAuthorization", "LiveGate", "PreparedBundle", "prepare_live", "verify_prepared"]
