"""Historical observations remain scoped references, separate from current results."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path

from myrk.project_paths import project_root
from tests.support.agent_acceptance import AcceptanceCase, AcceptancePlan, CaseOutcome, CaseRegistry, run_plan


def historical_report(root):
    registry = CaseRegistry()

    async def observed(_context):
        return CaseOutcome("passed")

    for name in ("actions.check", "actions.files", "review.service-retry", "formal.restart", "control.new"):
        registry.register(AcceptanceCase(name, "reporting", ("deepseek",), observed))
    result = asyncio.run(run_plan(AcceptancePlan(cases=[name for name in registry.cases if name != "formal.restart"]), root=root, registry=registry))
    return json.loads(result.result_path.read_bytes())


def test_report_separates_offline_observations_and_unmeasured_historical_real(tmp_path):
    report = historical_report(tmp_path / "report")
    assert all("historical_real_status" not in case["facts"] for case in report["cases"])
    assert report["measurements"]["historical_real"] == "not_measured"
    rows = {row["case_id"]: row for row in report["coverage"]}
    assert all(row["historical_real"] == "not_measured" and row["historical_real_evidence"] == [] for row in rows.values())
    assert rows["control.new"]["historical_real_references"] == []
    assert rows["control.new"]["historical_offline_product_observations"] == []
    expected = {"actions.check": "b-9wy88ebv", "actions.files": "b-ubupmwth", "review.service-retry": "b-58ofc62l"}
    for case_id, receipt in expected.items():
        observations = rows[case_id]["historical_offline_product_observations"]
        assert [item["id"] for item in observations] == [receipt]
        assert observations[0]["resolution_status"] == "unresolved"
    assert rows["formal.restart"]["historical_real_references"][0]["id"] == "restart-resume-20261111"


def test_historical_reference_index_is_frozen_and_links_stable_originals(tmp_path):
    report = historical_report(tmp_path / "report")
    source = Path(__file__).resolve().parents[2]
    project = project_root(source)
    assert "tests/acceptance/materials/history/index.json" in report["version"]["source_files"]
    history = report["historical_observations"]
    assert len(history["historical_real_references"]) == 6
    assert len(history["historical_offline_product_observations"]) == 3
    for row in [*history["historical_real_references"], *history["historical_offline_product_observations"]]:
        assert row["protocol"] and row["model"] and row["source_version"] and row["result"] and row["scope"]
        assert row["evidence"]
        for evidence in row["evidence"]:
            target = Path(evidence["path"])
            assert target.is_relative_to(project) and ".scratch/worktrees" not in target.as_posix()
            actual = target if target.is_file() else source / target.relative_to(project)
            assert actual.is_file() and sha256(actual.read_bytes()).hexdigest() == evidence["sha256"]
