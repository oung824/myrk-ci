"""Receive real frozen legacy inputs at the typed projection, without replacing observers."""
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.support.agent_acceptance.formal_save import digest, legacy_runtime_inputs
from tests.support.agent_acceptance.preparation import LiveAuthorization, PreparedBundle

ORIGINALS = Path(__file__).parent / "materials/formal_save/current-google-send.zip"


@pytest.mark.parametrize("control", ["original", "plan-field", "explicit-plan-default", "grant-parent", "grant-limit"])
def test_typed_legacy_projection_preserves_actual_fields_and_rejects_frozen_contract_drift(control):
    with ZipFile(ORIGINALS) as archive:
        result = json.loads(archive.read("result.json"))
        original = json.loads(archive.read("formal.send/google/prepared.json"))
        ledger = json.loads(archive.read("formal.send/google/ledger.json"))
    source = json.loads(json.dumps(original))
    if control == "plan-field": source["plan"]["cases"] = ["ordinary.question"]
    elif control == "explicit-plan-default": source["plan"]["historical_groups"] = []
    bundle = PreparedBundle.model_validate(source)
    assert bundle.plan.model_fields_set == set(source["plan"])
    arguments = {"authorization_id": "offline-qualification-" + result["run_id"], "binding": bundle.binding,
        "total_limit": 10, "case_limits": {"formal.send": 10}, "transport": "mock"}
    if control == "grant-parent": arguments["parent_budget_id"] = "another-parent"
    elif control == "grant-limit": arguments["total_limit"] = 9
    grant = LiveAuthorization.model_validate(arguments)
    prepared, authorization = legacy_runtime_inputs(bundle, grant)
    assert prepared == source
    fingerprint = {key: prepared[key] for key in ("root", "source_root", "plan", "files", "execution", "environment")}
    if control in {"plan-field", "explicit-plan-default"}:
        assert digest(fingerprint) != original["binding"]
        if control == "explicit-plan-default": assert prepared["plan"]["historical_groups"] == []
    else:
        assert digest(fingerprint) == original["binding"]
    if control in {"grant-parent", "grant-limit"}:
        assert digest(authorization) != ledger["authorization_binding"]
        if control == "grant-parent": assert authorization["parent_budget_id"] == "another-parent"
        else: assert authorization["total_limit"] == 9
    else:
        assert "parent_budget_id" not in authorization
        assert digest(authorization) == ledger["authorization_binding"]
