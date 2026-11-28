"""Convert only the identified originals used by early quality regressions."""

from hashlib import sha256
from pathlib import Path

from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_packet import QualityPacketRef
from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput


_VOLUNTEER_TEMPLATE = SOURCE / "tests/acceptance/materials/compaction/continuity/qg6/volunteer-roster.input.json"
_VOLUNTEER_TEMPLATE_SHA256 = "893c7122ac47b8728d8a2f64e3a5e566e3c32f9f042096caba11b21fd4aeeca0"
_FROZEN_VOLUNTEER_INPUT_SHA256 = "69c3fa1c539c41d8773efc177455a5019c390c5f7e11afce3815dd8bcae45066"
_CARGO_TEMPLATE = SOURCE / "tests/acceptance/materials/save-import-recovery/qg5-8.txt"
_CARGO_TEMPLATE_SHA256 = "68cb86a93a4933e9976b3af62429eff7c6493e777c5b39864419344d63aa60d0"
_LEGACY_PACKET_SHA256 = frozenset({
    "b691bf576b3607c8c1e9b1762d8524606dd1db3b1900668cd6c1216e57ad536a",
    "c106426edecda5972ed3834a6b19e3377c225f7f1aeb4fc035147a288217b0c4",
    "53a98389ebcfd731f451be20a403aa5cb4af74299eb48f07574a6229645ef960",
})


def _volunteer_roster(visible: QualityVisibleInput) -> QualityVisibleInput:
    return visible.model_copy(update={
        "parameters": {**visible.parameters, "historical_shift_count": 12, "current_shift_count": 9},
        "sources": {**visible.sources,
            "attendance.txt": QualitySource(text="Synthetic implementation source: completed attendance will be established only by an actual read. volunteers=31.\n"),
            "roster-check.txt": QualitySource(text="Synthetic implementation roster-check source. No check is complete until its actual read returns.\n")},
    })


def convert_frozen_input(visible: QualityVisibleInput, original: bytes,
                         reference: QualityPacketRef | None) -> QualityVisibleInput:
    if (visible.adapter == "qg6-9" and reference is not None and reference.sha256 in _LEGACY_PACKET_SHA256
            and sha256(original).hexdigest() == _FROZEN_VOLUNTEER_INPUT_SHA256):
        return _volunteer_roster(visible)
    return visible


def convert_implementation_template(visible: QualityVisibleInput, source: Path, original: bytes,
                                    family: str) -> QualityVisibleInput:
    if (visible.adapter, source, family) == ("qg6-9", _VOLUNTEER_TEMPLATE, "volunteer-roster"):
        if sha256(original.replace(b"\r\n", b"\n")).hexdigest() == _VOLUNTEER_TEMPLATE_SHA256:
            return _volunteer_roster(visible)
    if (visible.adapter, source, family) == ("qg5-8", _CARGO_TEMPLATE, "qg5-recovery-independent-8"):
        if sha256(original.replace(b"\r\n", b"\n")).hexdigest() == _CARGO_TEMPLATE_SHA256:
            return visible.model_copy(update={
                "parameters": {**visible.parameters, "resume_facts": {
                    "subject": "货站装箱F箱", "target": "cargo-label.txt",
                    "before": "货站F箱标签=旧版\n", "after": "货站F箱标签=更正版\n",
                    "remaining": "F箱已更正；子任务仅回查side原来源，装箱备注仍未完成。重开继续原父任务，不重复更正或子任务。"}},
                "sources": {**visible.sources, "side/source.txt": QualitySource(
                    text="货站F箱侧记：装箱备注仍未完成。此子来源不授予修改父标签或跨目录操作的权限。\n")},
            })
    return visible
