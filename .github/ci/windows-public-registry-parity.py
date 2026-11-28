"""Run isolated assertions against published registration and reporting code."""

from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys


SELECTION = {
    "historical-cli-report":
        "tests/acceptance/test_historical_reuse_regrading.py::test_builtin_run_and_cli_report_keep_historical_groups_separate",
    "caller-only-readiness":
        "tests/acceptance/test_quality_materials_human_gate.py::test_a_caller_only_registry_cannot_claim_real_formal_material_readiness",
}


def main():
    group, = sys.argv[1:]
    node = SELECTION[group]
    test_file = node.partition("::")[0]
    ci = Path(__file__).resolve().parent
    root = ci.parents[1]
    plugin = root / "tests/support/github_registry_parity.py"

    with ExitStack() as cleanup:
        if group == "historical-cli-report":
            history = root / "tests/acceptance/materials/history/index.json"
            history.parent.mkdir(parents=True, exist_ok=True)
            with history.open("xb") as index:
                cleanup.callback(history.unlink)
                index.write((ci / "fixtures/empty-acceptance-history.json").read_bytes())
        with plugin.open("xb") as installed:
            cleanup.callback(plugin.unlink)
            installed.write((ci / "windows-public-registry-parity-plugin.py").read_bytes())

        entry = ci / "windows-public-backend.py"
        backend = runpy.run_path(str(entry))
        backend["AUDITED_GROUPS"][group] = (test_file.removeprefix("tests/").removesuffix(".py"),)
        backend["AUDITED_SELECTION"][group] = (node,)
        backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
        managed = backend["run_pytest"]

        def run(summary, environment, phase, paths, *options):
            return managed(summary, environment, phase, paths, *options, "-p", "tests.support.github_registry_parity")

        backend["main"].__globals__["run_pytest"] = run
        digest = sha256(json.dumps([node], separators=(",", ":")).encode("utf-8")).hexdigest()
        sys.argv = [str(entry), group, "--expected-count", "1", "--expected-node-digest", digest]
        return backend["main"]()


if __name__ == "__main__":
    sys.exit(main())
