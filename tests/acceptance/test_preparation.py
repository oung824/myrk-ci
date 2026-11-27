"""Closed preparation survives disposable fixtures and rejects changed bindings."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.support.agent_acceptance import (AcceptancePlan, LiveGate, SharedControlError,
    LiveAuthorization, builtin_registry, prepare_live, verify_prepared)


def test_prepared_root_is_frozen_at_its_final_location_and_reopens_in_new_process(prepared_root):
    root = prepared_root
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=root, registry=builtin_registry())
    assert prepared.root == root.resolve()
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "verify-prepared", str(root)],
                            capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["binding"] == prepared.binding
    assert verify_prepared(root, registry=builtin_registry()).binding == prepared.binding


def test_live_refuses_missing_authorization_before_acquiring_connection(prepared_root):
    root = prepared_root
    prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=root, registry=builtin_registry())
    gate = LiveGate(root, registry=builtin_registry())
    with pytest.raises(SharedControlError, match="authorization"):
        gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work")


def test_preparation_rejects_tampered_frozen_material(prepared_root):
    root = prepared_root
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=root, registry=builtin_registry())
    material = root / next(iter(prepared.files))
    material.write_text("changed", encoding="utf-8")
    with pytest.raises(SharedControlError, match="binding"):
        verify_prepared(root, registry=builtin_registry())


def test_live_send_reservation_persists_before_handoff_and_budget_survives_reopen(prepared_root):
    root = prepared_root
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=root, registry=builtin_registry())
    grant = LiveAuthorization(authorization_id="synthetic-approved-grant", binding=prepared.binding, total_limit=1,
                              case_limits={"ordinary.question": 1}, transport="mock",
        total_microusd=122880, case_microusd={'ordinary.question': 122880})
    gate = LiveGate(root, registry=builtin_registry(), authorization=grant)
    assert gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work") == 1
    assert json.loads((root / "ledger.json").read_text(encoding="utf-8"))["attempts"][0]["purpose"] == "work"
    reopened = LiveGate(root, registry=builtin_registry(), authorization=grant)
    with pytest.raises(SharedControlError, match="budget"):
        reopened.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work")
    reopened.stop()
    with pytest.raises(SharedControlError, match="stopped"):
        gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work")


@pytest.mark.parametrize("damage", ["missing", "null", "unknown-case", "wrong-number"])
def test_shared_ledger_damage_stops_before_another_reservation(prepared_root, damage):
    root = prepared_root
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                            root=root, registry=builtin_registry())
    grant = LiveAuthorization(authorization_id="synthetic-approved-grant", binding=prepared.binding,
                              total_limit=3, case_limits={"ordinary.question": 3}, transport="mock",
        total_microusd=368640, case_microusd={'ordinary.question': 368640})
    gate = LiveGate(root, registry=builtin_registry(), authorization=grant)
    assert gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work") == 1
    path = root / "ledger.json"
    if damage == "missing":
        path.unlink()
    elif damage == "null":
        path.write_text("null", encoding="utf-8")
    else:
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["attempts"][0]["case_id" if damage == "unknown-case" else "number"] = "unregistered" if damage == "unknown-case" else 9
        path.write_text(json.dumps(ledger), encoding="utf-8")

    original = path.read_bytes() if path.exists() else None
    prepared_original = (root / "prepared.json").read_bytes()
    expected = "^Prepared execution binding cannot be read:" if damage in {"missing", "null"} else "accounting"
    with pytest.raises(SharedControlError, match=expected):
        gate.reserve(case_id="ordinary.question", protocol="deepseek", actor="main", purpose="work")
    assert (path.read_bytes() if path.exists() else None) == original
    assert (root / "prepared.json").read_bytes() == prepared_original


@pytest.mark.parametrize("damage", ["missing", "null", "missing-fields"])
def test_unreadable_prepared_binding_is_a_shared_control_failure(prepared_root, damage):
    root = prepared_root
    prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
                 root=root, registry=builtin_registry())
    path = root / "prepared.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("null" if damage == "null" else "{}", encoding="utf-8")
    with pytest.raises(SharedControlError, match="binding"):
        verify_prepared(root, registry=builtin_registry())
