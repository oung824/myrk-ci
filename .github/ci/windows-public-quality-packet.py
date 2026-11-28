"""Run selected behavior against a real freeze of generated public material."""

from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" or (
        os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise RuntimeError("This check requires a disposable GitHub-hosted Windows runner.")
    group, = sys.argv[1:]
    ci = Path(__file__).resolve().parent
    root = ci.parents[1]
    if root != Path(os.environ["GITHUB_WORKSPACE"]).resolve() or root != Path.cwd().resolve():
        raise RuntimeError("This check must run from the public checkout.")

    manifest = json.loads((ci / "windows-public-quality-packet-selection.json").read_bytes())
    nodes = tuple(manifest[group])
    payload = json.dumps(sorted(nodes), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = sha256(payload).hexdigest()
    files = tuple(dict.fromkeys(node.partition("::")[0] for node in nodes))
    test_modules = tuple(path.removeprefix("tests/").removesuffix(".py") for path in files)
    driver = ci / "windows-public-backend.py"
    backend = runpy.run_path(str(driver))
    backend["AUDITED_GROUPS"][group] = test_modules
    backend["AUDITED_SELECTION"][group] = nodes
    backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
    managed = backend["run_pytest"]

    def run(summary, environment, phase, paths, *options):
        return managed(summary, environment, phase, paths, *options,
                       "-p", "tests.support.github_public_quality_packet", f"--myrk-quality-packet-group={group}")

    backend["main"].__globals__["run_pytest"] = run
    sys.argv = [str(driver), group, "--expected-count", str(len(nodes)), "--expected-node-digest", digest]
    publish = backend["publish"]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    publish(summary, "- Generated packet fixture; true freeze, binding and selected original test bodies.")
    publish(summary, "- Coverage is paired behavior. Verification of original private material stays local.")
    packet_root = root / ".scratch/retro-agent-live-acceptance-20261116/quality-packets"
    if packet_root.resolve() != packet_root:
        raise RuntimeError("The public quality packet area must not follow a link.")
    plugin = root / "tests/support/github_public_quality_packet.py"
    source = (ci / "windows-public-quality-packet-bootstrap.py").read_bytes()
    packet_root.mkdir(parents=True, exist_ok=False)
    try:
        with plugin.open("xb") as installed:
            installed.write(source)
        try:
            result = backend["main"]()
        finally:
            plugin.unlink()
    finally:
        packet_root.rmdir()
    publish(summary, "- The generated packet area and temporary pytest plugin were removed.")
    return result


if __name__ == "__main__":
    sys.exit(main())
