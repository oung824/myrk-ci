"""Register the production handlers used by selected synthetic tests."""

import pytest


@pytest.fixture(autouse=True)
def retained_synthetic_registry(request, monkeypatch):
    filename, _, node = request.node.nodeid.partition("::")
    filename = filename.rsplit("/", 1)[-1]
    if filename in ("test_judge_scoring_provenance.py", "test_judge_human_reference.py"):
        from tests.support.agent_acceptance import ordinary

        modules = (ordinary,)
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
