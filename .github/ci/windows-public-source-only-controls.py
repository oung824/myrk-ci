"""Run isolated assertions whose inputs come from public source."""
from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import sys


SELECTION = {
    "ordinary-protocols": (
        "tests/acceptance/test_public_coverage.py::test_public_native_parameters_cover_the_required_protocols[ordinary.-21]",
    ),
    "delegated-recovery-shape": (
        "tests/acceptance/test_quality_legacy_fixtures.py::test_current_delegated_recovery_requires_structured_facts_and_side_source[resume_facts]",
        "tests/acceptance/test_quality_legacy_fixtures.py::test_current_delegated_recovery_requires_structured_facts_and_side_source[side/source.txt]",
    ),
}


def main():
    group, count, digest = sys.argv[1:]
    ci = Path(__file__).resolve().parent
    root = ci.parents[1]
    nodes = SELECTION[group]
    test_file = nodes[0].partition("::")[0]
    sys.path.insert(0, str(root))
    with ExitStack() as cleanup:
        if group == "ordinary-protocols":
            generator = runpy.run_path(str(ci / "windows-public-seed-ordinary-protocols.py"))
            groups = generator["ordinary_protocol_groups"]()
            destination = root / "tests/acceptance/materials/catalogue/required.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as catalogue:
                cleanup.callback(destination.unlink)
                json.dump(groups, catalogue, indent=2)
                catalogue.write("\n")
        else:
            fixture = ci / "fixtures/quality-legacy-delegated-shape.py"
            destination = root / test_file
            with destination.open("xb") as projected:
                cleanup.callback(destination.unlink)
                projected.write(fixture.read_bytes())

        entry = ci / "windows-public-backend.py"
        backend = runpy.run_path(str(entry))
        backend["AUDITED_GROUPS"][group] = (test_file.removeprefix("tests/").removesuffix(".py"),)
        backend["AUDITED_SELECTION"][group] = nodes
        backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
        sys.argv = [str(entry), group, "--expected-count", count, "--expected-node-digest", digest]
        return backend["main"]()


if __name__ == "__main__":
    sys.exit(main())
