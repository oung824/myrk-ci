"""Verify the task closeout through the installed Git commit hook."""

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import subprocess

from tests.test_closeout import cli, snapshot
from tests.test_closeout_runs import prepare_runner, run_check
from tests.test_pre_commit import fixture_environment, git, repository


CONTROL_NAMES = (".closeout/baseline.json", ".closeout/resources.json")


def commit(root: Path, *names: str, manifest: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = fixture_environment()
    if manifest is not None:
        environment["MYRK_CLOSEOUT_MANIFEST"] = str(manifest)
    selection = ("--only", "--", *names) if names else ()
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
         "commit", "-m", "Deliver selected files", *selection],
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def write_manifest(source: Path, baseline: Path, files: list[dict[str, object]]) -> Path:
    resources = baseline.with_name("resources.json")
    controls = [{"path": str(path), "outcome": "committed"} for path in (baseline, resources)]
    facts = json.loads(baseline.read_text(encoding="utf-8"))
    resources.write_text(json.dumps({"project_root": facts["project_root"], "closeout": {
        "baseline": str(baseline), "task_id": facts["task_id"], "target_ref": facts["target_ref"],
        "files": [*controls, *files],
    }}) + "\n", encoding="utf-8")
    names = [path.relative_to(source).as_posix() for path in (baseline, resources)]
    staged = git(source, "add", "-f", "--", *names)
    assert staged.returncode == 0, staged.stderr
    return resources


def preflight_result(result: subprocess.CompletedProcess[str]) -> dict:
    output = result.stdout + result.stderr
    documents = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    document = next((value for value in documents if value.get("phase") == "preflight"), None)
    assert document is not None, output
    return document


