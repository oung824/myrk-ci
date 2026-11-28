"""Seed collection metadata from the published ordinary acceptance handlers."""
import json
from pathlib import Path
import sys


PROTOCOL_GROUPS = {
    ("deepseek", "openai-chat", "google"): "three_protocols",
    ("deepseek",): "deepseek_only",
    ("deepseek", "openai-chat"): "deepseek_openai",
    ("openai-chat",): "openai_only",
}


def ordinary_protocol_groups() -> dict[str, list[str]]:
    from tests.support.agent_acceptance import CaseRegistry
    from tests.support.agent_acceptance.ordinary import register

    registry = CaseRegistry()
    register(registry)
    groups: dict[str, list[str]] = {group: [] for group in PROTOCOL_GROUPS.values()}
    for case in registry.cases.values():
        groups[PROTOCOL_GROUPS[case.protocols]].append(case.case_id)
    return groups


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    groups = ordinary_protocol_groups()
    destination = root / "tests/acceptance/materials/catalogue/required.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as fixture:
        json.dump(groups, fixture, indent=2)
        fixture.write("\n")
