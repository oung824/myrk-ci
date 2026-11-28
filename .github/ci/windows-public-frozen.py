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


def main():
    group, count, digest = sys.argv[1:]
    filename, selected, excluded = GROUPS[group]
    driver = Path(__file__).with_name("windows-public-backend.py")
    backend = runpy.run_path(str(driver))
    backend["AUDITED_GROUPS"][group] = (f"acceptance/{filename}",)
    prefix = f"tests/acceptance/{filename}.py::"
    if selected:
        backend["AUDITED_SELECTION"][group] = tuple(prefix + name for name in selected)
    backend["AUDITED_DESELECT"][group] = tuple(prefix + name for name in excluded)
    sys.argv = [str(driver), group, "--expected-count", count, "--expected-node-digest", digest]
    return backend["main"]()


if __name__ == "__main__":
    sys.exit(main())
