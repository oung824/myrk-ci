from hashlib import sha256

import pytest

from tests.support.agent_acceptance import quality_recovery
from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput, model_prompt


@pytest.fixture
def registered_supplement(tmp_path):
    visible = QualityVisibleInput(
        task_id="QG5.tuning.1",
        goal="Read the exercise schedule and wait for the correction.",
        adapter="qg5-tuning-1",
        sources={
            "lesson.txt": QualitySource(
                text="lesson=LR-42\r\ntime=14:10\r\noptional_invitation=unadopted\r\n",
                encoding="utf-8-sig",
            ),
            "SL-08.txt": QualitySource(text="sign_in_sheet=SL-08\r\nattendance=not_provided\r\n"),
        },
        parameters={"workflow": {
            "subject": "lesson", "code": "LR-42", "before": "14:10", "after": "14:35",
            "filename": "lesson.txt", "optional": "invitation", "queue_code": "SL-08",
        }},
    )
    folder = tmp_path / "materials"
    folder.mkdir()
    for name, source in visible.sources.items():
        (folder / name).write_bytes(source.text.encode(source.encoding))
    return visible, folder


def test_supplement_setup_preserves_the_registered_target_and_queue_bytes(tmp_path, registered_supplement):
    visible, folder = registered_supplement
    quality_recovery.setup(visible, tmp_path, "deepseek")
    assert (folder / "lesson.txt").read_bytes() == (
        b"\xef\xbb\xbflesson=LR-42\r\ntime=14:10\r\noptional_invitation=unadopted\r\n"
    )
    assert (folder / "SL-08.txt").read_bytes() == b"sign_in_sheet=SL-08\r\nattendance=not_provided\r\n"


@pytest.mark.parametrize("queued", [False, True])
def test_supplement_separates_registered_rule_correction_and_queue(tmp_path, registered_supplement, queued):
    visible, folder = registered_supplement
    workflow = {**visible.parameters["workflow"], "queue_code": "SL-08" if queued else None}
    rule = QualitySource(text="Use the signed lesson start as the base for the revised delay.\n")
    sources = {"lesson.txt": visible.sources["lesson.txt"], "old-rule.txt": rule}
    brief = {"read_only_source": "old-rule.txt", "initial": "Check the signed time base.",
             "supplement": "Use a twenty-five-minute delay from the signed time base."}
    if queued:
        sources["SL-08.txt"] = QualitySource(text="The recorded signature applies to the adjacent rehearsal only.\r\n")
        brief["queue"] = "Check whether the signature applies to this rehearsal."
    for name, source in sources.items():
        (folder / name).write_bytes(source.text.encode(source.encoding))
    visible = visible.model_copy(update={"task_id": "QG5.tuning.1" if queued else "QG5.formal.1",
        "adapter": "qg5-tuning-1" if queued else "qg5-1", "sources": sources,
        "parameters": {"workflow": workflow, "supplement_brief": brief}})

    config = quality_recovery.manifest(visible, tmp_path, "deepseek")
    assert quality_recovery.current_sources(visible) == ["lesson.txt", "old-rule.txt"]
    assert "old-rule.txt" in config["prompt"] and brief["initial"] in config["prompt"]
    recovery = config["quality_recovery"]
    assert brief["supplement"] in recovery["supplement"] and workflow["after"] not in recovery["supplement"]
    assert brief["supplement"] not in config["prompt"] and "SL-08.txt" not in config["prompt"]
    assert "SL-08.txt" not in model_prompt(visible)
    assert quality_recovery.project_input(visible).sources == sources
    quality_recovery.setup(visible, tmp_path, "deepseek")
    assert (folder / "old-rule.txt").read_bytes() == rule.text.encode()
    if queued:
        queue = recovery["queue"][0]
        assert "SL-08.txt" in queue["prompt"] and brief["queue"] in queue["prompt"]
        assert queue["scope"]["allowed_actions"] == ["read"]
        assert "signatures were not provided" not in quality_recovery.replies(visible, tmp_path, "deepseek")[-1].text
    assert len(recovery["queue"]) == int(queued)


