"""Check that archived originals keep their exact bytes through Git closeout."""

import hashlib
import json
from pathlib import Path
import subprocess

from tests.test_closeout import cli, snapshot
from tests.test_closeout_hook import CONTROL_NAMES, commit, preflight_result, write_manifest
from tests.test_pre_commit import fixture_environment, git, repository


def write_run_evidence(repository: Path, task_id: str, *, unsealed_report: Path | None = None) -> tuple[Path, Path, Path, dict[str, object]]:
    invocation_id = "1" * 32
    directory = repository / ".scratch/check-runs"
    origin = directory / "launches" / f"{invocation_id}.json"
    receipt = directory / "results" / ("legacy.json" if unsealed_report is not None else "b-original.json")
    artifact = directory / "results/b-original-report.txt"
    origin.parent.mkdir(parents=True)
    receipt.parent.mkdir(exist_ok=True)
    artifact.write_bytes(b"Original report\r\n")
    receipt_data: dict[str, object] = {
        "invocation_id": invocation_id, "task_id": task_id, "project_root": str(repository),
        "source_root": str(repository), "result_path": str(receipt), "exit_code": 0,
        "artifact_inventory_complete": unsealed_report is None,
    }
    if unsealed_report is not None:
        unsealed_report.write_bytes(b"Reported without a producer fingerprint\r\n")
        receipt_data["reports"] = [str(unsealed_report)]
    receipt.write_bytes((json.dumps(receipt_data) + "\r\n").encode("utf-8"))
    receipt_path = str(receipt) if unsealed_report is None else receipt.relative_to(repository).as_posix()
    artifact_path = str(artifact) if unsealed_report is None else artifact.relative_to(repository).as_posix()
    launch: dict[str, object] = {
        "version": 1, "invocation_id": invocation_id, "task_id": task_id,
        "project_root": str(repository), "source_root": str(repository), "result_path": receipt_path,
        "mode": "mypy", "source_version": {"commit": git(repository, "rev-parse", "HEAD").stdout.strip(),
                                           "working_tree": "clean"},
        "coverage": {"arguments": ["tests"]}, "started_at": "2025-01-01T12:00:00+00:00",
        "terminal": {"status": "passed", "result_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                     "required_artifacts": {artifact_path: hashlib.sha256(artifact.read_bytes()).hexdigest()}},
    }
    origin.write_bytes((json.dumps(launch) + "\r\n").encode("utf-8"))
    return origin, receipt, artifact, launch


def enable_original_bytes(repository: Path, resources: Path, **settings: object) -> None:
    document = json.loads(resources.read_text(encoding="utf-8"))
    document["closeout"].update(verify_original_bytes=True, **settings)
    resources.write_bytes(json.dumps(document).encode("utf-8"))
    added = git(repository, "add", "-f", "--", str(resources))
    assert added.returncode == 0, added.stderr


def original_byte_errors(report: dict) -> list[str]:
    return [error for error in report["errors"] if "original bytes" in error.lower()]


def test_commit_only_rejects_normalization_of_an_explicit_original(repository: Path) -> None:
    name = ".closeout/historical-evidence.txt"
    original = repository / name
    payload = b"Historical evidence\r\n"
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--task-files", "--scope", ".closeout", target_ref=ref)
    original.write_bytes(payload)
    added = git(repository, "add", "-f", "--", name)
    assert added.returncode == 0, added.stderr
    declaration = {"path": name, "outcome": "committed", "preserve_bytes": True}
    resources = write_manifest(repository, baseline, [declaration])
    head = git(repository, "rev-parse", "HEAD").stdout

    rejected = commit(repository, name, *CONTROL_NAMES, manifest=resources)

    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    rejected_report = preflight_result(rejected)
    assert any(str(original) in error for error in original_byte_errors(rejected_report)), rejected_report
    assert git(repository, "rev-parse", "HEAD").stdout == head

    normalized = commit(repository, name, *CONTROL_NAMES)
    assert normalized.returncode == 0, normalized.stdout + normalized.stderr
    checked, report = cli(repository, "check", str(resources), "--commit", "HEAD")
    assert checked.returncode == 1, report
    assert any(str(original) in error for error in original_byte_errors(report)), report

    (repository / ".gitattributes").write_bytes(f"/{name} -text -diff\n".encode("utf-8"))
    added = git(repository, "add", "--", ".gitattributes")
    assert added.returncode == 0, added.stderr
    refreshed = git(repository, "add", "--renormalize", "--", name)
    assert refreshed.returncode == 0, refreshed.stderr
    resources = write_manifest(repository, baseline, [declaration, {"path": ".gitattributes", "outcome": "committed"}])

    delivered = commit(repository, name, ".gitattributes", *CONTROL_NAMES, manifest=resources)

    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert preflight_result(delivered)["ok"] is True
    blob = subprocess.run(
        ["git", "-C", str(repository), "show", f"HEAD:{name}"],
        capture_output=True, env=fixture_environment(), timeout=30, check=True,
    )
    assert blob.stdout == payload
    checked, report = cli(repository, "check", str(resources), "--commit", "HEAD")
    assert checked.returncode == 0, report


def test_linked_worktree_commit_checks_common_original_against_proposed_index(repository: Path) -> None:
    worktree = repository / ".scratch/worktrees/delivery"
    created = git(repository, "worktree", "add", "-b", "delivery", str(worktree))
    assert created.returncode == 0, created.stderr
    baseline = snapshot(worktree, "--task-files", "--scope", ".closeout", target_ref="refs/heads/delivery")

    name = "report.txt"
    original = repository / name
    copy = worktree / name
    payload = b"Original report\r\n"
    original.write_bytes(payload)
    copy.write_bytes(payload)
    (repository / ".gitattributes").write_bytes(b"/report.txt -text -diff\n")
    added = git(repository, "add", "--", name, ".gitattributes")
    assert added.returncode == 0, added.stderr
    raw = git(repository, "hash-object", "--no-filters", "--", str(original))
    assert raw.returncode == 0, raw.stderr
    assert git(repository, "rev-parse", ":" + name).stdout == raw.stdout
    added = git(worktree, "update-index", "--add", "--cacheinfo", f"100644,{raw.stdout.strip()},{name}")
    assert added.returncode == 0, added.stderr
    assert git(worktree, "rev-parse", ":" + name).stdout == raw.stdout

    declarations: list[dict[str, object]] = [
        {"path": str(copy), "outcome": "committed"},
        {"path": str(original), "outcome": "committed", "preserve_bytes": True},
    ]
    resources = write_manifest(worktree, baseline, declarations)
    head = git(worktree, "rev-parse", "HEAD").stdout

    rejected = commit(worktree, name, *CONTROL_NAMES, manifest=resources)

    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    report = preflight_result(rejected)
    assert any(str(original) in error for error in original_byte_errors(report)), report
    assert git(worktree, "rev-parse", "HEAD").stdout == head
    assert git(worktree, "rev-parse", ":" + name).stdout == raw.stdout
    assert git(repository, "rev-parse", ":" + name).stdout == raw.stdout

    unstaged = git(repository, "update-index", "--force-remove", "--", name)
    assert unstaged.returncode == 0, unstaged.stderr
    assert not git(repository, "ls-files", "--", name).stdout
    attributes = worktree / ".gitattributes"
    attributes.write_bytes((repository / attributes.name).read_bytes())
    added = git(worktree, "add", "--", attributes.name)
    assert added.returncode == 0, added.stderr
    refreshed = git(worktree, "add", "--renormalize", "--", name)
    assert refreshed.returncode == 0, refreshed.stderr
    assert git(worktree, "rev-parse", ":" + name).stdout == raw.stdout
    resources = write_manifest(worktree, baseline, [*declarations, {"path": str(attributes), "outcome": "committed"}])

    delivered = commit(worktree, name, attributes.name, *CONTROL_NAMES, manifest=resources)

    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert preflight_result(delivered)["ok"] is True
    assert git(worktree, "rev-parse", "HEAD:" + name).stdout == raw.stdout
    checked, report = cli(worktree, "check", str(resources), "--commit", "HEAD")
    assert checked.returncode == 0, report


def test_linked_worktree_delivery_preserves_an_original_already_in_target(repository: Path) -> None:
    worktree = repository / ".scratch/worktrees/delivery"
    created = git(repository, "worktree", "add", "-b", "delivery", str(worktree))
    assert created.returncode == 0, created.stderr
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    name = "archived-original.txt"
    original = repository / name
    original.write_bytes(b"Existing archived original\r\n")
    (repository / ".gitattributes").write_bytes(f"/{name} -text -diff\n".encode())
    added = git(repository, "add", "--", name, ".gitattributes")
    assert added.returncode == 0, added.stderr
    archived = commit(repository)
    assert archived.returncode == 0, archived.stdout + archived.stderr
    assert not git(worktree, "ls-files", "--", name).stdout

    baseline = snapshot(worktree, "--task-files", "--scope", ".closeout", "--scope", "delivery.txt", target_ref=ref)
    delivery = worktree / "delivery.txt"
    delivery.write_bytes(b"New delivery\n")
    added = git(worktree, "add", "--", delivery.name)
    assert added.returncode == 0, added.stderr
    declarations: list[dict[str, object]] = [
        {"path": str(delivery), "outcome": "committed"},
        {"path": str(original), "outcome": "committed", "preserve_bytes": True},
    ]
    resources = write_manifest(worktree, baseline, declarations)

    delivered = commit(worktree, delivery.name, *CONTROL_NAMES, manifest=resources)

    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert preflight_result(delivered)["ok"] is True
    assert not git(worktree, "ls-files", "--", name).stdout
    merged = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "merge", "--no-edit", "delivery")
    assert merged.returncode == 0, merged.stderr
    checked, report = cli(worktree, "check", str(resources), "--commit", ref)
    assert checked.returncode == 0, report

    synced = git(worktree, "merge", "--ff-only", ref)
    assert synced.returncode == 0, synced.stderr
    copy = worktree / name
    copy.unlink()
    resources = write_manifest(worktree, baseline, [
        *declarations, {"path": str(copy), "outcome": "committed", "deletion": True},
    ])
    rejected = commit(worktree, name, *CONTROL_NAMES, manifest=resources)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    report = preflight_result(rejected)
    assert f"Committed content is missing: {original}" in report["errors"], report


