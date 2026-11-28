"""Register the production handlers used by selected synthetic tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def retained_synthetic_registry(request, monkeypatch):
    filename, _, node = request.node.nodeid.partition("::")
    filename = filename.rsplit("/", 1)[-1]
    if filename in ("test_judge_scoring_provenance.py", "test_judge_human_reference.py"):
        from tests.support.agent_acceptance import ordinary

        modules = (ordinary,)
    elif filename in ("test_preparation.py", "test_integration_contract.py", "test_formal_scenarios.py",
                      "test_formal_qualification_cancel.py"):
        from tests.support.agent_acceptance import ordinary

        modules = (ordinary,)
        if node in (
            "test_prepared_root_is_frozen_at_its_final_location_and_reopens_in_new_process",
            "test_fresh_execute_cannot_compete_with_the_existing_send_owner",
        ):
            source = request.config.rootpath
            startup = request.getfixturevalue("tmp_path") / "ordinary-child-startup"
            startup.mkdir()
            hook = source / ".github/ci/windows-public-ordinary-child.py"
            (startup / "sitecustomize.py").write_bytes(hook.read_bytes())
            inherited = os.environ.get("PYTHONPATH")
            paths = (str(startup), str(source), inherited) if inherited else (str(startup), str(source))
            monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    elif filename == "test_question_review.py":
        from tests.support.agent_acceptance import compaction, delegation, memory, ordinary, review_cases

        modules = (ordinary, delegation, review_cases, memory, compaction)
    elif filename == "test_save_import_recovery.py":
        from tests.support.agent_acceptance import actions, resume_queue

        modules = (actions, resume_queue)
    elif filename == "test_compaction_source_continuity.py":
        from tests.support.agent_acceptance import compaction

        modules = (compaction,)
    elif filename == "test_request_cost_gate.py":
        from tests.support.agent_acceptance import compaction, delegation, memory

        modules = (compaction, delegation, memory)
    elif filename in ("test_queue_parallel_stop.py", "test_review_contract_triggers.py"):
        if node == "test_old_v2_original_integrity_and_stop_survive_read_only_compatibility":
            modules = ()
        else:
            from tests.support.agent_acceptance import queue_parallel_stop

            modules = (queue_parallel_stop,)
    elif filename == "test_candidate_refreeze_compatibility.py":
        modules = ()
    else:
        return

    from tests.support import agent_acceptance
    from tests.support.agent_acceptance.catalogue import complete_catalogue

    def registry_for_test(*, quality_packet=None):
        registry = agent_acceptance.CaseRegistry()
        for module in modules:
            module.register(registry)
        complete_catalogue(registry)
        if quality_packet is not None:
            from tests.support.agent_acceptance.quality_packet import register_quality_packet

            register_quality_packet(registry, quality_packet)
        return registry

    monkeypatch.setattr(agent_acceptance, "builtin_registry", registry_for_test)
    if hasattr(request.module, "builtin_registry"):
        monkeypatch.setattr(request.module, "builtin_registry", registry_for_test)


@pytest.fixture(autouse=True)
def retained_refreeze_usage_authority(request):
    if request.node.nodeid != (
        "tests/acceptance/test_candidate_refreeze_compatibility.py::"
        "test_reviewed_new_criteria_packet_preserves_tasks_and_parent"
    ):
        yield
        return

    from tests.support.agent_acceptance.quality import initialize_quality_usage_authority

    authority = initialize_quality_usage_authority()
    directory = authority.parent
    try:
        yield
    finally:
        assert directory.parent.name == ".scratch"
        assert directory.name == "quality-material-usage"
        assert not directory.is_symlink() and not directory.is_junction()
        for name in ("records", "evidence"):
            member = directory / name
            assert not member.is_symlink() and not member.is_junction()
            member.rmdir()
        for name in ("authority.json", "index.json"):
            member = directory / name
            assert member.is_file() and not member.is_symlink()
            member.unlink()
        directory.rmdir()
