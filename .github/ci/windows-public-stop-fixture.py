"""Skip the unused catalogue load for the synthetic saved-gate stop test."""
import pytest


@pytest.fixture(autouse=True)
def saved_gate_fixture(request, monkeypatch):
    expected = "tests/acceptance/test_integration_contract.py::test_formal_stop_verdict_survives_failed_original_validation"
    if not request.node.nodeid.startswith(expected + "["):
        return
    from tests.support import agent_acceptance

    def unavailable_catalogue(*, quality_packet=None):
        assert quality_packet is None
        return object()

    monkeypatch.setattr(agent_acceptance, "builtin_registry", unavailable_catalogue)