def test_linked_worktree_delivery_preserves_an_original_in_an_earlier_source_commit(repository: Path) -> None:
    worktree = repository / ".scratch/worktrees/delivery"
    created = git(repository, "worktree", "add", "-b", "delivery", str(worktree))
    assert created.returncode == 0, created.stderr
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(worktree, "--task-files", "--scope", ".closeout", "--scope", "delivery.txt", target_ref=ref)

    (repository / "target.txt").write_bytes(b"Unrelated target change\n")
    added = git(repository, "add", "--", "target.txt")
    assert added.returncode == 0, added.stderr
    advanced = commit(repository)
    assert advanced.returncode == 0, advanced.stdout + advanced.stderr

    name = "archived-original.txt"
    payload = b"Original archived on delivery branch\r\n"
    (worktree / name).write_bytes(payload)
    (worktree / ".gitattributes").write_bytes(f"/{name} -text -diff\n".encode())
    added = git(worktree, "add", "--", name, ".gitattributes")
    assert added.returncode == 0, added.stderr
    archived = commit(worktree)
    assert archived.returncode == 0, archived.stdout + archived.stderr

    original = repository / name
    original.write_bytes(payload)
    delivery = worktree / "delivery.txt"
    delivery.write_bytes(b"New delivery\n")
    added = git(worktree, "add", "--", delivery.name)
    assert added.returncode == 0, added.stderr
    resources = write_manifest(worktree, baseline, [
        {"path": str(delivery), "outcome": "committed"},
        {"path": str(original), "outcome": "committed", "preserve_bytes": True},
    ])

    delivered = commit(worktree, delivery.name, *CONTROL_NAMES, manifest=resources)

    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert preflight_result(delivered)["ok"] is True
    original.unlink()
    merged = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "merge", "--no-edit", "delivery")
    assert merged.returncode == 0, merged.stderr
    assert original.read_bytes() == payload
    checked, report = cli(worktree, "check", str(resources), "--commit", ref)
    assert checked.returncode == 0, report


