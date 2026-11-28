"""Run the exact public Ticket 04 retest through the managed backend launcher."""
from pathlib import Path
import runpy
import sys


SCOPES = {
    "artifacts": (("test_artifacts", "test_artifact_reopen"), 24, "39f15135a601a82f02fbd382f27a0149091509e7cbb00001e427140092e5239d"),
    "conversation": (("test_conversation", "test_conversation_recovery", "test_conversation_tools",
                      "test_conversation_execution"), 45, "1d60c46507099b2cd39a09f35bebc5e197a63f4681cf6919252ba6fb044910a1"),
    "controls": (("acceptance/test_integration_contract",), 11, "4d026b2763e255447b034497b1f5da61c34595ce8280ebbab0b91b4ca3d071cb"),
}


def main(scope):
    names, count, digest = SCOPES[scope]
    engine = Path(__file__).with_name("windows-public-backend.py")
    backend = runpy.run_path(str(engine))
    group = "audited-integration-controls" if scope == "controls" else f"ticket-04-{scope}"
    backend["AUDITED_GROUPS"].clear()
    backend["AUDITED_GROUPS"][group] = names
    backend["AUDITED_DIAGNOSTIC_GROUPS"].add(group)
    publish = backend["publish_test_diagnostic"]

    def publish_focus(summary, xml_path, _first_path, outcomes):
        for position, (_node, outcome) in enumerate(sorted(outcomes.items()), 1):
            if outcome in {"failed", "skipped", "incomplete"}:
                backend["publish"](summary, f"- Audited node {position}: {outcome}.")
        for name in names:
            publish(summary, xml_path, f"tests/{name}.py", outcomes)

    backend["main"].__globals__["publish_test_diagnostic"] = publish_focus
    sys.argv = [str(engine), group, "--expected-count", str(count), "--expected-node-digest", digest]
    return backend["main"]()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