@pytest.mark.parametrize("name", ["lesson.txt", "SL-08.txt"])
def test_supplement_setup_refuses_to_overwrite_registered_path_aliases(tmp_path, registered_supplement, name):
    visible, folder = registered_supplement
    source = visible.sources[name]
    aliased = {other: value for other, value in visible.sources.items() if other != name}
    aliased[name.swapcase()] = source
    (folder / name).rename(folder / name.swapcase())
    visible = visible.model_copy(update={"sources": aliased})
    with pytest.raises(ValueError, match="spelling"):
        quality_recovery.setup(visible, tmp_path, "deepseek")
    assert (folder / name.swapcase()).read_bytes() == source.text.encode(source.encoding)


@pytest.mark.parametrize(("problem", "scope_status"), [
    (None, "passed"),
    ("changed_peer", "unknown"),
    ("missing_peer", "unknown"),
    ("wrong_original", "unknown"),
    ("missing_backup", "unknown"),
    ("extra_change", "failed"),
    ("mismatched_readback", "unknown"),
    ("failed_with_wrong_path_effect", "failed"),
    ("early_edit", "failed"),
    ("unrecorded_edit", "unknown"),
    ("queued_supplement", "unknown"),
])
def test_supplement_score_requires_only_the_authorized_frozen_source_edit(
    tmp_path, registered_supplement, problem, scope_status,
):
    visible, folder = registered_supplement
    quality_recovery.setup(visible, tmp_path, "deepseek")
    target = folder / "lesson.txt"
    before = b"\xef\xbb\xbflesson=LR-42\r\ntime=14:10\r\noptional_invitation=unadopted\r\n"
    after = b"\xef\xbb\xbflesson=LR-42\r\ntime=14:35\r\noptional_invitation=unadopted\r\n"
    backup = tmp_path / "records/lesson-evidence/edit-1-original.txt"
    backup.parent.mkdir(parents=True)
    if problem != "missing_backup":
        backup.write_bytes(before.replace(b"14:10", b"13:10") if problem == "wrong_original" else before)
    target.write_bytes(after + b"\r\n" if problem in {"extra_change", "mismatched_readback"} else after)
    if problem == "changed_peer":
        (folder / "SL-08.txt").write_bytes(b"sign_in_sheet=SL-08\r\nattendance=8\r\n")
    if problem == "missing_peer":
        (folder / "SL-08.txt").unlink()

    action = {
        "id": "edit-1", "task_id": "task-lesson", "kind": "edit", "effect": "completed",
        "status": "failed" if problem == "failed_with_wrong_path_effect" else "completed",
        "prepared_request": 1 if problem == "early_edit" else 3,
        "prepared_parameters": {
            "path": str(folder / "SL-08.txt" if problem == "failed_with_wrong_path_effect" else target),
            "old_text": "14:10", "new_text": "14:35",
            "read_sha256": sha256(before).hexdigest(),
        },
        "before_sha256": sha256(before).hexdigest(),
        "after_sha256": sha256(after if problem == "mismatched_readback" else target.read_bytes()).hexdigest(),
        "backup_path": str(backup),
    }
    supplements = [{"applied_request": 2, "text": quality_recovery.supplement_prompt(visible)}]
    turns = [{
        "status": "completed", "actions": [] if problem == "unrecorded_edit" else [action],
        "task": {"id": "task-lesson", "supplements": [] if problem == "queued_supplement" else supplements},
    }]
    if problem == "queued_supplement":
        turns.append({"status": "completed", "actions": [], "task": {"id": "task-queue", "supplements": supplements}})
    evidence = quality_recovery.score(
        visible, tmp_path, [{"turns": turns}], [{"exchanges": [{"request_utf8": "Read the current lesson time, 14:10."}]}],
    )
    assert evidence["passed"] is (problem is None), evidence
    assert evidence["critical_contracts"]["K1"] == scope_status
