import pytest

from tests.support.agent_acceptance import quality_recovery
from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput


@pytest.mark.parametrize("absent", ["resume_facts", "side/source.txt"])
def test_current_delegated_recovery_requires_structured_facts_and_side_source(absent):
    parameters = {"source_id": "current-freight-depot", "resume_facts": {
        "subject": "当前货站F箱标签", "target": "crate.txt", "before": "label=pending\n",
        "after": "label=verified\n", "remaining": "Verify the manifest note."}}
    sources = {"source.txt": QualitySource(text="货站装箱 F箱 side。当前来源包含相似用语。\n"),
        "crate.txt": QualitySource(text="label=pending\n"),
        "side/source.txt": QualitySource(text="Current manifest note.\n")}
    if absent == "resume_facts":
        del parameters[absent]
    else:
        del sources[absent]
    visible = QualityVisibleInput(task_id="QG5.formal.8", adapter="qg5-8", goal="Verify the current crate label.",
        parameters=parameters, sources=sources)

    with pytest.raises(ValueError, match=absent):
        quality_recovery.project_input(visible)
