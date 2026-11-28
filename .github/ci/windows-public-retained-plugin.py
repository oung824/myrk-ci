"""Register the production handlers used by selected synthetic tests."""

import pytest


@pytest.fixture(autouse=True)
def retained_synthetic_registry(request, monkeypatch):
    filename = request.node.nodeid.partition("::")[0]
    if filename == "tests/acceptance/test_judge_scoring_provenance.py":
        from tests.support.agent_acceptance import ordinary

        modules = (ordinary,)
    elif filename == "tests/acceptance/test_save_import_recovery.py":
        from tests.support.agent_acceptance import actions, resume_queue

        modules = (actions, resume_queue)
    else:
        return

    from tests.support import agent_acceptance
    from tests.support.agent_acceptance.catalogue import complete_catalogue

    def registry_for_test():
        registry = agent_acceptance.CaseRegistry()
        for module in modules:
            module.register(registry)
        complete_catalogue(registry)
        return registry

    monkeypatch.setattr(agent_acceptance, "builtin_registry", registry_for_test)
    if hasattr(request.module, "builtin_registry"):
        monkeypatch.setattr(request.module, "builtin_registry", registry_for_test)
