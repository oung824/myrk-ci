"""Run selected b713 formal test bodies that construct their own input."""
import json
from pathlib import Path
import runpy
import shutil
import sys

ci = Path(__file__).resolve().parent
source = ci.parents[1]
scope, = sys.argv[1:]
selected = json.loads((ci / "windows-isolated-formal-selection.json").read_text(encoding="utf-8"))[scope]
engine = ci / "windows-public-backend.py"
backend = runpy.run_path(str(engine))
backend["AUDITED_GROUPS"].clear()
backend["AUDITED_GROUPS"][scope] = (selected["file"].removeprefix("tests/").removesuffix(".py"),)
backend["AUDITED_SELECTION"][scope] = tuple(selected["node_ids"])
plugin = source / "tests/support/github_public_stop_fixture.py"
if selected["use_stop_fixture"]:
    assert not plugin.exists()
    shutil.copyfile(ci / "windows-public-stop-fixture.py", plugin)
    managed = backend["run_pytest"]

    def run(summary, environment, phase, paths, *options):
        return managed(summary, environment, phase, paths, *options, "-p", "tests.support.github_public_stop_fixture")

    backend["main"].__globals__["run_pytest"] = run
try:
    sys.argv = [str(engine), scope, "--expected-count", str(selected["count"]),
                "--expected-node-digest", selected["node_ids_sha256"]]
    code = backend["main"]()
finally:
    if selected["use_stop_fixture"]:
        plugin.unlink()
sys.exit(code)
