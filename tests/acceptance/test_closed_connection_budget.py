"""Read exact old mixed-adapter budget facts without rerunning three Main scenarios."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

from tests.acceptance.test_connection_desktop_lifecycle import assert_frozen_execution_budget
from tests.support.agent_acceptance.core import CaseOutcome, FrozenExecution

ORIGINALS = Path(__file__).parent / "materials/connection-desktop/closed-lane2-ledger-4ef"


def test_closed_three_execution_parent_uses_common_budget_ledger_without_replaying_main(tmp_path):
    index = json.loads((ORIGINALS / "index.json").read_bytes())
    archive = ORIGINALS / "originals.zip"
    assert archive.stat().st_size == index["archive"]["bytes"] and sha256(archive.read_bytes()).hexdigest() == index["archive"]["sha256"]
    paths = {}
    with ZipFile(archive) as zipped:
        for name, row in index["files"].items():
            data = zipped.read(row["member"])
            assert len(data) == row["bytes"] and sha256(data).hexdigest() == row["sha256"]
            path = tmp_path / row["member"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            paths[name] = path
    original = json.loads(paths["result"].read_bytes())
    assert original["planned_denominator"] == len(original["cases"]) == 3
    plan = {row["execution_id"]: FrozenExecution.model_validate(row) for row in original["planned_executions"]}
    parent = original["version"]["plan"]["parent_budget"]
    bindings = set()
    for raw in original["cases"]:
        entry = index["cases"][raw["execution_id"]]
        evidence = {label: paths.get(entry["files"].get(label), Path(path)) for label, path in raw["evidence"].items()}
        outcome = CaseOutcome(**{**raw, "evidence": evidence})
        before = asdict(outcome)
        ledger = assert_frozen_execution_budget(outcome, plan[outcome.execution_id])
        assert ledger["authorization"]["parent_budget_id"] == parent["parent_id"]
        assert ledger["authorization"]["total_limit"] == parent["request_cap"] == 30
        bindings.add(ledger["binding"])
        assert asdict(outcome) == before
    assert len(bindings) == 1
    assert [case["product_status"] for case in original["cases"]] == ["failed", "passed", "passed"]
