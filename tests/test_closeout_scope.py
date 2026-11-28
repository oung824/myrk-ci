"""Exercise task closeout and project audit with interleaved checks."""

import json
from pathlib import Path

from tests.test_closeout import cli, git, manifest, repo, snapshot
from tests.test_closeout_borrowed import pinned
from tests.test_closeout_runs import prepare_runner, run_check


def test_foreign_run_can_arrive_change_and_be_removed_during_closeout(repo: Path) -> None:
    prepare_runner(repo)
    legacy = repo / ".scratch/check-runs/results/b-earlier.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"exit_code": 0, "started_at": "2020-01-09T16:40:00+00:00",
                                  "source_root": str(repo)}), encoding="utf-8")
    baseline = snapshot(repo, "--verify-runs")
    assert str(legacy) not in {item["path"] for item in json.loads(baseline.read_text())["files"]}
    own = run_check(repo, "task-1", "pytest", "tests/test_first.py", "-q")
    assert own["exit_code"] == 0, own
    receipt = Path(own["result_path"])
    launch = repo / ".scratch/check-runs/launches" / f"{own['invocation_id']}.json"
    evidence = [receipt, launch]

    foreign = run_check(repo, "task-2", "pytest", "tests/test_second.py", "-q")
    assert foreign["exit_code"] == 0, foreign
    unrelated = Path(foreign["result_path"])
    files = [{"path": str(path), "outcome": "committed"} for path in evidence]
    files.append({**pinned(unrelated), "outcome": "external", "owner": "task-2", "reason": "Prior broad inventory"})
    resources = manifest(repo, baseline, files)
    git(repo, "add", "-f", "--", *(str(path) for path in evidence))
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report["errors"]
    assert str(unrelated) not in {item["path"] for item in report["candidates"]}
    assert [run["invocation_id"] for run in report["check_runs"]] == [own["invocation_id"]]

    process, prior_audit = cli(repo, "audit")
    assert process.returncode == 0, prior_audit
    observed = {item["path"]: item for item in prior_audit["records"]}
    assert observed[str(unrelated)]["task_id"] == "task-2"
    assert observed[str(legacy)]["ownership"] == "unresolved"
    audit_path = repo / ".closeout/prior-audit.json"
    audit_path.write_text(json.dumps(prior_audit), encoding="utf-8")

    git(repo, "commit", "-m", "Deliver the task check evidence")
    unrelated.write_bytes(unrelated.read_bytes() + b"\n")
    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 0, report["errors"]
    process, audit = cli(repo, "audit", "--previous", str(audit_path))
    assert process.returncode == 0, audit
    assert str(unrelated) in audit["changes"]["changed"]
    changed = next(item for item in audit["records"] if item["path"] == str(unrelated))
    assert (changed["ownership"], changed["claimed_task_id"]) == ("unresolved", "task-2")

    conflicting = json.loads(unrelated.read_text())
    conflicting["task_id"] = "task-1"
    unrelated.write_text(json.dumps(conflicting), encoding="utf-8")
    for phase in ("preflight", "check"):
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 1, report
        assert any(str(unrelated) in error and "without a matching launch" in error for error in report["errors"])

    unrelated.unlink()
    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 0, report["errors"]
    process, audit = cli(repo, "audit", "--previous", str(audit_path))
    assert process.returncode == 0, audit
    assert str(unrelated) in audit["changes"]["removed"]


def test_foreign_receipt_in_this_proposed_index_needs_a_disposition(repo: Path) -> None:
    prepare_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    resources = manifest(repo, baseline, [])
    foreign = run_check(repo, "task-2", "pytest", "tests/test_second.py", "-q")
    assert foreign["exit_code"] == 0, foreign
    receipt = foreign["result_path"]
    git(repo, "add", "-f", "--", receipt)

    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 1, report
    assert receipt in report["unclassified"]


def test_incoming_foreign_receipt_is_ignored_across_file_sources(repo: Path) -> None:
    prepare_runner(repo)
    foreign = run_check(repo, "task-2", "pytest", "tests/test_second.py", "-q")
    assert foreign["exit_code"] == 0, foreign
    receipt = Path(foreign["result_path"])
    archived = receipt.with_name("b-archived.json")
    archived.write_bytes(receipt.read_bytes())
    git(repo, "add", "-f", "--", str(archived))
    git(repo, "commit", "-m", "Add producer's earlier archived receipt")
    source = repo / ".scratch/w"
    git(repo, "worktree", "add", "-b", "delivery", str(source))
    source_copy = source / archived.relative_to(repo)
    archived.write_bytes(archived.read_bytes() + b"\n")
    source_copy.write_bytes(source_copy.read_bytes() + b"\n")

    ref = "refs/heads/delivery"
    baseline = snapshot(source, "--verify-runs", "--scope", str(receipt.parent), target_ref=ref)
    scoped_baseline = json.loads(baseline.read_text())
    assert not {str(path) for path in (receipt, archived, source_copy)} & {item["path"] for item in scoped_baseline["files"]}
    status, old = cli(source, "snapshot", "--task-id", "task-1", "--target-ref", ref)
    assert status.returncode == 0, old
    assert {str(path) for path in (receipt, archived, source_copy)} <= {item["path"] for item in old["files"]}
    old["runs_observed_at"] = scoped_baseline["runs_observed_at"]
    baseline.write_text(json.dumps(old), encoding="utf-8")

    git(repo, "add", "-f", "--", str(receipt))
    git(repo, "commit", "--only", "-m", "Producer adds its own receipt", "--", str(receipt))
    git(source, "merge", "--ff-only", "main")
    integrated = source / receipt.relative_to(repo)
    resources = manifest(source, baseline, [])

    attributes = repo / ".git/info/attributes"
    attributes.write_text(f"{source_copy.relative_to(source).as_posix()} filter=writer\n", encoding="utf-8")
    git(repo, "config", "filter.writer.clean", "echo executed > filter-marker.txt")
    excluded = {str(path) for path in (receipt, archived, source_copy, integrated)}
    for phase in ("preflight", "check"):
        process, report = cli(source, phase, str(resources))
        assert process.returncode == 0, report["errors"]
        assert excluded.isdisjoint(item["path"] for item in report["files"])
    assert not (repo / "filter-marker.txt").exists()
    assert not (source / "filter-marker.txt").exists()

    process, audit = cli(repo, "audit")
    assert process.returncode == 0, audit
    assert excluded <= {item["path"] for item in audit["records"]}
