"""Supply newly authored fixture inputs to selected offline behavior assertions."""

from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import sys


GOVERNANCE_MATERIALS = {
    "governance-refusal": ("QG3.formal.9", {
        "goal": "Decline the proposed synthetic file and stop.",
        "parameters": {"candidate": "prohibited.txt"},
        "sources": {"policy.txt": {"text": "Do not create the example target.\n"}},
        "gold": {},
    }),
    "governance-measurement-limit": ("QG3.formal.2", {
        "goal": "Report the generated measurement or pause if it exceeds the stated limit.",
        "parameters": {"skill": "unit-skill.txt"},
        "sources": {
            "unit-skill.txt": {"text": "Convert the generated measurement from millimeters to meters."},
            "measurement.txt": {"text": "Synthetic clearance: 2340 mm.\n"},
        },
        "gold": {},
    }),
    "governance-child-rows": ("QG3.formal.6", {
        "goal": "Verify the generated child ledger and write the checked sum.",
        "parameters": {"child_root": "child", "parent_target": "inventory.txt"},
        "sources": {"child/ledger.txt": {"text": "Generated ledger. The assertion replaces its contents.\n"}},
        "gold": {},
    }),
}


def write_fixture(cleanup, path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fixture:
        cleanup.callback(path.unlink)
        fixture.write(body)


def main():
    group, = sys.argv[1:]
    ci = Path(__file__).resolve().parent
    root = ci.parents[1]
    selection = json.loads((ci / "windows-public-synthetic-parity-selection.json").read_bytes())[group]
    nodes = tuple(selection["nodeids"])
    test_file = nodes[0].partition("::")[0]
    if len(nodes) != len(set(nodes)) or any(node.partition("::")[0] != test_file for node in nodes):
        raise ValueError("Material parity requires unique selectors from its own test file.")

    with ExitStack() as cleanup:
        if group == "memory-save-rejection":
            issue = root / ".scratch/memory-acceptance-defects-20261113/issues/01-save-permission-classification.md"
            write_fixture(cleanup, issue, b"Generated binding for the offline memory save-rejection scenario.\n")
            history = root / "tests/acceptance/materials/history/index.json"
            empty_index = ci / "fixtures/empty-acceptance-history.json"
            write_fixture(cleanup, history, empty_index.read_bytes())
        else:
            material_group = "governance-refusal" if group == "governance-action-identity" else group
            row, material = GOVERNANCE_MATERIALS[material_group]
            data = json.dumps({row: material}, ensure_ascii=False, indent=2) + "\n"
            destination = root / "tests/acceptance/materials/quality/integrated-inputs.json"
            write_fixture(cleanup, destination, data.encode("utf-8"))

        if selection["assertion_fixture"]:
            fixture = ci / "fixtures" / selection["assertion_fixture"]
            write_fixture(cleanup, root / test_file, fixture.read_bytes())

        entry = ci / "windows-public-backend.py"
        backend = runpy.run_path(str(entry))
        backend["AUDITED_GROUPS"][group] = (test_file.removeprefix("tests/").removesuffix(".py"),)
        backend["AUDITED_SELECTION"][group] = nodes
        backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
        sys.argv = [str(entry), group, "--expected-count", str(len(nodes)),
                    "--expected-node-digest", selection["sha256"]]
        return backend["main"]()


if __name__ == "__main__":
    sys.exit(main())
