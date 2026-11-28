"""Provide ordinary case registration for one public behavior projection."""
import pytest


@pytest.fixture(autouse=True)
def ordinary_registry_fixture(request, monkeypatch):
    if not request.node.nodeid.startswith("tests/acceptance/test_question_review.py::"):
        return
    from tests.support.agent_acceptance import CaseRegistry
    from tests.support.agent_acceptance.catalogue import complete_catalogue
    from tests.support.agent_acceptance.ordinary import register

    def make_registry():
        registry = CaseRegistry()
        register(registry)
        complete_catalogue(registry)
        return registry

    monkeypatch.setattr(request.module, "builtin_registry", make_registry)
