"""Generate neutral shutdown traces for the public matcher tests."""
import json
from pathlib import Path
import sys


ANCHORS = ("synthetic-context-close", "synthetic-generator-close",
           "reset-event-exit", "reset-product-exit", "synthetic-final-close")
AUXILIARIES = ("sdk-open-close", "sdk-event-close")
WRAPPERS = ("asyncgen_close", "loop_shutdown")


def rule(identifier: str, wrappers: tuple[str, ...]) -> dict[str, object]:
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "synthetic_shutdown.py", line 317, in close_context\n'
        "    _active_capture.reset(token)\n"
        '  File "synthetic_cleanup.py", line 12, in finalize\n'
        f"    fixture_close({identifier!r})\n"
        "ValueError: synthetic token at 0x<address> was created in a different Context"
    )
    return {"id": identifier, "wrapper_kinds": wrappers,
            "closing_asyncgen": "Fixture." + identifier.replace("-", "_"), "traceback": traceback}


def synthetic_rules() -> dict[str, object]:
    from tests.support.agent_acceptance.shutdown import NODE, SELECTED_NODE

    choices = ((), (AUXILIARIES[0],), (AUXILIARIES[1],), tuple(sorted(AUXILIARIES)))
    return {
        "rule_set": "reset-family-v1",
        "nodes": [NODE, SELECTED_NODE],
        "record_header": {"phase": "teardown", "logger": "asyncio", "level": "ERROR"},
        "log_prefix": "ERROR asyncio: ",
        "maximum_records": 3,
        "required_anchors": 1,
        "loop_message": "synthetic loop shutdown: name='Task-<ordinal>' coro=<fixture at 0x<address>>",
        "anchors": [rule(name, WRAPPERS if name != ANCHORS[-1] else WRAPPERS[:1]) for name in ANCHORS],
        "auxiliaries": [rule(name, WRAPPERS[:1]) for name in AUXILIARIES],
        "allowed_combinations": [
            {"anchor": anchor, "auxiliaries": list(auxiliary)} for anchor in ANCHORS for auxiliary in choices
        ],
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    rules = synthetic_rules()
    destination = root / "tests/acceptance/materials/shutdown/reset-family.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        json.dump(rules, output, indent=2)
        output.write("\n")
