"""The pytest entry shares the existing check runner and resource ownership."""

import asyncio
import json
import os
from pathlib import Path

from tests.support.agent_acceptance import SharedControlError, prepare_live, run_plan
from tests.support.agent_acceptance.preparation import LiveGate


def test_selected_plan(acceptance_plan, acceptance_registry, acceptance_live_request, request, tmp_path):
    if request.config.getoption("acceptance_list"):
        print(json.dumps(acceptance_registry.catalogue(), sort_keys=True))
        return
    root = request.config.getoption("acceptance_root")
    if acceptance_plan.mode == "prepare-live":
        if root is None:
            raise SharedControlError("prepare-live requires an explicit persistent --acceptance-root")
        bundle = prepare_live(acceptance_plan, root=Path(root), registry=acceptance_registry)
        print(bundle.model_dump_json())
        return
    if root is not None:
        raise SharedControlError("--acceptance-root is reserved for the final prepare-live root")
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / "agent-acceptance"
    gate = None
    if acceptance_live_request is not None:
        value = acceptance_live_request
        gate = LiveGate(value.run_root, acceptance_registry, value.grant)
    execution_ids = acceptance_live_request.execution_ids if acceptance_live_request is not None else None
    result = asyncio.run(run_plan(acceptance_plan, root=reports, registry=acceptance_registry, live_gate=gate, execution_ids=execution_ids))
    selected = [item for item in result.cases if execution_ids is None or item.execution_id in execution_ids]
    assert all(item.tool_status == "passed" and item.expectation_status in {"met", "review_required"} for item in selected), selected
