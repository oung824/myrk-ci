"""Run an explicit public backend test group and publish counts from managed receipts."""

import argparse
import ast
from collections import Counter, defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
GROUPS = {
    'records-and-context': (
        'test_conversation_format', 'test_context_task_goal', 'test_input_measurement_compatibility',
        'test_legacy_conversation_export', 'test_message_persistence_compatibility',
        'test_rendered_input_budget',
    ),
    'compaction-and-budget': (
        'test_auto_compaction', 'test_compaction_fact_contract', 'test_compaction_latest_requirement',
        'test_compaction_resume', 'test_independent_budget',
    ),
    'model-protocols': (
        'test_deepseek_output_limit', 'test_gemini_provider', 'test_model_controls', 'test_request_observation_replay',
    ),
    'desktop-service': (
        'test_connection_credentials', 'test_desktop_compaction', 'test_model_selection',
        'test_recovery_control_desktop',
    ),
    'check-resources': (
        'test_bare_check_paths', 'test_check_task_id', 'test_closeout', 'test_navigation',
        'test_process_receipt', 'test_project_paths', 'test_runtime_resources',
    ),
    'layout-context': ('test_layout_context',),
    'judge-fixtures': ('acceptance/test_judge_full_ledger', 'acceptance/test_judge_native_roundtrip'),
    'synthetic-acceptance': (
        'acceptance/test_actions', 'acceptance/test_compaction_replay', 'acceptance/test_recorded_response',
        'acceptance/test_reporting_response_decoding', 'acceptance/test_review_failure_matching',
        'acceptance/test_save_diagnostics',
    ),
    'synthetic-quality': (
        'acceptance/test_quality_governance_parcel_score', 'acceptance/test_quality_recovery_supplement',
        'acceptance/test_quality_diagnostic_review',
    ),
    'synthetic-judge-qualification': ('acceptance/test_judge_qualification',),
    'synthetic-trust-and-memory': ('acceptance/test_trust', 'acceptance/test_memory'),
    'synthetic-review-and-cancellation': (
        'acceptance/test_action_review', 'acceptance/test_merger_t02_cancel_boundary',
        'acceptance/test_public_cancellation_protocols', 'acceptance/test_task_continuation_snapshot_contract',
    ),
    'synthetic-local-scoring': ('test_formal_delivery_scoring',),
    'synthetic-question-review': ('acceptance/test_question_review',),
    'synthetic-delegation': ('acceptance/test_delegation',),
    'synthetic-compaction': ('acceptance/test_compaction',),
    'process-and-scheduling': ('test_parallel_checks', 'test_process_observation'),
    'root-git-hooks': ('test_closeout_file_lists', 'test_pre_commit', 'test_pre_commit_size'),
    'executor-and-network': (
        'test_action_reporting_facts', 'test_execution_identity', 'test_executor',
        'test_full_network', 'test_supervisor_origin',
    ),
    'serial-checks': ('test_check_commands_serial',),
    'action-approval': (
        'test_approval_instruction_contract', 'test_approval_modes', 'test_auto_review',
        'test_extension_review',
    ),
    'artifact-delivery': (
        'test_artifacts', 'test_extensions', 'test_independent_delivery',
    ),
    'conversation-and-delegation': (
        'test_context', 'test_conversation', 'test_delegation', 'test_delegation_verification_status',
        'test_independent_conversation', 'test_task_goal_context',
    ),
    'desktop-conversation': ('test_desktop_service', 'test_live_settings', 'test_window'),
    'recovery': ('test_compaction_recovery', 'test_recovery', 'test_recovery_ownership', 'test_step_window_recovery'),
    'retained-context': ('test_compaction_protocols', 'test_memory', 'test_persistent_layout', 'test_project_context'),
    'provider-transcripts': ('test_deepseek_protocol', 'test_gemini_observation', 'test_gemini_tools_and_resume'),
    'request-protocols': ('test_request_contracts', 'test_request_observation'),
    'review-evidence': (
        'test_independent_review', 'test_review_format_compatibility', 'test_review_tool_recovery',
        'test_review_tool_recovery_http',
    ),
}
INTEGRATED_GROUPS = {
    'integrated-runner-scheduling': (
        'test_parallel_checks', 'test_parallel_check_scheduling',
        'test_parallel_check_interrupt', 'test_process_observation',
    ),
    'integrated-runner-receipts': ('test_closeout_runs', 'test_closeout_borrowed'),
}
PHASES = {'setup', 'call', 'teardown'}
RESULTS = ROOT / '.scratch/check-runs/results'
SYNTHETIC_COUNTS = {
    'synthetic-acceptance': 82,
    'synthetic-quality': 36,
    'synthetic-judge-qualification': 18,
    'synthetic-trust-and-memory': 42,
    'synthetic-review-and-cancellation': 22,
    'synthetic-local-scoring': 73,
    'synthetic-question-review': 76,
    'synthetic-delegation': 44,
    'synthetic-compaction': 28,
}
DESELECT = {
    'synthetic-judge-qualification': (
        'tests/acceptance/test_judge_qualification.py::test_native_judge_controls_keep_complete_input_and_zero_send_boundaries',
    ),
    'synthetic-trust-and-memory': (
        'tests/acceptance/test_memory.py::test_unsaved_notes_remain_absent_and_failed_batches_do_not_partially_apply[save-failure]',
    ),
    'synthetic-review-and-cancellation': (
        'tests/acceptance/test_action_review.py::test_independent_review_preserves_actual_decision_and_failures[stop]',
    ),
    'synthetic-local-scoring': (
        'tests/test_formal_delivery_scoring.py::test_local_delivery_does_not_replace_formal_domain_qualification',
    ),
    'synthetic-question-review': (
        'tests/acceptance/test_question_review.py::test_five_output_types_use_actual_sources_and_keep_domain_criteria',
        'tests/acceptance/test_question_review.py::test_formal_attachment_uses_the_same_read_only_product_settings',
    ),
    'synthetic-delegation': (
        'tests/acceptance/test_delegation.py::test_child_delivers_real_file_parent_reads_back_and_checks_final_bytes',
        'tests/acceptance/test_delegation.py::test_ordinary_readback_and_final_check_do_not_invent_special_verification_registration',
        'tests/acceptance/test_delegation.py::test_correct_artifact_cannot_hide_scripted_false_acceptance_claims',
    ),
    'synthetic-compaction': (
        'tests/acceptance/test_compaction.py::test_semantic_material_runs_summary_and_post_reopen_work_without_grading_leak',
    ),
}
AUDITED_GROUPS = {
    'audited-records-services': GROUPS['records-and-context'] + GROUPS['desktop-service'],
    'audited-context-budget': GROUPS['compaction-and-budget'] + GROUPS['retained-context'],
    'audited-model-protocols': GROUPS['model-protocols'],
    'audited-runtime-resources': GROUPS['check-resources'] + (
        'test_check_resource_projects', 'test_check_resources',
    ),
    'audited-hooks': GROUPS['root-git-hooks'] + (
        'test_pre_commit_checks', 'test_pre_commit_navigation', 'test_pre_commit_navigation_sources',
    ),
    'audited-scheduling': INTEGRATED_GROUPS['integrated-runner-scheduling'],
    'audited-receipts': INTEGRATED_GROUPS['integrated-runner-receipts'] + (
        'test_closeout_archive_bytes', 'test_closeout_archive_locations', 'test_closeout_delivery',
        'test_closeout_hook', 'test_closeout_scope', 'test_plan_check_evidence', 'test_scoped_closeout_files',
    ),
    'audited-executor': GROUPS['executor-and-network'] + GROUPS['layout-context'],
    'audited-desktop-conversation': GROUPS['desktop-conversation'],
    'audited-approvals': GROUPS['action-approval'],
    'audited-delivery-review': GROUPS['artifact-delivery'] + GROUPS['review-evidence'],
    'audited-delegation': GROUPS['conversation-and-delegation'],
    'audited-recovery': GROUPS['recovery'],
    'audited-serial': GROUPS['serial-checks'] + ('test_check_job_cleanup_serial', 'test_check_runtime_serial'),
    'audited-request-protocols': GROUPS['provider-transcripts'] + GROUPS['request-protocols'],
    'audited-http-observation': (
        'test_default_observed_http', 'test_gemini_incomplete_response', 'test_gemini_response_observation',
        'test_http_observation_clients', 'test_request_contracts_continuation',
        'test_request_observation_review_usage', 'test_request_observation_roles_and_layout',
    ),
    'audited-synthetic-actions': GROUPS['synthetic-acceptance'],
    'audited-synthetic-qualification': (
        GROUPS['synthetic-quality'] + GROUPS['synthetic-judge-qualification']
        + GROUPS['synthetic-trust-and-memory'] + GROUPS['synthetic-review-and-cancellation']
    ),
    'audited-local-scoring': GROUPS['synthetic-local-scoring'],
    'audited-judge': GROUPS['judge-fixtures'],
    'audited-queue-controls': ('acceptance/test_queue_parallel_stop',),
    'audited-request-cost-controls': ('acceptance/test_request_cost_gate',),
    'audited-outcome-controls': (
        'acceptance/test_formal_desktop_exit',
        'acceptance/test_quality_packet_integration',
        'acceptance/test_compaction_source_continuity',
        'acceptance/test_quality_compaction_projection',
        'acceptance/test_review_refactor_behavior',
        'test_formal_delivery_scoring',
    ),
    'audited-gemini-launch-controls': ('acceptance/test_connection_desktop_lifecycle',),
    'audited-compaction-restart-controls': ('acceptance/test_integration',),
    'audited-candidate-controls': ('acceptance/test_candidate_refreeze',),
    'audited-sdk-reopen': ('test_provider_history_reopen',),
    'audited-governance-stop-control': ('acceptance/test_quality_governance_stop_target',),
    'audited-memory-criteria': ('acceptance/test_review_contract_triggers',),
    'audited-quality-materials-1': ('acceptance/test_quality_materials_human_gate',),
    'audited-quality-materials-2': ('acceptance/test_quality_materials_human_gate',),
    'audited-quality-materials-3': ('acceptance/test_quality_materials_human_gate',),
    'audited-quality-materials-4': ('acceptance/test_quality_materials_human_gate',),
    'audited-quality-materials-5': ('acceptance/test_quality_materials_human_gate',),
    'audited-quality-materials-6': ('acceptance/test_quality_materials_human_gate',),
    'audited-integration-controls': ('acceptance/test_integration_contract',),
    'audited-formal-http-controls': ('acceptance/test_formal_http_binding',),
    'audited-historical-controls': ('acceptance/test_historical_reuse_regrading',),
    'audited-delegation-fixtures': ('acceptance/test_delegation',),
    'audited-compaction-fixtures': ('acceptance/test_compaction',),
    'audited-native-fixtures': ('acceptance/test_native_stream_close', 'acceptance/test_runtime_diagnostics', 'acceptance/test_legacy_reasoning_off', 'acceptance/test_reply_stream_lifecycle'),
    'audited-review-fixtures': ('acceptance/test_action_failure_matching', 'acceptance/test_question_review', 'acceptance/test_files_calculation_checks', 'acceptance/test_review_refactor_behavior', 'acceptance/test_review_contract_triggers'),
}
AUDITED_DESELECT = {
    'audited-historical-controls': (
        'tests/acceptance/test_historical_reuse_regrading.py::test_builtin_run_and_cli_report_keep_historical_groups_separate',
        'tests/acceptance/test_historical_reuse_regrading.py::test_current_material_use_proof_is_separate_from_legacy_as_of',
    ),
    'audited-synthetic-qualification': (
        DESELECT['synthetic-judge-qualification'] + DESELECT['synthetic-trust-and-memory']
        + DESELECT['synthetic-review-and-cancellation']
    ),
    'audited-local-scoring': DESELECT['synthetic-local-scoring'],
    'audited-delegation-fixtures': DESELECT['synthetic-delegation'],
    'audited-compaction-fixtures': DESELECT['synthetic-compaction'],
}
AUDITED_SELECTION = {
    'audited-queue-controls': (
        'tests/acceptance/test_queue_parallel_stop.py::test_public_supplement_retries_and_revocation_before_a_final_reply',
        'tests/acceptance/test_queue_parallel_stop.py::test_public_unselected_supplement_can_be_revoked_after_stop_and_reopen',
        'tests/acceptance/test_queue_parallel_stop.py::test_public_supplement_id_belongs_to_one_task_in_a_conversation',
    ),
    'audited-request-cost-controls': (
        'tests/acceptance/test_request_cost_gate.py::test_not_sent_releases_only_money_and_retry_has_its_own_identity',
        'tests/acceptance/test_request_cost_gate.py::test_timeout_keeps_full_unresolved_amount_and_retry_reserves_again',
        'tests/acceptance/test_request_cost_gate.py::test_cancelled_terminal_http_remains_unknown_and_fully_occupied',
    ),
    'audited-outcome-controls': (
        'tests/acceptance/test_formal_desktop_exit.py::test_formal_desktop_verdict_requires_a_clean_runner_and_owner',
        'tests/acceptance/test_quality_packet_integration.py::test_stage_selection_keeps_original_denominator_and_does_not_run_unselected_tasks',
        'tests/acceptance/test_quality_packet_integration.py::test_reimbursement_terminal_answer_quotes_only_the_current_policy',
        'tests/acceptance/test_quality_packet_integration.py::test_interrupted_terminal_fragment_uses_the_current_source',
        'tests/acceptance/test_compaction_source_continuity.py::test_frozen_compaction_settings_match_the_approved_local_estimate',
        'tests/acceptance/test_quality_compaction_projection.py::test_projection_fix_keeps_original_context_configuration_and_frozen_request_caps',
        'tests/acceptance/test_review_refactor_behavior.py::test_public_formal_projection_keeps_each_domain_and_legacy_restart',
        'tests/test_formal_delivery_scoring.py::test_local_delivery_does_not_replace_formal_domain_qualification',
    ),
    'audited-gemini-launch-controls': (
        'tests/acceptance/test_connection_desktop_lifecycle.py::test_public_gemini_25_flash_low_uses_exact_native_budget',
    ),
    'audited-compaction-restart-controls': (
        'tests/acceptance/test_integration.py::test_repeated_actual_compaction_survives_complete_electron_restart_without_repeating_actions',
    ),
    'audited-memory-criteria': (
        'tests/acceptance/test_review_contract_triggers.py::test_memory_stop_contract_receiver_keeps_failed_and_missing_distinct[False-failed]',
        'tests/acceptance/test_review_contract_triggers.py::test_memory_stop_contract_receiver_keeps_failed_and_missing_distinct[None-unknown]',
        'tests/acceptance/test_review_contract_triggers.py::test_memory_stop_contract_receiver_keeps_failed_and_missing_distinct[True-passed]',
        'tests/acceptance/test_review_contract_triggers.py::test_memory_without_a_stop_branch_keeps_no_trigger_na',
    ),
    'audited-quality-materials-1': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_actual_domain_standard_drift_does_not_create_a_ready_review_package[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_domain_dimensions_cannot_be_added_after_observing_the_original_output[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_per_task_frozen_domain_standard_preserves_critical_dimensions_in_human_forms[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_review_and_reference_survive_retiring_source_materials_but_copies_cannot_execute[unavailable-quality_run0]',
    ),
    'audited-quality-materials-2': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_adjustment_before_export_cannot_be_forgotten_by_packaged_freeze[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_formal_material_used_for_a_fix_becomes_regression_without_erasing_original_results',
        'tests/acceptance/test_quality_materials_human_gate.py::test_quality_export_includes_frozen_gold_authorization_and_scoring_bytes_only_for_review',
        'tests/acceptance/test_quality_materials_human_gate.py::test_review_and_reference_survive_retiring_source_materials_but_copies_cannot_execute[unused-quality_run0]',
    ),
    'audited-quality-materials-3': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_applicable_gemini_tool_roundtrip_is_required_even_when_general_quality_hits_the_line[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_legacy_undeclared_adapter_cannot_be_relabelled_common_only_after_generation[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_quality_reference_is_immutable_matches_materials_and_uses_integer_regression_limits',
        'tests/acceptance/test_quality_materials_human_gate.py::test_synthetic_manual_forms_import_all_originals_without_creating_human_reviews',
    ),
    'audited-quality-materials-4': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_a_caller_only_registry_cannot_claim_real_formal_material_readiness',
        'tests/acceptance/test_quality_materials_human_gate.py::test_code_version_can_change_but_unmatched_models_require_separate_old_version_costs[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_formal_plan_rejects_declared_models_or_environment_without_the_frozen_native_route_binding',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[gold-in-input]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[renamed-input]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[used-formal]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending[critical]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending[version]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_required_summaries_cannot_be_hidden_by_a_passing_main_answer[quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_quality_threshold_boundaries_keep_failures_unknowns_and_gemini_contracts_separate',
        'tests/acceptance/test_quality_materials_human_gate.py::test_shared_screening_is_one_preplanned_generation_with_stricter_cap_and_original_quality_denominator',
        'tests/acceptance/test_quality_materials_human_gate.py::test_usage_authority_refuses_reinitialization_and_truncated_journal[outside_evidence]',
    ),
    'audited-quality-materials-5': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_candidate_source_drift_requires_same_parent_stage_refreeze_and_preserves_closed_original',
        'tests/acceptance/test_quality_materials_human_gate.py::test_code_version_can_change_but_unmatched_models_require_separate_old_version_costs[quality_run1]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_freeze_binds_real_bytes_distinct_families_and_exact_quality_schedule',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[judge-family]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[shapes]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_readiness_requires_the_public_execution_adapters_and_keeps_synthetic_status',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending[missing]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_review_gold_and_wrong_execution_mapping_are_rejected_before_import',
        'tests/acceptance/test_quality_materials_human_gate.py::test_prior_read_only_quality_review_does_not_become_tuning_or_forge_historical_applicability',
        'tests/acceptance/test_quality_materials_human_gate.py::test_relabeling_a_synthetic_reference_cannot_establish_an_actual_human_baseline',
        'tests/acceptance/test_quality_materials_human_gate.py::test_usage_authority_refuses_reinitialization_and_truncated_journal[changed_record]',
    ),
    'audited-quality-materials-6': (
        'tests/acceptance/test_quality_materials_human_gate.py::test_copying_and_refreezing_the_same_input_and_goal_does_not_reset_observed_use',
        'tests/acceptance/test_quality_materials_human_gate.py::test_critical_failure_is_not_averaged_into_general_quality',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[family]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[missing]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_material_freeze_rejects_missing_reused_or_nonindependent_originals[template]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending[base-critical]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_missing_or_conflicting_domain_standard_blocks_quality_execution_before_sending[unknown]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_packaged_freeze_cannot_restore_unseen_status_after_formal_material_adjustment',
        'tests/acceptance/test_quality_materials_human_gate.py::test_quality_plan_reuses_frozen_execution_parent_caps_and_evaluator_only_gold',
        'tests/acceptance/test_quality_materials_human_gate.py::test_review_and_reference_survive_retiring_source_materials_but_copies_cannot_execute[adjusted-quality_run0]',
        'tests/acceptance/test_quality_materials_human_gate.py::test_usage_authority_refuses_reinitialization_and_truncated_journal[missing_record]',
    ),
    'audited-integration-controls': (
        'tests/acceptance/test_integration_contract.py::test_missing_evidence_link_stops_the_shared_run',
        'tests/acceptance/test_integration_contract.py::test_incomplete_publication_retains_only_a_proven_stop_failure',
        'tests/acceptance/test_integration_contract.py::test_publication_preserves_proven_late_request_when_earlier_original_is_incomplete',
        'tests/acceptance/test_integration_contract.py::test_formal_stop_verdict_survives_failed_original_validation',
        'tests/acceptance/test_integration_contract.py::test_runtime_negative_claim_cannot_change_the_static_expectation',
        'tests/acceptance/test_integration_contract.py::test_default_executor_uses_the_shared_install_and_explicit_invalid_path_fails_before_execution',
    ),
    'audited-native-fixtures': (
        'tests/acceptance/test_native_stream_close.py',
        'tests/acceptance/test_runtime_diagnostics.py',
        'tests/acceptance/test_legacy_reasoning_off.py',
        'tests/acceptance/test_reply_stream_lifecycle.py::test_native_mock_response_close_records_only_actual_body_progress',
        'tests/acceptance/test_reply_stream_lifecycle.py::test_complete_native_sdk_reply_keeps_decoded_response_evidence',
    ),
    'audited-review-fixtures': (
        'tests/acceptance/test_action_failure_matching.py',
        'tests/acceptance/test_question_review.py::test_shared_critical_dimension_requires_each_declared_output_type',
        'tests/acceptance/test_files_calculation_checks.py::test_native_check_receipt_binds_executed_command_input_version_and_output',
        'tests/acceptance/test_files_calculation_checks.py::test_receipt_observation_preserves_access_and_does_not_follow_retargeted_paths',
        'tests/acceptance/test_review_refactor_behavior.py::test_task_continuation_preserves_spent_work_and_selects_only_current_inputs',
        'tests/acceptance/test_review_refactor_behavior.py::test_public_native_service_continuation_preserves_the_old_turn_and_step',
        'tests/acceptance/test_review_refactor_behavior.py::test_invalid_invoice_source_keeps_the_public_unsupported_fallback',
        'tests/acceptance/test_review_contract_triggers.py::test_memory_stop_contract_receiver_keeps_failed_and_missing_distinct',
        'tests/acceptance/test_review_contract_triggers.py::test_memory_without_a_stop_branch_keeps_no_trigger_na',
    ),
}
DIAGNOSTIC_FILES = {
    'test_action_reporting_facts': 'executor-and-network',
    'test_recovery': 'recovery',
    'acceptance/test_actions': 'synthetic-acceptance',
}
DIAGNOSTIC_CASES = {
    'test_action_reporting_facts': (
        'test_bounded_action_summary_keeps_preparation_read_when_deletion_is_declined',
    ),
    'test_recovery': (
        'test_interrupted_real_edit_is_verified_then_resumed_without_repeating[after_check]',
        'test_running_check_is_observed_without_retry_until_its_receipt_exists',
    ),
    'acceptance/test_actions': ('test_local_effect_branches_keep_real_results[read-denied]',),
}
DIAGNOSTIC_XML = 'public-diagnostic.xml'
AUDITED_DIAGNOSTIC_GROUPS = {'audited-quality-materials-4'}


class CheckError(Exception):
    pass


def publish(summary, message):
    print(message, flush=True)
    if summary:
        with Path(summary).open('a', encoding='utf-8') as output:
            output.write(message + '\n')


def run_pytest(summary, environment, phase, paths, *options):
    started = monotonic()
    result = subprocess.run(
        [sys.executable, '-B', 'scripts/run_checks.py', 'pytest', '-q',
         '-p', 'scripts.pytest_shards', *options, *paths],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding='utf-8', check=False,
    )
    publish(summary, f'- {phase} took {monotonic() - started:.2f} s. Exit code: {result.returncode}.')
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CheckError('The managed launcher did not return a receipt.') from None
    commands = receipt['commands']
    runtime_removed = int(receipt.get('runtime_removed') is True and not Path(receipt['runtime_directory']).exists())
    interpreter_exited = sum(command.get('interpreter_exit_confirmed') is True for command in commands)
    jobs_empty = sum(command.get('job_active_processes') == 0 for command in commands)
    publish(summary, f'- {phase} cleanup: {runtime_removed}/1 runtime removed, '
                     f'{interpreter_exited}/1 interpreter exited, {jobs_empty}/1 Windows process group empty.')
    if len(commands) != 1 or receipt['exit_code'] != result.returncode:
        raise CheckError('The launcher receipt does not match this invocation.')
    cleanup_complete = runtime_removed == interpreter_exited == jobs_empty == 1
    if result.returncode and environment.get('MYRK_TASK_ID') == 'github-public-backend-judge-fixtures':
        failure_text = commands[0].get('output_tail', '')
        for name in receipt.get('failure_logs', []):
            path = Path(name).resolve()
            if path.is_relative_to(RESULTS.resolve()) and path.is_file():
                with path.open('rb') as output:
                    output.seek(max(path.stat().st_size - 6000, 0))
                    failure_text += output.read().decode('utf-8', errors='replace')
        exception = re.findall(r'\b(?:FileNotFoundError|ImportError|ModuleNotFoundError|PermissionError|'
                               r'ValueError|TypeError|AttributeError|RuntimeError|AssertionError)\b', failure_text)
        frames = re.findall(r'\b(?:tests|myrk)[\\/][A-Za-z0-9_.\\/-]+\.py:\d+\b', failure_text)
        if exception:
            publish(summary, '- Judge exception classes: ' + ', '.join(dict.fromkeys(exception)) + '.')
        if frames:
            publish(summary, '- Judge failure frames: ' + ', '.join(list(dict.fromkeys(frames))[-6:]) + '.')
    return result.returncode, receipt, cleanup_complete


def report_file(receipt, name):
    reports = receipt.get('retained_reports', {}).get('paths', [])
    paths = [Path(directory) / 'pytest-shards' / name for directory in reports]
    matches = [path for path in paths if path.is_file() and path.resolve().is_relative_to(RESULTS.resolve())]
    if len(matches) != 1:
        raise CheckError(f'The managed launcher did not retain one {name} report.')
    return matches[0]


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def publish_judge_diagnostic(summary, observation_path):
    reports = observation_path.parent.parent
    for result_path in reports.glob('ordinary-public/*/result.json'):
        if not result_path.resolve().is_relative_to(reports.resolve()):
            continue
        result = read_json(result_path)
        for item in result.get('cases', []):
            facts = item.get('facts', {})
            budget = facts.get('budget', {})
            numbers = [budget.get(field) for field in ('actual_http', 'reserved_requests', 'completed_responses')]
            if any(type(number) is not int or not 0 <= number <= 130 for number in numbers):
                continue
            status = item.get('product_status')
            if status not in ('passed', 'failed', 'inconclusive', 'not_executed'):
                status = 'unknown'
            if facts.get('reason') == 'dependency did not pass':
                category = 'prerequisite'
            elif facts.get('unfinished'):
                category = 'budget-stop'
            elif facts.get('error_type') in (
                'AssertionError', 'FileNotFoundError', 'PermissionError', 'OSError', 'RuntimeError',
                'ValueError', 'APIConnectionError', 'RequestError', 'TimeoutError',
            ):
                category = facts['error_type']
            elif facts.get('error') or facts.get('error_type'):
                category = 'other-error'
            else:
                category = 'none'
            publish(summary, f'- Synthetic judge outcome: {status}, {category}, '
                             f'{numbers[0]} HTTP, {numbers[1]} reserved, {numbers[2]} completed.')
            failure = item.get('evidence', {}).get('failure')
            if not isinstance(failure, str):
                continue
            trace = (result_path.parent / failure).resolve()
            if not trace.is_relative_to(result_path.parent) or trace.name != 'failure.txt' or not trace.is_file():
                continue
            frames = re.findall(r'^  File "([^"]+)", line ([0-9]+),', trace.read_text(encoding='utf-8'), re.MULTILINE)
            for filename, lineno in frames[-7:]:
                source = Path(filename).resolve()
                if source.is_relative_to(ROOT / 'myrk') or source.is_relative_to(ROOT / 'tests'):
                    relative = source.relative_to(ROOT).as_posix()
                    if re.fullmatch(r'[A-Za-z0-9_./-]+\.py', relative) and source.is_file():
                        publish(summary, f'- Synthetic judge source frame: {relative}:{int(lineno)}.')


def publish_test_diagnostic(summary, xml_path, selected_path, outcomes):
    if not xml_path.is_file() or not xml_path.resolve().is_relative_to(RESULTS.resolve()):
        raise CheckError('The managed launcher did not retain the diagnostic report.')
    source = ast.parse((ROOT / selected_path).read_bytes())
    functions = {node.name for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    labels = {node.value for node in ast.walk(source) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    published = set(subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py'], cwd=ROOT, encoding='utf-8').split('\0'))
    tree = ElementTree.parse(xml_path)
    error_names = {'AssertionError', 'FileNotFoundError', 'PermissionError', 'OSError', 'RuntimeError', 'ValueError',
                   'TypeError', 'KeyError', 'AttributeError', 'TimeoutError', 'ConnectionError', 'ExceptionGroup',
                   'Failed', 'ModuleNotFoundError', 'ImportError', 'UnicodeDecodeError', 'ValidationError'}
    for case in tree.iter('testcase'):
        name = case.get('name', '')
        if case.get('file', '').replace('\\', '/') != selected_path:
            continue
        if outcomes.get(f'{selected_path}::{name}') != 'failed':
            continue
        node = re.fullmatch(r'(test_[A-Za-z0-9_]+)(?:\[([A-Za-z0-9_-]+)\])?', name)
        if node is None or node[1] not in functions:
            continue
        label = name if node[2] is None or node[2] in labels else node[1]
        publish(summary, f'- Failed case: {selected_path}::{label}.')
        for failure in (*case.findall('failure'), *case.findall('error')):
            frames = re.findall(r'^([^\r\n]+\.py):([0-9]+):\s*([A-Za-z][A-Za-z0-9_]*)',
                                failure.text or '', re.MULTILINE)
            for file, line, category in frames[-6:]:
                path = (ROOT / file.strip()).resolve()
                if path.is_relative_to(ROOT):
                    relative = path.relative_to(ROOT).as_posix()
                    if relative in published and re.fullmatch(r'[A-Za-z0-9_./-]+\.py', relative):
                        status = category if category in error_names else 'test-failure'
                        publish(summary, f'- Failure frame: {relative}:{int(line)} ({status}).')


def node_outcomes(expected, observation):
    reports_by_node = defaultdict(list)
    for report in observation['reports']:
        reports_by_node[report['nodeid']].append((report['when'], report['outcome']))
    outcomes = {}
    for node in expected:
        reports = reports_by_node[node]
        phases = Counter(phase for phase, _ in reports)
        results = {result for _, result in reports}
        if 'failed' in results:
            outcomes[node] = 'failed'
        elif 'skipped' in results:
            outcomes[node] = 'skipped'
        elif phases == dict.fromkeys(PHASES, 1) and results == {'passed'}:
            outcomes[node] = 'passed'
        else:
            outcomes[node] = 'incomplete'
    unexpected = len(reports_by_node.keys() - outcomes.keys())
    return outcomes, unexpected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group', choices=(*GROUPS, *INTEGRATED_GROUPS, *AUDITED_GROUPS))
    parser.add_argument('--native-only', action='store_true')
    parser.add_argument('--diagnostic-file', choices=DIAGNOSTIC_FILES)
    parser.add_argument('--failed-cases-only', action='store_true')
    parser.add_argument('--expected-count', type=int)
    parser.add_argument('--expected-node-digest')
    arguments = parser.parse_args()
    group = arguments.group
    if arguments.native_only and group != 'judge-fixtures':
        parser.error('--native-only requires the judge-fixtures group')
    if arguments.diagnostic_file and (group != DIAGNOSTIC_FILES[arguments.diagnostic_file] or arguments.native_only):
        parser.error('--diagnostic-file must match its registered group')
    if arguments.failed_cases_only and not arguments.diagnostic_file:
        parser.error('--failed-cases-only requires --diagnostic-file')
    if arguments.expected_count is not None and arguments.expected_count < 1:
        parser.error('--expected-count must be positive')
    if group in AUDITED_GROUPS and (
        arguments.expected_count is None
        or not re.fullmatch(r'[0-9a-f]{64}', arguments.expected_node_digest or '')
    ):
        parser.error('Audited groups require --expected-count and a SHA-256 --expected-node-digest')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', MYRK_TASK_ID=f'github-public-backend-{group}')
    for name in ('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE',
                 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(name, None)
    publish(summary, f'### Public offline backend · {group}')
    publish(summary, 'Selected backend test files on a Windows hosted runner.')
    groups = AUDITED_GROUPS if group in AUDITED_GROUPS else INTEGRATED_GROUPS if group in INTEGRATED_GROUPS else GROUPS
    names = ('acceptance/test_judge_native_roundtrip',) if arguments.native_only else groups[group]
    if arguments.diagnostic_file:
        names = (arguments.diagnostic_file,)
    paths = tuple(f'tests/{name}.py' for name in names)
    selection = AUDITED_SELECTION.get(group, paths)
    if arguments.failed_cases_only:
        selection = tuple(f'{paths[0]}::{node}' for node in DIAGNOSTIC_CASES[arguments.diagnostic_file])
    exclusions = AUDITED_DESELECT.get(group, DESELECT.get(group, ()))
    deselection = tuple(f'--deselect={node}' for node in exclusions)
    for path in selection:
        publish(summary, f'- `{path}`')
    started = monotonic()
    stage = 'inventory'
    try:
        inventory = [name for key, names in groups.items() if key not in AUDITED_SELECTION for name in names]
        if len(inventory) != len(set(inventory)) or any(not (ROOT / path).is_file() for path in paths):
            raise CheckError('A test file is duplicated across groups or missing from this group.')
        stage = 'collection'
        status, receipt, cleaned = run_pytest(summary, environment, stage, selection, '--collect-only', '--myrk-shard=collect',
                                              *deselection)
        if status or not cleaned:
            raise CheckError('Collection or its process cleanup failed.')
        manifest_path = report_file(receipt, 'collection.json')
        manifest = read_json(manifest_path)
        expected = manifest['nodeids']
        counts = Counter(node.partition('::')[0] for node in expected)
        publish(summary, f'- Collected {len(expected)} tests across {len(counts)}/{len(paths)} selected files.')
        if set(counts) != set(paths) or len(expected) != len(set(expected)):
            raise CheckError('The collected files or test identities differ from the selection.')
        if arguments.expected_count is not None and len(expected) != arguments.expected_count:
            raise CheckError(f'Expected {arguments.expected_count} tests in this selected group.')
        if arguments.expected_node_digest:
            payload = json.dumps(sorted(expected), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if sha256(payload).hexdigest() != arguments.expected_node_digest:
                raise CheckError('The collected test identities differ from the audited manifest digest.')
            publish(summary, f'- Audited node SHA-256: {arguments.expected_node_digest}.')
        if arguments.failed_cases_only and set(expected) != set(selection):
            raise CheckError('The collected cases differ from the reproduced failures.')
        if not arguments.diagnostic_file and group in SYNTHETIC_COUNTS and (len(expected) != SYNTHETIC_COUNTS[group]
                or any(node.startswith(exclusions) for node in expected)):
            raise CheckError('The synthetic case selection differs from the reviewed scope.')
        stage = 'execution'
        diagnostic = arguments.diagnostic_file or group in AUDITED_DIAGNOSTIC_GROUPS
        diagnostic_options = (
            '--tb=short', '-o', 'junit_family=xunit1', f'--junitxml={DIAGNOSTIC_XML}',
        ) if diagnostic else ()
        status, receipt, cleaned = run_pytest(summary, environment, stage, selection, '--myrk-shard=other',
                                              f'--myrk-shard-manifest={manifest_path}', *deselection, *diagnostic_options)
        observation_path = report_file(receipt, 'observation.json')
        observation = read_json(observation_path)
        if status and arguments.native_only:
            publish_judge_diagnostic(summary, observation_path)
        outcomes, unexpected = node_outcomes(expected, observation)
        totals = Counter(outcomes.values())
        publish(summary, f'- Outcomes: {totals["passed"]} passed, {totals["failed"]} failed, '
                         f'{totals["skipped"]} skipped, {totals["incomplete"]} incomplete, {unexpected} unexpected.')
        if status and diagnostic:
            publish_test_diagnostic(summary, observation_path.parent.parent / DIAGNOSTIC_XML, paths[0], outcomes)
        for path in paths:
            counts = Counter(outcome for node, outcome in outcomes.items() if node.partition('::')[0] == path)
            publish(summary, f'- `{path}`: {sum(counts.values())} collected, {counts["passed"]} passed, '
                             f'{counts["failed"]} failed, {counts["skipped"]} skipped, {counts["incomplete"]} incomplete.')
        if (status or not cleaned or unexpected or totals['passed'] != len(expected)
                or observation['collected_nodeids'] != expected or observation['selected_nodeids'] != expected
                or observation['exitstatus'] != status or not observation['session_finished']
                or not observation['collection_completed'] or observation['interrupted']
                or observation['collection_errors'] or observation['internal_errors']):
            raise CheckError('The test outcomes, coverage, or process cleanup failed verification.')
    except CheckError as error:
        publish(summary, f'- Failed at {stage}. {error}')
        return 1
    except (ElementTree.ParseError, KeyError, OSError, TypeError, ValueError) as error:
        publish(summary, f'- Failed at {stage}. Receipt or environment error: {type(error).__name__}.')
        return 1
    finally:
        publish(summary, f'- Collection, execution and cleanup took {monotonic() - started:.2f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
