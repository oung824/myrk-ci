"""Task-specific long-context materials reach the real native Main execution."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize("task_id", ["QG6.formal.4", "QG6.formal.6"])
def test_qg6_long_context_source_sequences_use_registered_native_prompts(prepared_root, acceptance_public_root, monkeypatch, task_id):
    import os
    if task_id == "QG6.formal.4":
        from tests.support.agent_acceptance import quality_compaction

        project_input = quality_compaction.project_input

        def shorter_proposal_control(visible):
            visible = project_input(visible)
            if visible.task_id != task_id:
                return visible
            current = quality_compaction.current_sources(visible)[:6]
            proposed = quality_compaction.unadopted_proposals(visible)
            return visible.model_copy(update={"sources": {name: visible.sources[name] for name in (*current, *proposed)}})

        monkeypatch.setattr(quality_compaction, "project_input", shorter_proposal_control)
    import subprocess
    import sys
    from zipfile import ZipFile, ZIP_DEFLATED
    from tests.support.agent_acceptance import (AcceptancePlan, FrozenParentBudget, LiveAuthorization, LiveGate,
        builtin_registry, prepare_live, quality_plan, run_plan)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg6-control-" + uuid4().hex)
    parent = FrozenParentBudget(parent_id="synthetic-qg6-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=60, phase_microusd={"screening": 0, "quality": 884760, "judge": 0}, reserve_microusd=0, profile="synthetic")
    index = Path("E:/Myrk/.scratch/acceptance-parent-budgets") / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
    bundle = None
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        proposal_parent = parent.model_copy(update={"request_cap": 2520,
            "phase_microusd": {"screening": 0, "quality": 39076920, "judge": 0}})
        proposal = quality_plan(SOURCE / reference.path, registry=registry, parent_budget=proposal_parent, mode="offline")
        identity = f"quality:deepseek:{task_id}:1"
        execution = next(row for row in proposal.executions if row.execution_id == identity).model_copy(update={"order": 1})
        plan = AcceptancePlan(executions=(execution,), parent_budget=parent, quality_packet=reference, mode="offline")
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-qg6-explicit", binding=bundle.binding, total_limit=60,
            case_limits={row.execution_id: row.request_cap for row in plan.executions}, total_microusd=884760,
            case_microusd={row.execution_id: row.cost_cap_microusd for row in plan.executions},
            parent_budget_id=parent.parent_id, transport="mock")
        acceptance_public_root.mkdir(parents=True, exist_ok=True)
        request = acceptance_public_root / "synthetic-execution-request.json"
        request.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json")}), encoding="utf-8")
        output = acceptance_public_root / "execution"
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
            str(request), "--output", str(output)], cwd=prepared_root,
            env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        (acceptance_public_root / "cli.stdout").write_bytes(process.stdout)
        (acceptance_public_root / "cli.stderr").write_bytes(process.stderr)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((output / "result.json").read_bytes())
        outcome = next(row for row in result["cases"] if row["execution_id"] == identity)
        assert outcome["tool_status"] == "passed", outcome["facts"]
        contract = outcome["facts"]["quality_compaction_contract"]
        assert contract["task_id"] == task_id
        assert contract["all_actual_sources_read"] is True
        assert contract["source_originals_unchanged"] is True
        if task_id == "QG6.formal.4":
            assert contract["proposal_prelude_saved_without_actions"] is True
            assert outcome["facts"]["goal_program_contract_passed"] is True
            assert contract["normal_summaries"] >= 1
            assert outcome["facts"]["real_http_attempts"] == 0
        else:
            assert contract["source_facts"] == {"archive_boxes": 21, "reading_room_pages": 93}
            assert contract["normal_summaries"] >= 3
            assert contract["continuation_accepted_after_three_summaries"] is True
            assert contract["actual_continuation_read"] is True
        assert outcome["semantic_status"] == "not_measured"
        assert result["planned_executions"] == [row.model_dump(mode="json") for row in plan.executions]
        assert len(result["cases"]) == 1
        wire = "\n".join((Path(path) if Path(path).is_absolute() else output / path).read_text(encoding="utf-8") for name, path in outcome["evidence"].items()
            if name.endswith("receiver/exchange.json"))
        if task_id == "QG6.formal.4":
            from copy import deepcopy
            from tests.support.agent_acceptance.quality_compaction import proposal_before_source_report
            from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

            visible = QualityVisibleInput.model_validate_json((packet / f"input/{task_id}.json").read_bytes())
            files = {name: Path(path) if Path(path).is_absolute() else output / path
                for name, path in outcome["evidence"].items()}
            records = [json.loads(path.read_bytes()) for name, path in files.items()
                if "/records/" in "/" + name and path.suffix == ".json"]
            exchange = [json.loads(path.read_bytes()) for name, path in files.items() if name.endswith("receiver/exchange.json")]
            original = next(record for record in records if len(record.get("turns", [])) == 2)
            prematurely_read = deepcopy(original)
            prematurely_read["turns"][0]["actions"].append(prematurely_read["turns"][1]["actions"][0])
            assert not proposal_before_source_report(visible, [prematurely_read], exchange)
            blank = deepcopy(original)
            blank["turns"][0]["answer"] = " "
            assert not proposal_before_source_report(visible, [blank], exchange)
            assert "irrigation-proposal.txt" in wire and "unadopted-irrigation-note-only" not in wire
        else:
            assert "archive_boxes" in wire and "reading_room_pages" in wire
            assert "release_cards29" not in wire and "archive_pages84" not in wire
        assert outcome["facts"]["budget"]["actual_http"] <= 60
    finally:
        if bundle is not None:
            acceptance_public_root.mkdir(parents=True, exist_ok=True)
            with ZipFile(acceptance_public_root / "executed-source-snapshot.zip", "x", ZIP_DEFLATED) as archive:
                for name, expected in bundle.files.items():
                    content = (prepared_root / name).read_bytes()
                    assert sha256(content).hexdigest() == expected
                    archive.writestr(name, content)
        originals = [packet / "freeze.json", packet / f"input/{task_id}.json", packet / f"gold/{task_id}.json"]
        if any(path.is_file() for path in originals):
            destination = acceptance_public_root / "review-context"
            destination.mkdir(parents=True, exist_ok=False)
            bindings = {}
            for path in originals:
                if path.is_file():
                    target = destination / path.relative_to(packet)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                    bindings[path.relative_to(packet).as_posix()] = {"source": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                        "purpose": "review_context", "product_output": False}
            (destination / "originals-index.json").write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        if index.exists():
            assert index.parent == Path("E:/Myrk/.scratch/acceptance-parent-budgets")
            index.unlink()
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.acceptance_desktop
def test_qg6_reopen_keeps_used_capacity_recovery_and_does_not_replay_sources(prepared_root, acceptance_public_root):
    import os
    import subprocess
    import sys
    from zipfile import ZipFile, ZIP_DEFLATED
    from tests.support.agent_acceptance import (AcceptancePlan, FrozenParentBudget, LiveAuthorization, LiveGate,
        builtin_registry, prepare_live, quality_plan, run_plan)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg6-control-" + uuid4().hex)
    parent = FrozenParentBudget(parent_id="synthetic-qg6-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=60, phase_microusd={"screening": 0, "quality": 884760, "judge": 0}, reserve_microusd=0, profile="synthetic")
    index = Path("E:/Myrk/.scratch/acceptance-parent-budgets") / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
    bundle = None
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        proposal_parent = parent.model_copy(update={"request_cap": 2520,
            "phase_microusd": {"screening": 0, "quality": 39076920, "judge": 0}})
        proposal = quality_plan(SOURCE / reference.path, registry=registry, parent_budget=proposal_parent, mode="offline")
        identity = "quality:deepseek:QG6.formal.10:1"
        execution = next(row for row in proposal.executions if row.execution_id == identity).model_copy(update={"order": 1})
        plan = AcceptancePlan(executions=(execution,), parent_budget=parent, quality_packet=reference, mode="offline")
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-qg6-explicit", binding=bundle.binding, total_limit=60,
            case_limits={row.execution_id: row.request_cap for row in plan.executions}, total_microusd=884760,
            case_microusd={row.execution_id: row.cost_cap_microusd for row in plan.executions},
            parent_budget_id=parent.parent_id, transport="mock")
        acceptance_public_root.mkdir(parents=True, exist_ok=True)
        request = acceptance_public_root / "synthetic-execution-request.json"
        request.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json")}), encoding="utf-8")
        output = acceptance_public_root / "execution"
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
            str(request), "--output", str(output)], cwd=prepared_root,
            env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        (acceptance_public_root / "cli.stdout").write_bytes(process.stdout)
        (acceptance_public_root / "cli.stderr").write_bytes(process.stderr)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((output / "result.json").read_bytes())
        outcome = next(row for row in result["cases"] if row["execution_id"] == identity)
        assert outcome["tool_status"] == "passed", outcome["facts"]
        contract = outcome["facts"]["quality_compaction_contract"]
        assert contract["task_id"] == "QG6.formal.10"
        assert contract["source_facts"] == {"meters": 46, "samples": 205}
        assert contract["normal_summaries"] >= 3
        assert contract["all_actual_sources_read"] is True
        assert contract["source_originals_unchanged"] is True
        assert contract["reopen_preserves_used_recovery"] is True
        assert contract["capacity_recovery_decisions"] == 1
        assert contract["actual_continuation_read"] is True
        assert outcome["semantic_status"] == "not_measured"
        assert result["planned_executions"] == [row.model_dump(mode="json") for row in plan.executions]
        assert len(result["cases"]) == 1
        wire = "\n".join((Path(path) if Path(path).is_absolute() else output / path).read_text(encoding="utf-8") for name, path in outcome["evidence"].items()
            if name.endswith("receiver/exchange.json"))
        assert "meters" in wire and "samples" in wire
        assert "release_cards29" not in wire and "archive_pages84" not in wire
        assert outcome["facts"]["budget"]["actual_http"] <= 60
    finally:
        if bundle is not None:
            acceptance_public_root.mkdir(parents=True, exist_ok=True)
            with ZipFile(acceptance_public_root / "executed-source-snapshot.zip", "x", ZIP_DEFLATED) as archive:
                for name, expected in bundle.files.items():
                    content = (prepared_root / name).read_bytes()
                    assert sha256(content).hexdigest() == expected
                    archive.writestr(name, content)
        originals = [packet / "freeze.json", packet / "input/QG6.formal.10.json", packet / "gold/QG6.formal.10.json"]
        if any(path.is_file() for path in originals):
            destination = acceptance_public_root / "review-context"
            destination.mkdir(parents=True, exist_ok=False)
            bindings = {}
            for path in originals:
                if path.is_file():
                    target = destination / path.relative_to(packet)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                    bindings[path.relative_to(packet).as_posix()] = {"source": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                        "purpose": "review_context", "product_output": False}
            (destination / "originals-index.json").write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        if index.exists():
            assert index.parent == Path("E:/Myrk/.scratch/acceptance-parent-budgets")
            index.unlink()
        if packet.exists(): shutil.rmtree(packet)
