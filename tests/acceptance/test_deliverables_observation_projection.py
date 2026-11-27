"""Project real closed-run owner counters without repeating or regrading Main."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest


ORIGINALS = Path(__file__).parent / "materials/deliverables/owner-observation-4ef"


@pytest.mark.parametrize("protocol", ["deepseek", "google"])
@pytest.mark.parametrize("control", ["original", "missing-reopen", "missing-counter", "ordinary-profile", "malformed"])
def test_normal_profile_projection_requires_both_actual_phase_observations(tmp_path, protocol, control):
    from tests.support.agent_acceptance.deliverables import score

    index = json.loads((ORIGINALS / "index.json").read_bytes())
    row = index["entries"][protocol]
    owners = {}
    for phase, original in row["phases"].items():
        source = ORIGINALS / original["member"]
        assert source.stat().st_size == original["bytes"] and sha256(source.read_bytes()).hexdigest() == original["sha256"]
        target = tmp_path / phase / "owner/state.json"
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        owners[phase] = target
    expected = sum(json.loads(path.read_bytes())["normal_profile_accesses"] for path in owners.values())
    if control == "missing-reopen":
        owners["reopen"].unlink()
        expected = None
    elif control in {"missing-counter", "ordinary-profile"}:
        observed = json.loads(owners["work"].read_bytes())
        if control == "missing-counter":
            observed.pop("normal_profile_accesses")
            expected = None
        else:
            observed["synthetic_profile"] = False
            observed["normal_profile_accesses"] = 1
            expected = 1
        owners["work"].write_text(json.dumps(observed), encoding="utf-8")
    elif control == "malformed":
        owners["work"].write_bytes(b"{")
    before = {path: path.read_bytes() for path in owners.values() if path.exists()}
    if control == "malformed":
        with pytest.raises(json.JSONDecodeError):
            score("formal.deliverables", tmp_path, [], [])
    else:
        projected = score("formal.deliverables", tmp_path, [], [])
        assert projected["normal_profile_accesses"] == expected
        assert projected["passed"] is False and projected["product_execution"] == "incomplete"
    assert all(path.read_bytes() == content for path, content in before.items())
    assert all(sha256((ORIGINALS / source["member"]).read_bytes()).hexdigest() == source["sha256"] for source in row["phases"].values())