def test_automatic_archival_requires_run_observation_baseline(repository: Path) -> None:
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--task-files", "--scope", ".closeout", target_ref=ref)
    resources = write_manifest(repository, baseline, [])
    enable_original_bytes(repository, resources)

    rejected, report = cli(repository, "preflight", str(resources))

    assert rejected.returncode == 1, report
    assert any("verify_original_bytes" in error and "snapshot --verify-runs" in error for error in report["errors"]), report


def test_automatic_archival_checks_only_verified_originals(repository: Path) -> None:
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--task-files", "--verify-runs", "--scope", ".closeout", target_ref=ref)
    unsealed = repository / ".scratch/check-runs/results/b-legacy-report.txt"
    origin, receipt, artifact, _ = write_run_evidence(repository, "task-1", unsealed_report=unsealed)
    evidence = (origin, receipt, artifact, unsealed)
    names = [path.relative_to(repository).as_posix() for path in evidence]
    added = git(repository, "add", "-f", "--", *names)
    assert added.returncode == 0, added.stderr
    resources = write_manifest(repository, baseline, [{"path": name, "outcome": "committed"} for name in names])

    compatible, report = cli(repository, "preflight", str(resources))
    assert compatible.returncode == 0, report
    enable_original_bytes(repository, resources)

    rejected = commit(repository, *names, *CONTROL_NAMES, manifest=resources)

    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    errors = original_byte_errors(preflight_result(rejected))
    assert len(errors) == 3, errors
    assert all(any(str(path) in error for error in errors) for path in (origin, receipt, artifact)), errors
    assert all(str(unsealed) not in error for error in errors), errors


def test_automatic_archival_checks_borrowed_originals_selected_for_commit(repository: Path) -> None:
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--task-files", "--verify-runs", "--scope", ".closeout", target_ref=ref)
    origin, receipt, artifact, launch = write_run_evidence(repository, "producer")
    reference = {
        "requirement": "the original check passed", "original_task_id": "producer",
        **{key: launch[key] for key in ("invocation_id", "mode", "source_root", "source_version", "coverage")},
        **{key: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
           for key, path in (("origin", origin), ("receipt", receipt))},
        "artifacts": [{"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    }
    resources = write_manifest(repository, baseline, [])
    enable_original_bytes(repository, resources, borrowed_checks=[reference])
    borrowing, report = cli(repository, "preflight", str(resources))
    assert borrowing.returncode == 0, report

    name = receipt.relative_to(repository).as_posix()
    resources = write_manifest(repository, baseline, [{"path": name, "outcome": "committed"}])
    enable_original_bytes(repository, resources, borrowed_checks=[reference])
    added = git(repository, "add", "-f", "--", name)
    assert added.returncode == 0, added.stderr
    rejected = commit(repository, name, *CONTROL_NAMES, manifest=resources)

    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    errors = original_byte_errors(preflight_result(rejected))
    assert len(errors) == 1 and str(receipt) in errors[0], errors
