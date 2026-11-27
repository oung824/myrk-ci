"""Observe the unchanged private supervisor and its real descendants externally."""

import asyncio
import json
import os
from pathlib import Path
import traceback
from uuid import uuid4

from pydantic import TypeAdapter

from tests.support.process_observation import CaptureCommand, Observation, ParentEvidence, ProcessObserver, SpawnEvidence, WindowsProcesses
from tests.support.process_receipt import _parent_pid


def install_check_observation() -> None:
    """Hold the real direct launch; never replace the private supervisor code."""
    from myrk import checks
    original = checks.subprocess.Popen
    directory = Path(os.environ["MYRK_PROCESS_RECEIPT_DIRECTORY"])
    directory.mkdir(parents=True, exist_ok=True)

    async def verify_ancestry(process, identity, owner_observer: ProcessObserver) -> None:
        handled: set[Path] = set()
        try:
            while process.poll() is None:
                for request in directory.glob("*.ancestry-request"):
                    if request in handled:
                        continue
                    try:
                        data = json.loads(request.read_text(encoding="utf-8"))
                    except PermissionError:
                        continue
                    owner = TypeAdapter(Observation).validate_python(data["owner_observation"])
                    if owner.pid != process.pid:
                        continue
                    assert owner.expected == identity and process.poll() is None
                    actual_owner = owner_observer.status()[0]
                    assert actual_owner.identity == identity and actual_owner.ownership == "owned" and actual_owner.state == "running"
                    system = WindowsProcesses()
                    observer = ProcessObserver(system)
                    try:
                        nodes = []
                        pid = data["interpreter_pid"]
                        for _ in range(8):
                            actual = observer.capture(pid)
                            assert actual.identity is not None and actual.identity.created_filetime is not None and actual.identity.image is not None
                            parent, error = _parent_pid(system, pid)
                            assert error is None and parent is not None
                            if not nodes:
                                assert parent == data["reported_parent_pid"]
                            nodes.append((actual.identity, parent))
                            if parent == process.pid:
                                break
                            pid = parent
                        assert nodes[-1][1] == process.pid and process.poll() is None
                        identities = {node.pid: node for node, _ in nodes}
                        identities[process.pid] = identity
                        records = [CaptureCommand("capture", node.pid, expected=node,
                            direct_spawn=SpawnEvidence(node, parent, (), "", "fixture_receipt"),
                            parent=ParentEvidence(identities[parent], parent)) for node, parent in reversed(nodes)]
                        data.update(records=TypeAdapter(list[CaptureCommand]).dump_python(records, mode="json"),
                            reached_owner=True, external_ancestry=True, externally_held_launcher=process.pid,
                            external_chain=[{"pid": node.pid, "parent_pid": parent} for node, parent in nodes])
                        path = request.with_suffix(".json")
                        pending = path.with_suffix(".pending")
                        pending.write_text(json.dumps(data), encoding="utf-8")
                        pending.replace(path)
                        handled.add(request)
                    finally:
                        observer.close()
                await asyncio.sleep(.01)
        finally:
            owner_observer.close()

    def verified(task) -> None:
        if not task.cancelled() and task.exception() is not None:
            (directory / "ancestry-failure.txt").write_text("".join(traceback.format_exception(task.exception())), encoding="utf-8")

    def launch(argv, *args, **kwargs):
        if argv != checks.SUPERVISOR_RUNTIME.command():
            return original(argv, *args, **kwargs)
        receipt = directory / f"process-supervisor-{uuid4().hex}.json"
        kwargs["env"] = {**kwargs["env"], "MYRK_PROCESS_OWNER_FILE": str(receipt) + ".ack"}
        process = original(argv, *args, **kwargs)
        observer = ProcessObserver()
        try:
            first = observer.capture(process.pid)
            assert first.identity is not None and first.identity.created_filetime is not None and first.identity.image is not None
            assert process.poll() is None
            spawn = SpawnEvidence(first.identity, os.getpid(), tuple(argv), str(kwargs["cwd"]), "caller_spawn")
            actual = observer.capture(process.pid, expected=first.identity, direct_spawn=spawn)
            assert actual.ownership == "owned" and actual.state == "running" and process.poll() is None
            record = CaptureCommand("capture", process.pid, expected=first.identity, direct_spawn=spawn)
            pending = receipt.with_suffix(".pending")
            pending.write_text(json.dumps({"version": 1, "source": __file__, "interpreter_pid": process.pid,
                "records": [TypeAdapter(CaptureCommand).dump_python(record, mode="json")],
                "launcher_observation": TypeAdapter(Observation).dump_python(actual, mode="json")}), encoding="utf-8")
            pending.replace(receipt)
            verifying = asyncio.create_task(verify_ancestry(process, first.identity, observer))
            verifying.add_done_callback(verified)
        except BaseException:
            observer.close()
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=5)
            raise
        return process

    checks.subprocess.Popen = launch
