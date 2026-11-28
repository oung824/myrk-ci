"""Select frozen synthetic cases through the audited backend driver."""

from pathlib import Path
import runpy
import sys


GROUPS = {
    "frozen-bindings": (
        "test_frozen_execution_bindings",
        (),
        ("test_offline_formal_domain_keeps_the_original_gate_and_product_identity",),
    ),
    "frozen-budget": ("test_frozen_execution_budget", (), ()),
    "frozen-controls": ("test_frozen_execution_control", (), ()),
    "frozen-candidate-compatibility": (
        "test_candidate_refreeze_compatibility",
        (
            "test_reviewed_candidate_transfers_the_original_reserve_using_its_active_execution",
            "test_candidate_budget_stop_preserves_genesis_grant_and_full_unknown_reservation",
        ),
        (),
    ),
    "frozen-candidate-cancellation": (
        "test_quality_compaction_correction_instances",
        ("test_candidate_cancellation_keeps_the_original_genesis_grant",),
        (),
    ),
    "frozen-authority": (
        "test_quality_packet_authority",
        (
            "test_candidate_report_keeps_unaffected_protocols_eligible_after_local_stop",
            "test_canonical_visible_file_operations_cannot_escape_the_task_folder",
        ),
        (),
    ),
    "judge-sampling": ("test_judge_scoring_audits", (), ()),
    "judge-native-guards": (
        "test_judge_qualification",
        ("test_native_judge_controls_keep_complete_input_and_zero_send_boundaries",),
        (),
    ),
}

RETAINED_SELECTION = {
    "retained-judge-provenance": (
        "test_judge_scoring_provenance.py::test_fixture_audit_cannot_supply_actual_final[self-declared-source]",
        "test_judge_scoring_provenance.py::test_fixture_audit_cannot_supply_actual_final[bound-local-originals]",
        "test_judge_scoring_provenance.py::test_fixture_ordinary_and_critical_dimensions_have_no_actual_human_source[sufficiency]",
        "test_judge_scoring_provenance.py::test_fixture_ordinary_and_critical_dimensions_have_no_actual_human_source[K1]",
        "test_judge_scoring_provenance.py::test_original_program_failure_cannot_be_replaced_by_a_declared_pass",
        "test_judge_scoring_provenance.py::test_actual_human_fallback_does_not_accept_fixture_audits_or_unsigned_templates",
    ),
    "retained-report-controls": (
        "test_guidance_skills_extensions.py::test_domain_screening_retains_its_frozen_criteria_version[guidance-v1-guidance-v1]",
        "test_guidance_skills_extensions.py::test_domain_screening_retains_its_frozen_criteria_version[None-question-v2]",
        "test_save_import_recovery.py::test_legacy_stopped_and_save_failure_public_counterexamples",
    ),
}


def run_retained(backend, driver, group):
    nodes = tuple(f"tests/acceptance/{node}" for node in RETAINED_SELECTION[group])
    files = tuple(dict.fromkeys(node.partition("::")[0].removeprefix("tests/").removesuffix(".py") for node in nodes))
    backend["AUDITED_GROUPS"][group] = files
    backend["AUDITED_SELECTION"][group] = nodes
    backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
    plugin_name = "github_retained_synthetic"
    plugin = driver.resolve().parents[2] / "tests/support" / f"{plugin_name}.py"
    plugin_source = driver.with_name("windows-public-retained-plugin.py").read_bytes()
    with plugin.open("xb") as installed:
        installed.write(plugin_source)
    managed = backend["run_pytest"]

    def run(summary, environment, phase, paths, *options):
        return managed(summary, environment, phase, paths, *options, "-p", f"tests.support.{plugin_name}")

    backend["main"].__globals__["run_pytest"] = run
    try:
        return backend["main"]()
    finally:
        plugin.unlink()


def main():
    group, count, digest = sys.argv[1:]
    driver = Path(__file__).with_name("windows-public-backend.py")
    backend = runpy.run_path(str(driver))
    sys.argv = [str(driver), group, "--expected-count", count, "--expected-node-digest", digest]
    if group in RETAINED_SELECTION:
        return run_retained(backend, driver, group)
    filename, selected, excluded = GROUPS[group]
    backend["AUDITED_GROUPS"][group] = (f"acceptance/{filename}",)
    prefix = f"tests/acceptance/{filename}.py::"
    if selected:
        backend["AUDITED_SELECTION"][group] = tuple(prefix + name for name in selected)
    backend["AUDITED_DESELECT"][group] = tuple(prefix + name for name in excluded)
    return backend["main"]()


if __name__ == "__main__":
    sys.exit(main())
