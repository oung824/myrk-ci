"""Static scope and expectations for the maintained executable catalogue."""

from dataclasses import replace
import json
from pathlib import Path

from tests.support.agent_acceptance.core import AcceptanceCase, CaseRegistry, Protocol


GROUPS = {
    "deliverables": "deliverables", "ordinary": "conversation", "attachments": "conversation", "project-trust": "context-tools",
    "history-recipient": "context-tools", "skills": "context-tools", "plans": "context-tools", "extensions": "context-tools",
    "file-effects": "actions", "calculation": "actions", "process-effects": "actions", "controlled-send": "actions",
    "approval-modes": "review", "independent-review": "review", "delegation": "delegation",
    "personal-memory": "memory", "project-memory": "memory", "memory-failure": "memory", "memory-review": "memory",
    "task-control": "queue-parallel", "recovery": "recovery", "recovery-desktop": "recovery",
    "desktop-lifecycle": "lifecycle", "desktop": "providers", "providers": "providers",
    "provider-failures": "failures", "service-failures": "failures", "request-input": "request-layout",
    "compaction": "compaction", "compaction-semantics": "compaction", "failure-reporting": "honest-reporting",
    "integration": "recovery",
}

from tests.support.agent_acceptance.deliverables import SCREENING as DELIVERABLE_SCREENING

SCREENING = {
    **DELIVERABLE_SCREENING,
    "formal.provider-settings": {"deepseek": {"scenario_id":"D16","request_cap":12,"criteria_version":"connection-desktop-v1"}, "google": {"scenario_id":"G04","request_cap":12,"criteria_version":"connection-desktop-v1"}},
    "desktop.handoff": {"deepseek": {"scenario_id":"D17","request_cap":6,"criteria_version":"connection-desktop-v1"}},
    "formal.delegation-scope-deny": {"deepseek": {"scenario_id":"R11","request_cap":24,"criteria_version":"delegation-parent-check-v1"}},
    "formal.delegation-child-limit": {"deepseek": {"scenario_id":"R12","request_cap":30,"criteria_version":"delegation-parent-check-v1"}},
    "formal.delegation": {"deepseek": {"scenario_id":"R10","request_cap":30,"criteria_version":"delegation-parent-check-v1"},"google": {"scenario_id":"R10","request_cap":30,"criteria_version":"delegation-parent-check-v1"}},
    "formal.review-stop": {"deepseek": {"scenario_id":"R09", "request_cap":16, "criteria_version":"independent-review-v1"}},
    "formal.review-deny": {"deepseek": {"scenario_id":"R08", "request_cap":16, "criteria_version":"independent-review-v1"}},
    "formal.independent-review": {"deepseek": {"scenario_id":"R07", "request_cap":16, "criteria_version":"independent-review-v1"}, "google": {"scenario_id":"R07", "request_cap":16, "criteria_version":"independent-review-v1"}},
    "formal.approval-full-access": {"deepseek": {"scenario_id": "R03", "request_cap": 12, "criteria_version": "approval-modes-v1"}},
    "formal.approval-deny": {"deepseek": {"scenario_id": "R02", "request_cap": 12, "criteria_version": "approval-modes-v1"}},
    "formal.user-approval": {"deepseek": {"scenario_id": "R01", "request_cap": 12, "criteria_version": "approval-modes-v1"}, "google": {"scenario_id": "R01", "request_cap": 12, "criteria_version": "approval-modes-v1"}},
    "formal.files": {"deepseek": {"scenario_id": "D09", "request_cap": 20, "criteria_version": "files-calculation-v1"}},
    "formal.calculation-check": {"deepseek": {"scenario_id": "D10", "request_cap": 16, "criteria_version": "files-calculation-v1"}, "google": {"scenario_id": "G02", "request_cap": 16, "criteria_version": "files-calculation-v1", "shared_extension_fact": "formal.guidance-extensions"}},
    "formal.command": {"deepseek": {"scenario_id": "D11", "request_cap": 20, "criteria_version": "files-calculation-v1"}},
    "ordinary.question": {"deepseek": {"scenario_id": "D01", "request_cap": 4}},
    "ordinary.attachment": {"deepseek": {"scenario_id": "D02", "request_cap": 6}, "google": {"scenario_id": "G01", "request_cap": 6}},
    "ordinary.encoded-long": {"deepseek": {"scenario_id": "D03", "request_cap": 12}},
    "formal.question": {"deepseek": {"scenario_id": "D01", "request_cap": 4}},
    "formal.attachment": {"deepseek": {"scenario_id": "D02", "request_cap": 6}, "google": {"scenario_id": "G01", "request_cap": 6}},
}

