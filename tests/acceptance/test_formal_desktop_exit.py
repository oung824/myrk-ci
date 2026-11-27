import json

import pytest

from tests.support.agent_acceptance.formal import scenario_outcome
from tests.support.agent_acceptance.frozen import SOURCE


_DIAGNOSIS = SOURCE / ".scratch/qg54-desktop-diagnosis-20261123"


@pytest.mark.parametrize("runner_exit", [0, 1])
def test_recovered_qg54_timeout_fails_tool_and_preserves_product_evidence(runner_exit):
    processes = json.loads((_DIAGNOSIS / "native-processes.json").read_bytes())
    permission_loss = json.loads((_DIAGNOSIS / "known-default-permission-loss.json").read_bytes())
    facts = {
        "passed": False,
        "tool_contract_passed": True,
        "processes_complete": processes["complete"],
        "phases": [{"phase": "work", "exit_code": runner_exit}],
        "desktop_exits": {"work": {"root_exit": processes["root_exit"], "failure": processes["failure"]}},
        "critical_contracts": {"K1": "failed"},
        "known_default_permission_loss": permission_loss,
    }

    outcome = scenario_outcome(facts, {}, expected_phases=1)

    assert outcome.tool_status == "failed"
    assert outcome.product_status == "failed"
    assert outcome.facts["processes_complete"] is True
    assert outcome.facts["critical_contracts"]["K1"] == "failed"
    assert outcome.facts["known_default_permission_loss"] == permission_loss
    assert outcome.facts["desktop_exits"]["work"]["root_exit"]["signal"] == "SIGTERM"


@pytest.mark.parametrize(("runner_exit", "owner_exit", "failure", "expected_tool_status"), [
    (0, {"code": 0, "signal": None}, None, "passed"),
    (1, {"code": 0, "signal": None}, None, "failed"),
    (0, {"code": 0, "signal": None}, "Receiver exit was not observed.", "failed"),
    (0, {"code": 1, "signal": None}, None, "failed"),
    (0, None, None, "failed"),
])
def test_formal_desktop_verdict_requires_a_clean_runner_and_owner(
    runner_exit, owner_exit, failure, expected_tool_status,
):
    facts = {
        "passed": True,
        "tool_contract_passed": True,
        "processes_complete": True,
        "phases": [{"phase": "work", "exit_code": runner_exit}],
        "desktop_exits": {"work": {"root_exit": owner_exit, "failure": failure}},
    }

    assert scenario_outcome(facts, {}, expected_phases=1).tool_status == expected_tool_status
