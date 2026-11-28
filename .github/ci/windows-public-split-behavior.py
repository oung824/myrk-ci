"""Run an exact offline test body with only its ordinary synthetic registry."""
import json
from pathlib import Path
import runpy
import shutil
import sys

ci = Path(__file__).resolve().parent
root = ci.parents[1]
group, = sys.argv[1:]
rows = json.loads((ci / "windows-public-split-selection.json").read_text(encoding="utf-8"))
selected = rows[group]
nodes = selected["node_ids"]
files = sorted({node.partition("::")[0] for node in nodes})
selectors = tuple(nodes) if selected.get("exact_arguments") else tuple(sorted({node.partition("[")[0] for node in nodes}))
plugin = root / "tests/support/github_split_behavior.py"
assert not plugin.exists()
shutil.copyfile(ci / "windows-public-split-plugin.py", plugin)
try:
    entry = ci / "windows-public-backend.py"
    backend = runpy.run_path(str(entry))
    backend["AUDITED_GROUPS"].clear()
    backend["AUDITED_GROUPS"][group] = tuple(path.removeprefix("tests/").removesuffix(".py") for path in files)
    backend["AUDITED_SELECTION"][group] = selectors
    managed = backend["run_pytest"]

    def run(summary, environment, phase, paths, *options):
        return managed(summary, environment, phase, paths, *options, "-p", "tests.support.github_split_behavior",
                       f"--myrk-split-group={group}")

    backend["main"].__globals__["run_pytest"] = run
    sys.argv = [str(entry), group, "--expected-count", str(selected["count"]),
                "--expected-node-digest", selected["node_ids_sha256"]]
    result = backend["main"]()
finally:
    plugin.unlink()
sys.exit(result)
