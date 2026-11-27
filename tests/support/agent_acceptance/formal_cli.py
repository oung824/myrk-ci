"""Fresh-process formal qualification and pre-connection authorization checks."""

import argparse
import asyncio
import json
from pathlib import Path

from tests.support.agent_acceptance.core import AcceptancePlan, run_plan
from tests.support.agent_acceptance.preparation import LiveExecutionRequest, LiveGate, verify_prepared, read_prepared_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "execute"])
    parser.add_argument("authority", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "execute":
        from tests.support.agent_acceptance import builtin_registry

        if arguments.output is None:
            parser.error("execute requires --output")
        value = LiveExecutionRequest.model_validate_json(arguments.authority.read_bytes())
        saved = read_prepared_snapshot(value.run_root, binding=value.grant.binding)
        registry = builtin_registry(quality_packet=saved.plan.quality_packet)
        gate = LiveGate(value.run_root, registry, value.grant)
        bundle = verify_prepared(value.run_root, registry=registry, binding=value.grant.binding)
        plan = AcceptancePlan(cases=[value.case_id], protocols=[value.protocol], mode="live") if value.case_id and value.protocol else bundle.plan.model_copy(update={"mode": "live"})
        result = asyncio.run(run_plan(plan,
            root=arguments.output, registry=registry, live_gate=gate, execution_ids=value.execution_ids))
        print(result.result_path)
    else:
        from tests.support.agent_acceptance.formal import verify_authority

        authority, gate = verify_authority(arguments.authority)
        bundle, _, _ = gate.validate(case_id=authority.case_id, protocol=authority.protocol)
        print(bundle.binding)


if __name__ == "__main__":
    main()
