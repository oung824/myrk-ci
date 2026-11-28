"""Collect exact synthetic recovery selectors and verify managed cleanup."""
import runpy
import sys
from pathlib import Path

SELECTIONS = {'recovery-controls': ('acceptance/test_quality_recovery_instances',
                       ('tests/acceptance/test_quality_recovery_instances.py::test_qg5_first_prompt_lists_only_current_source_encoding',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_independent_restock_prepares_only_each_phases_questions',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_late_supplement_requires_a_distinct_registered_attachment_in_slot_seven',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_legacy_preparation_derives_only_missing_materials',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_parallel_stop_edit_keeps_the_rest_of_the_frozen_note',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_parallel_stop_uses_the_frozen_bytes_for_the_expected_send',
                        'tests/acceptance/test_quality_recovery_instances.py::test_qg5_scope_rejects_a_completed_restock_read_during_inventory')),
 'resume-controls': ('acceptance/test_quality_recovery_resume_instances',
                     ('tests/acceptance/test_quality_recovery_resume_instances.py::test_delegated_recovery_projects_only_frozen_variant_facts',
                      'tests/acceptance/test_quality_recovery_resume_instances.py::test_delegated_recovery_variant_reads_exact_frozen_side_and_handoff',
                      'tests/acceptance/test_quality_recovery_resume_instances.py::test_saved_theatre_mock_reads_the_handoff_without_inventing_its_content',
                      'tests/acceptance/test_quality_recovery_resume_instances.py::test_saved_theatre_recovery_scores_the_original_handoff_read',
                      'tests/acceptance/test_quality_recovery_resume_instances.py::test_stopped_score_binds_observed_requests_and_the_accepted_original_stop')),
 'terminal-projection': ('acceptance/test_quality_compaction_sources',
                         ('tests/acceptance/test_quality_compaction_sources.py::test_qg6_terminal_report_uses_the_current_unadopted_proposal',))}

scope, count, digest = sys.argv[1:]
module, selectors = SELECTIONS[scope]
entry = Path(__file__).with_name("windows-public-backend.py")
backend = runpy.run_path(str(entry))
assert all(module not in values for values in backend["AUDITED_GROUPS"].values())
backend["AUDITED_GROUPS"][scope] = (module,)
backend["AUDITED_SELECTION"][scope] = selectors
sys.argv = [str(entry), scope, "--expected-count", count, "--expected-node-digest", digest]
sys.exit(backend["main"]())
