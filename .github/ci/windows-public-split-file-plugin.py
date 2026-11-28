"""Register only the selected public file or approval test domain."""
import pytest


@pytest.fixture(autouse=True)
def selected_file_behavior_registry(request, monkeypatch):
    name = request.node.nodeid.partition("::")[0]
    if name == "tests/acceptance/test_files_calculation_checks.py":
        from tests.support.agent_acceptance.files_calculation import register
    elif name == "tests/acceptance/test_approval_modes.py":
        from tests.support.agent_acceptance.approval_modes import register
    else:
        return

    from tests.support import agent_acceptance
    from tests.support.agent_acceptance.catalogue import complete_catalogue

    def registry_for_test():
        registry = agent_acceptance.CaseRegistry()
        register(registry)
        complete_catalogue(registry)
        return registry

    monkeypatch.setattr(agent_acceptance, "builtin_registry", registry_for_test)
    if hasattr(request.module, "builtin_registry"):
        monkeypatch.setattr(request.module, "builtin_registry", registry_for_test)