EXPECTATIONS = {
    "actions.files": "success_or_evidenced_historical_save_failure",
    "actions.check": "success_or_evidenced_historical_save_failure",
    "review.service-retry": "success_or_evidenced_historical_save_failure",
    "review.stop": "success_or_current_known_shutdown_failure_historical_unknown",
    "formal.send": "success_or_current_offline_formal_google_send_save_failure",
    "actions.record-write-failure": "planned_record_obstruction",
    "actions.check-missing-receipt": "planned_missing_receipt",
    "delegation.answer-divergence": "planned_false_acceptance",
    "compaction.window-reopen": "success",
}


def describe(case: AcceptanceCase) -> AcceptanceCase:
    """Use the same static expectation at standalone and central public entries."""
    name = case.case_id
    group = case.group or GROUPS.get(case.capability, case.capability)
    ticket = case.owner_ticket or ("01" if name.startswith("ordinary.") else "02" if name.startswith(("actions.", "send.", "approval.", "review."))
        else "03" if name.startswith(("delegation.", "skills.", "plans.", "extensions."))
        else "04" if name.startswith(("memory.", "trust.")) else "05" if name.startswith(("provider.", "desktop."))
        else "07" if name.startswith("compaction.") else "08" if name.startswith(("integration.", "formal.")) else "06")
    desktop = name in {"desktop.handoff", "desktop.lifecycle", "desktop.connection-basics", "recovery.desktop-restart"} or name.startswith(("integration.", "formal.", "preview."))
    current_issue = (Path(__file__).resolve().parents[3] / ".scratch/acceptance-save-failure-diagnosis-20261113/issues/02-formal-google-send.md",) if name == "formal.send" else ()
    return replace(case, group=group, owner_ticket=ticket, screening=SCREENING.get(name, case.screening),
        observation_layer="electron-main-service-sdk" if desktop else "conversation-sdk-executor" if name.startswith("files.") else "service-sdk" if ticket == "06" or ticket == "12" else "conversation-sdk-executor",
        expectation=EXPECTATIONS.get(name, case.expectation), bindings=tuple(dict.fromkeys((*case.bindings, Path(__file__), *current_issue))),
        applicability="Local file/process facts run once; no additional supplier behavior is claimed." if name in
            {"desktop.lifecycle", "desktop.connection-basics", "recovery.desktop-restart", "integration.compaction-restart"} else case.applicability)


def complete_catalogue(registry: CaseRegistry) -> None:
    for name, case in tuple(registry.cases.items()):
        registry.cases[name] = describe(case)


def required_protocols() -> dict[str, tuple[Protocol, ...]]:
    """Read protocol applicability independently of registry and future live plans."""
    source = Path(__file__).resolve().parents[2] / "acceptance/materials/catalogue/required.json"
    groups = json.loads(source.read_bytes())
    protocols: dict[str, tuple[Protocol, ...]] = {"three_protocols": ("deepseek", "openai-chat", "google"), "deepseek_only": ("deepseek",),
        "deepseek_openai": ("deepseek", "openai-chat"), "openai_only": ("openai-chat",)}
    required = {name: protocols[group] for group, names in groups.items() for name in names}
    if len(required) != sum(map(len, groups.values())):
        raise ValueError("Required acceptance catalogue contains duplicate case identifiers.")
    return required


def required_combinations(*cases: str, prefix: str = "") -> list[tuple[str, Protocol]]:
    return [(case, protocol) for case, protocols in required_protocols().items()
            if (case in cases if cases else case.startswith(prefix)) for protocol in protocols]


def validate_builtin_catalogue(registry: CaseRegistry) -> None:
    """The reviewed scope is independent of the executable registration loops."""
    required = required_protocols()
    if set(required) != set(registry.cases):
        raise ValueError("Required acceptance catalogue cases differ from the reviewed coverage manifest.")
    for name, expected in required.items():
        if registry.cases[name].protocols != expected:
            raise ValueError(f"Required acceptance protocols differ from the reviewed manifest: {name}")
