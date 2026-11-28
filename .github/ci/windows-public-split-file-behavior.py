"""Verify exact file and approval behavior tests with the managed backend runner."""
import json
from pathlib import Path
import runpy
import sys


def main():
    group, = sys.argv[1:]
    ci = Path(__file__).resolve().parent
    selected = json.loads((ci / "windows-public-split-file-selection.json").read_bytes())[group]
    filename = selected["file"]
    nodes = tuple(selected["node_ids"])
    if not nodes or len(nodes) != len(set(nodes)) or any(node.partition("::")[0] != filename for node in nodes):
        raise ValueError("The behavior split requires exact unique selectors from its own test file.")
    plugin = ci.parents[1] / "tests/support/github_split_file_behavior.py"
    with plugin.open("xb") as installed:
        installed.write((ci / "windows-public-split-file-plugin.py").read_bytes())
    try:
        entry = ci / "windows-public-backend.py"
        backend = runpy.run_path(str(entry))
        backend["AUDITED_GROUPS"][group] = (filename.removeprefix("tests/").removesuffix(".py"),)
        backend["AUDITED_SELECTION"][group] = nodes
        backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
        managed = backend["run_pytest"]

        def run(summary, environment, phase, paths, *options):
            return managed(summary, environment, phase, paths, *options,
                           "-p", "tests.support.github_split_file_behavior")

        backend["main"].__globals__["run_pytest"] = run
        sys.argv = [str(entry), group, "--expected-count", str(selected["count"]),
                    "--expected-node-digest", selected["node_ids_sha256"]]
        return backend["main"]()
    finally:
        plugin.unlink()


if __name__ == "__main__":
    sys.exit(main())
