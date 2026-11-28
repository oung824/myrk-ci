"""Supply only generated material to the selected offline behavior tests."""

from hashlib import sha256
import json
from pathlib import Path

import pytest


MEMORY = {
    "QG4.formal.4": "The public workshop gauge read 47 units during one synthetic check.",
    "QG4.tuning.2": "The public bench timer started at 25 seconds during one synthetic check.",
}


def visible_input(task_id, family):
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    if task_id == "QG1.tuning.1":
        goal = "Read the public weather measurement for the demonstration site."
        sources = {"weather.txt": QualitySource(text="Synthetic demonstration site: 22 degrees.\n")}
        parameters = {"reads": ["weather.txt"]}
    elif task_id == "QG2.formal.3":
        goal = "Calculate the decimal sum of the public demonstration charges and run the independent check."
        sources = {"amounts.csv": QualitySource(text="entry,amount\nfirst,12.41\nsecond,1.79\n")}
        parameters = {"calculate": {"path": "amounts.csv", "column": "amount"}, "check": "decimal-sum"}
    elif task_id in MEMORY:
        original = MEMORY[task_id]
        goal = f"Read the public measurement and preserve this one observation only: {original}"
        sources = {"source.txt": QualitySource(text=original + "\n")}
        parameters = {"original": original}
        if task_id == "QG4.tuning.2":
            parameters["project"] = "public-projection-workshop"
    elif task_id in {"QG6.formal.6", "QG6.formal.9"}:
        correction = task_id == "QG6.formal.9"
        goal = ("Report the public roster sources with the current explicitly authorized shift correction."
                if correction else "Read the public inventory sources without writing or starting another task.")
        quantity = {"shifts": 9} if correction else {"units": 17}
        sources = {f"public-source-{number:02}.txt": QualitySource(
            text=f"Synthetic observation {number}. {json.dumps(quantity)}\n")
            for number in range(16 if correction else 32)}
        parameters = {"material_family_id": family, "source_documents": [{"facts": quantity}]}
        if correction:
            parameters.update(historical_shift_count=12, current_shift_count=9)
            sources["attendance.txt"] = QualitySource(text="Public exercise attendance: 20 participants.\n")
            sources["roster-check.txt"] = QualitySource(text="Public exercise: independent roster check.\n")
    else:
        raise ValueError(f"No public behavior fixture is declared for {task_id}.")
    group, purpose, slot = task_id.lower().split(".")
    adapter = f"{group}-tuning-{slot}" if purpose == "tuning" else f"{group}-{slot}"
    return QualityVisibleInput(task_id=task_id, goal=goal, adapter=adapter, sources=sources, parameters=parameters)


def build_public_packet(destination: Path, active_ids: frozenset[str]):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality import freeze_quality
    from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef
    from tests.support.agent_acceptance.quality_runtime import output_standards, required_outputs

    if destination.parent != SOURCE / PACKETS or destination.resolve() != destination.absolute():
        raise ValueError("The public quality packet must use a new, controlled folder.")
    destination.mkdir(parents=True, exist_ok=False)
    drafts = json.loads((SOURCE / "tests/acceptance/materials/quality/synthetic.json").read_bytes())
    for task in drafts:
        task_id = task["task_id"]
        family = f"public-behavior-{task_id.lower()}"
        task["material_family_id"] = family
        task["prior_uses"] = ["implementation"]
        input_text, gold_text = task.pop("input"), task.pop("gold")
        if task_id in active_ids:
            visible = visible_input(task_id, family)
            input_text = visible.model_dump_json()
            gold_text = json.dumps({"review_only": True, "task_id": task_id,
                "gold": {"example": "Public synthetic behavior calibration only."},
                "private_review_guard": f"public-synthetic-review-{task_id}"})
            task["objective"] = visible.goal
            task["authorization"] = visible.goal
            task["allowed_paths"] = list(visible.sources)
            task["required_outputs"] = required_outputs(visible)
            task["domain_standards"] = output_standards(visible) or {}
        for kind, content in (("input", input_text), ("gold", gold_text)):
            folder = destination / kind
            folder.mkdir(exist_ok=True)
            path = folder / f"{task_id}.json"
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(content)
            task[f"{kind}_path"] = path
    frozen = freeze_quality(
        drafts, destination=destination / "freeze.json", provenance="synthetic",
        intended_use="implementation_regression", source_version="public-synthetic-behavior",
        environment={"platform": "github-hosted-windows", "transport": "offline"},
        models={"deepseek": {"model": "deepseek-flash"}, "google": {"model": "gemini-2.5-flash"}},
        criteria_version="public-synthetic-behavior", qualification_families=[],
    )
    return QualityPacketRef(path=frozen, sha256=sha256(frozen.read_bytes()).hexdigest())


def pytest_addoption(parser):
    parser.addoption("--myrk-quality-packet-group", required=True)


@pytest.fixture(scope="module", autouse=True)
def public_quality_packet(request):
    group = request.config.getoption("--myrk-quality-packet-group")
    module = request.module
    if group == "quality-packet-summary":
        from tests.acceptance import test_quality_compaction_correction_instances as owner

        active_ids = frozenset({"QG6.formal.9"})
    elif group == "quality-packet-memory-budget":
        from tests.acceptance import test_quality_memory_aux as owner

        active_ids = frozenset(MEMORY)
    else:
        from tests.support.agent_acceptance import quality_materials

        owner = module if module.__name__.rsplit(".", 1)[-1] == "test_quality_answer_isolation" else quality_materials
        active_ids = frozenset({"QG1.tuning.1", "QG2.formal.3", "QG6.formal.6"})

    def generated(destination):
        return build_public_packet(destination, active_ids)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(owner, "build_implementation_quality_packet", generated)
        yield
