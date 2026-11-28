"""Run an audited synthetic scope."""
import runpy
import sys
from pathlib import Path

GROUPS = {'qualified-adoption': ('test_formal_adoption',
                        25,
                        None,
                        ('test_actual_supplement_retry_keeps_source06_pending_instead_of_skipping_to07',)),
 'provider-mocks': ('test_desktop_provider',
                    20,
                    None,
                    ('test_formal_live_handoff_uses_terminal_mock_and_keeps_originals',
                     'test_frozen_preparation_executes_in_new_process_after_original_temporary_directory_is_removed',
                     'test_desktop_lifecycle_and_credentials_preserve_actual_process_evidence')),
 'provider-electron': ('test_desktop_provider',
                       1,
                       'test_desktop_lifecycle_and_credentials_preserve_actual_process_evidence',
                       ()),
 'resume-mocks': ('test_resume_queue',
                  18,
                  None,
                  ('test_complete_electron_exit_relaunch_and_original_task_continuation_keep_real_program_exit',)),
 'resume-electron': ('test_resume_queue',
                     1,
                     'test_complete_electron_exit_relaunch_and_original_task_continuation_keep_real_program_exit',
                     ())}
group, count, digest = sys.argv[1:]
filename, _, only, excluded = GROUPS[group]
entry = Path(__file__).with_name("windows-public-backend.py")
backend = runpy.run_path(str(entry))
assert all(f"acceptance/{filename}" not in files for files in backend["AUDITED_GROUPS"].values())
backend["AUDITED_GROUPS"][group] = (f"acceptance/{filename}",)
prefix = f"tests/acceptance/{filename}.py::"
if only is not None:
    backend["AUDITED_SELECTION"][group] = (prefix + only,)
backend["AUDITED_DESELECT"][group] = tuple(prefix + name for name in excluded)
sys.argv = [str(entry), group, "--expected-count", count, "--expected-node-digest", digest]
sys.exit(backend["main"]())