def test_commit_only_rejects_external_disposition_for_selected_receipt(repository: Path) -> None:
    selected = ".scratch/check-runs/results/b-selected.json"
    receipt = repository / selected
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b'{"task_id":"task-2","exit_code":1}\n')
    added = git(repository, "add", "-f", "--", selected)
    assert added.returncode == 0, added.stderr
    original = commit(repository)
    assert original.returncode == 0, original.stdout + original.stderr
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--verify-runs", "--scope", ".closeout", target_ref=ref)
    receipt.write_bytes(b'{"task_id":"task-2","exit_code":0}\n')
    resources = write_manifest(repository, baseline, [{
        "path": selected, "outcome": "external", "owner": "task-2", "reason": "Original producer",
        "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }])

    rejected = commit(repository, selected, *CONTROL_NAMES, manifest=resources)

    document = preflight_result(rejected)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert document["errors"] == [f"Proposed check file must be declared committed: {receipt}"]


def test_commit_only_checks_each_selected_file_and_keeps_other_staging(repository: Path) -> None:
    results = ".scratch/check-runs/results/"
    receipt, companion, deletion, unrelated = (repository / results / name for name in (
        "b-selected.json", "reports/selected.txt", "b-deleted.json", "b-unrelated.json",
    ))
    for path in (receipt, deletion, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"task_id":"task-2","exit_code":1}\n')
    companion.parent.mkdir()
    companion.write_bytes(b"Earlier check report\n")
    selected = [path.relative_to(repository).as_posix() for path in (receipt, companion, deletion)]
    other_name = unrelated.relative_to(repository).as_posix()
    added = git(repository, "add", "-f", "--", *selected, other_name)
    assert added.returncode == 0, added.stderr
    original = commit(repository)
    assert original.returncode == 0, original.stdout + original.stderr
    head = git(repository, "rev-parse", "HEAD").stdout
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--verify-runs", "--scope", ".closeout", target_ref=ref)

    unrelated.write_bytes(b'{"task_id":"task-2","exit_code":2}\n')
    added = git(repository, "add", "--", other_name)
    assert added.returncode == 0, added.stderr
    receipt.write_bytes(b'{"task_id":"task-2","exit_code":0}\r\n')
    companion.write_bytes(b"Final check report\r\n")
    deletion.unlink()
    resources = write_manifest(repository, baseline, [])

    rejected = commit(repository, *selected, *CONTROL_NAMES, manifest=resources)
    report = preflight_result(rejected)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert set(report["unclassified"]) == {str(path) for path in (receipt, companion, deletion)}
    assert git(repository, "rev-parse", "HEAD").stdout == head

    declarations: list[dict[str, object]] = [{"path": name, "outcome": "committed"} for name in selected]
    declarations[-1]["deletion"] = True
    resources = write_manifest(repository, baseline, declarations)
    accepted = commit(repository, *selected, *CONTROL_NAMES, manifest=resources)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    report = preflight_result(accepted)
    assert report["ok"] is True, report
    assert report["final_verification_required"] is True, report
    assert str(unrelated) not in {item["path"] for item in report["candidates"]}
    assert git(repository, "show", f"HEAD:{selected[0]}").stdout == '{"task_id":"task-2","exit_code":0}\n'
    assert git(repository, "show", f"HEAD:{selected[1]}").stdout == "Final check report\n"
    assert git(repository, "cat-file", "-e", f"HEAD:{selected[2]}").returncode != 0
    assert git(repository, "diff", "--cached", "--name-only").stdout.splitlines() == [other_name]
    final, report = cli(repository, "check", str(resources), "--commit", "HEAD")
    assert final.returncode == 0, report


def test_commit_only_rejects_a_receipt_changed_after_git_prepares_the_index(repository: Path) -> None:
    selected = ".scratch/check-runs/results/b-selected.json"
    receipt = repository / selected
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b'{"task_id":"task-2","exit_code":1}\n')
    added = git(repository, "add", "-f", "--", selected)
    assert added.returncode == 0, added.stderr
    original = commit(repository)
    assert original.returncode == 0, original.stdout + original.stderr
    head = git(repository, "rev-parse", "HEAD").stdout
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--verify-runs", "--scope", ".closeout", target_ref=ref)
    receipt.write_bytes(b'{"task_id":"task-2","exit_code":0}\n')
    resources = write_manifest(repository, baseline, [{"path": selected, "outcome": "committed"}])
    staged = git(repository, "ls-files", "--stage").stdout
    wrapper = repository / ".git/test-hooks/pre-commit"
    wrapper.parent.mkdir()
    wrapper.write_bytes((
        "#!/bin/sh\n"
        f"printf '%s\\n' '{{\"task_id\":\"task-2\",\"exit_code\":2}}' > '{selected}'\n"
        "exec .githooks/pre-commit\n"
    ).encode("utf-8"))
    configured = git(repository, "config", "core.hooksPath", ".git/test-hooks")
    assert configured.returncode == 0, configured.stderr

    rejected = commit(repository, selected, *CONTROL_NAMES, manifest=resources)

    report = preflight_result(rejected)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert report["errors"] == [f"Index content differs from local content: {receipt}"]
    assert git(repository, "rev-parse", "HEAD").stdout == head
    assert git(repository, "ls-files", "--stage").stdout == staged


def test_interleaved_tasks_finish_with_normal_commit_and_project_audit(
    repository: Path, record_property: Callable[[str, object], None],
) -> None:
    for name, value in (("user.name", "Closeout test"), ("user.email", "closeout@example.invalid")):
        configured = git(repository, "config", name, value)
        assert configured.returncode == 0, configured.stderr
    (repository / ".gitignore").write_text(".scratch/\n.closeout/\n", encoding="utf-8")
    start = prepare_runner(repository)
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--verify-runs", "--scope", ".closeout", target_ref=ref)
    assert json.loads(baseline.read_text(encoding="utf-8"))["start_commit"] == start

    own = run_check(repository, "task-1", "pytest", "tests/test_first.py", "-q")
    other = run_check(repository, "task-2", "pytest", "tests/test_second.py", "-q")
    assert (own["exit_code"], other["exit_code"]) == (0, 0), (own, other)
    receipt = Path(own["result_path"])
    launch = repository / ".scratch/check-runs/launches" / f"{own['invocation_id']}.json"
    unrelated = Path(other["result_path"])
    old = unrelated.with_name("b-legacy.json")
    old.write_text('{"exit_code":0,"started_at":"2020-01-09T16:40:00+00:00"}', encoding="utf-8")
    evidence = [receipt, launch]
    resources = write_manifest(repository, baseline, [{"path": str(path), "outcome": "committed"} for path in evidence])
    added = git(repository, "add", "-f", "--", *(path.relative_to(repository).as_posix() for path in evidence))
    assert added.returncode == 0, added.stderr
    before_audit, before_project = cli(repository, "audit")
    assert before_audit.returncode == 0, before_project
    recorded = {item["path"]: item for item in before_project["records"]}
    assert recorded[str(unrelated)]["task_id"] == "task-2"
    assert recorded[str(old)]["ownership"] == "unresolved"
    prior = repository / ".scratch/prior-audit.json"
    prior.write_text(json.dumps(before_project), encoding="utf-8")

    delivered = commit(repository, manifest=resources)
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    proposed = preflight_result(delivered)
    assert proposed["ok"] is True, proposed
    assert proposed["final_verification_required"] is True, proposed
    assert [run["invocation_id"] for run in proposed["check_runs"]] == [own["invocation_id"]]
    assert proposed["check_runs"][0]["source_version"]["commit"] == start
    assert proposed["check_runs"][0]["status"] == "passed"
    assert {str(path) for path in (unrelated, old)}.isdisjoint(item["path"] for item in proposed["candidates"])
    target = git(repository, "rev-parse", ref).stdout.strip()
    assert target != start

    unrelated.write_bytes(unrelated.read_bytes() + b"\n")
    verified, final = cli(repository, "check", str(resources), "--commit", target)
    assert verified.returncode == 0, final
    assert final["check_runs"] == proposed["check_runs"]
    changed_audit, changed_project = cli(repository, "audit", "--previous", str(prior))
    assert changed_audit.returncode == 0, changed_project
    assert str(unrelated) in changed_project["changes"]["changed"]

    unrelated.unlink()
    rechecked, final_after_cleanup = cli(repository, "check", str(resources), "--commit", target)
    assert rechecked.returncode == 0, final_after_cleanup
    assert final_after_cleanup["check_runs"] == proposed["check_runs"]
    removed_audit, removed_project = cli(repository, "audit", "--previous", str(prior))
    assert removed_audit.returncode == 0, removed_project
    assert str(unrelated) in removed_project["changes"]["removed"]
    hook_output = delivered.stdout + delivered.stderr
    hook_command = next(line for line in hook_output.splitlines() if "scripts/check_closeout.py preflight" in line)
    record_property("closeout_stages", json.dumps({
        "snapshot": {"command": "snapshot --verify-runs --scope .closeout", "exit_code": 0, "start_commit": start},
        "runs": {"task-1": own["invocation_id"], "task-2": other["invocation_id"]},
        "preflight": {"command": hook_command, "ok": proposed["ok"], "errors": proposed["errors"],
                      "check_runs": proposed["check_runs"]},
        "commit": {"command": delivered.args, "exit_code": delivered.returncode, "target": target},
        "check_after_change": {"command": verified.args, "exit_code": verified.returncode, "errors": final["errors"]},
        "check_after_removal": {"command": rechecked.args, "exit_code": rechecked.returncode, "errors": final_after_cleanup["errors"]},
        "audit": {"command": before_audit.args, "exit_code": before_audit.returncode,
                  "foreign_owner": recorded[str(unrelated)]["task_id"], "legacy_ownership": recorded[str(old)]["ownership"]},
        "audit_change": {"command": changed_audit.args, "exit_code": changed_audit.returncode, "path": str(unrelated)},
        "audit_removal": {"command": removed_audit.args, "exit_code": removed_audit.returncode, "path": str(unrelated)},
    }))
