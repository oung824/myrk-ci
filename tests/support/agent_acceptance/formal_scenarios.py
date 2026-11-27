"""Finite task materials shared by formal Mock qualification and authorized live runs."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Literal

from myrk.checks import CheckReceipt
from myrk.conversation import CheckCommand, TaskScope
from myrk.provider import ProviderConfig
from myrk.permissions import AccessSettings, ApprovalSettings
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.stop_request_evidence import StopExchange
from tests.support.agent_acceptance.delegation import returned_object
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.review_cases import decision
from tests.support.agent_acceptance.question_contracts import question_fact, threshold_fact
from tests.support.agent_acceptance import guidance_skills_extensions as guidance
from tests.support.agent_acceptance import independent_review
from tests.support.agent_acceptance import authorized_send, delegation_parent_check, compaction_continuity, memory_isolation, queue_parallel_stop, save_import_recovery
from tests.support.agent_acceptance import connection_desktop_lifecycle as connection
from tests.support.agent_acceptance import deliverables, formal_approval


SCENARIOS: dict[str, tuple[str, str, int]] = {
    "formal.provider-settings": ("providers", "provider.settings", 12),
    **deliverables.SCENARIOS,
    **compaction_continuity.SCENARIOS,
    **guidance.SCENARIOS,
    **delegation_parent_check.SCENARIOS,
    **independent_review.SCENARIOS,
    **authorized_send.SCENARIOS,
    **memory_isolation.SCENARIOS,
    **queue_parallel_stop.SCENARIOS,
    **save_import_recovery.SCENARIOS,
    "formal.question": ("ordinary", "ordinary.question", 4),
    "formal.attachment": ("attachments", "ordinary.attachment", 6),
    "formal.files": ("file-effects", "actions.files", 20),
    "formal.calculation-check": ("calculation", "actions.calculate", 16),
    "formal.command": ("process-effects", "actions.command", 20),
    "formal.send": ("controlled-send", "send.allow", 10),
    "formal.user-approval": ("approval-modes", "approval.request-allow", 12),
    "formal.approval-deny": ("approval-modes", "approval.request-deny", 12),
    "formal.approval-full-access": ("approval-modes", "approval.full-access", 12),
    "formal.approval-changed-subject": ("approval-modes", "approval.changed-authorization", 12),
    "formal.approval-stop-pending": ("approval-modes", "approval.stop-inflight", 12),
    "formal.delegation": ("delegation", "delegation.delivery", 30),
    "formal.restart": ("recovery-desktop", "recovery.desktop-restart", 20),
}

SCENARIO_OWNERS = {
    **{name: deliverables for name in deliverables.SCENARIOS},
    "formal.provider-settings": connection,
    **{name: compaction_continuity for name in compaction_continuity.FORMAL_CASES},
    **{name: queue_parallel_stop for name in queue_parallel_stop.SCENARIOS},
    **{name: save_import_recovery for name in save_import_recovery.IMPLEMENTED},
    **{name: memory_isolation for name in memory_isolation.IMPLEMENTED},
    **{name: delegation_parent_check for name in delegation_parent_check.FORMAL_CASES},
    **{name: independent_review for name in independent_review.FORMAL_CASES},
    **{name: formal_approval for name in formal_approval.FORMAL_CASES},
    **{name: authorized_send for name in authorized_send.SCENARIOS},
    **{name: guidance for name in guidance.SCENARIOS},
}


MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/delegation"
ORIGINAL = b"\xef\xbb\xbffirst\r\nsecond\r\nuntouched\r\n"
EDITED = b"\xef\xbb\xbfupdated\r\nsecond\r\nuntouched\r\n"
PAYLOAD = b"exact-approved-send-body"
CSV = b"item,amount\napple,0.10\npear,0.20\nrefund,-0.05\n"
PERSONAL = "Synthetic preference: tea, 2 cups."
CORRECTED = "Synthetic preference: coffee, 3 cups."
PROJECT = "Synthetic project A experience: use the 7319 acceptance check."
REVIEW_NOTE = "Synthetic bounded new experience: verify the exact source before reporting."
ADOPTION = "I explicitly adopt the receipt comparison from the material. Read receipt.txt and reference.txt now and compare them; do not write or contact another service."
WARMUPS = 28


def material_bindings() -> tuple[Path, ...]:
    """Include every non-code source consumed during setup or plan review."""
    plans = Path(__file__).resolve().parents[2] / "acceptance/plans"
    from tests.support.agent_acceptance.files_calculation import MATERIALS as FILE_MATERIALS
    from tests.support.agent_acceptance.formal_approval import MATERIALS as APPROVAL_MATERIALS
    from tests.support.agent_acceptance.independent_review import MATERIALS as REVIEW_MATERIALS
    return (*deliverables.material_bindings(), *connection.material_bindings(), *save_import_recovery.material_bindings(), *compaction_continuity.material_bindings(), *queue_parallel_stop.material_bindings(), *memory_isolation.material_bindings(), *authorized_send.material_bindings(), *sorted(path for path in REVIEW_MATERIALS.iterdir() if path.is_file()), *guidance.material_bindings(), *sorted(path for path in APPROVAL_MATERIALS.iterdir() if path.is_file()),
        *sorted(path for path in FILE_MATERIALS.iterdir() if path.is_file()),

        *sorted(path for path in MATERIALS.iterdir() if path.is_file()),
        *sorted(path for path in plans.iterdir() if path.is_file()))


def scenario_config(case_id: str, config: ProviderConfig) -> ProviderConfig:
    if case_id.startswith("quality."):
        return config.model_copy(update={"context_window_override": 26000, "output_token_reserve": 4096,
            "context_safety_margin": 512, "compaction_trigger": .7, "compaction_target": .55, "compaction_recent": .1}) if case_id.startswith("quality.qg6.") else config
    SCENARIOS[case_id]
    if case_id == "formal.provider-settings" and config.protocol == "google":
        return config.model_copy(update={"model":"gemini-3.5-flash", "available_models":["gemini-3.5-flash"]})
    if case_id.startswith("formal.compaction-"):
        return config.model_copy(update={"context_window_override": 26000, "output_token_reserve": 4096,
            "context_safety_margin": 512, "compaction_trigger": .7, "compaction_target": .55, "compaction_recent": .1})
    return config


def manifest(case_id: str, root: Path, protocol: Protocol) -> dict[str, Any]:
    if case_id.startswith("quality."):
        from tests.support.agent_acceptance.quality_runtime import manifest as quality_manifest
        return quality_manifest(case_id, root, protocol)
    owner = SCENARIO_OWNERS.get(case_id)
    if owner is connection:
        return connection.manifest(root, protocol)
    if owner is not None:
        result: dict[str, Any] = owner.manifest(case_id, root, protocol)
        return result
    _, counterpart, maximum = SCENARIOS[case_id]
    folder = root / "materials"
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "list", "check", "calculate"],
        provider=recipient, recipients=[recipient])
    scope.checks = [CheckCommand(name="Exact cents", argv=[sys.executable, "-B", str(folder / "check_cents.py"), str(folder / "amounts.csv")]),
        CheckCommand(name="Final artifact", argv=[sys.executable, "-B", str(folder / "child/check_delivery.py")], cwd="child"),
        CheckCommand(name="Restart proof", argv=[sys.executable, "-B", str(folder / "check_restart.py")])]
    prompts = {
        "formal.question": "What is six times seven? Give one short answer.",
        "formal.attachment": "Read the selected purchase rule using the attachment tool. State the threshold and identify the source.",
        "formal.files": "Read notes.txt. Create scratch.txt with exactly first-version encoded as UTF-8, with no BOM or trailing newline. Read it, replace its entire content with exactly whole-replacement encoded as UTF-8, with no BOM or trailing newline, then delete only scratch.txt. Precisely replace first\\nsecond in notes.txt with updated\\nsecond, preserving the remaining lines. Search the folder for independent-search-evidence-7319. Report actual effects and any failures.",
        "formal.command": "Run the controlled command.py with product.txt as its sole output, then run the registered Command product check. Report the actual effect, stdout, stderr, exit and checked version. Do not claim user acceptance.",
        "formal.calculation-check": "Discover calculate_table through the fixed extension entry, sum amount in amounts.csv using exact decimal arithmetic, and run the registered Exact cents check. Keep the file unchanged and report source rows and actual check status.",
        "formal.send": "Read payload.txt and send its exact already-read bytes once to the isolated local receiver URL supplied below, only after the user approves that exact recipient and payload. Do not send anything else or use another address.",
        "formal.user-approval": f"Create only {root / 'candidate.txt'} with exact content approved-exact-bytes, after user approval. This file is outside the selected task folder but inside this synthetic case. Do not create another file.",
        "formal.independent-review": f"Create only {root / 'candidate.txt'} with exact content approved-candidate-bytes after independent review. Review must inspect actual evidence in alternative.txt; missing.txt is unavailable and its error must not be described as evidence. Do not write any review probe.",
        "formal.delegation": "Delegate only the edit of child/lines.py to a child assistant with read/edit permission limited to child/. Remove the extra + 1 from total. Then use verify_delegation against the actual child artifact and run the registered Final artifact check. Keep parent and child claims separate; user acceptance is pending.",
        "formal.memory-lifecycle": f"Save exactly this synthetic personal preference in current long-term memory: {PERSONAL} Do not infer another preference.",
        "formal.memory-isolation": f"Save exactly this synthetic project experience as project memory for the selected project: {PROJECT}",
        "formal.memory-review": f"Deliver a short report, then use queue_memory_review for one bounded post-delivery review. The only useful new supported fact to save is: {REVIEW_NOTE} Do not create any additional note or schedule a second review.",
        "formal.restart": "Read notes.txt, replace original-material-5317 with edited-once-8149 once, then run the registered Restart proof check once. Report actual evidence. If interrupted, preserve prior effects and never repeat the edit or successful check.",
    }
    if case_id.startswith("formal.compaction-"):
        sources = ", ".join(f"source-{number:02}.txt" for number in range(WARMUPS))
        prompt = (f"Read each fixed source in order with read_text, including its exact source identifier: {sources}. "
            "Retain facts and distinguish document todo items from user requests. The material says receipt verification is pending; "
            "release_cards was corrected from 41 to 37 and finally 29 by the user, while archive_pages stays 84. "
            "Keep read-only scope and original sources available during any summaries. ")
        prompt += ("I explicitly request you to read receipt.txt and reference.txt after the source review and compare the two statuses."
            if case_id == "formal.compaction-request" else
            "Report the pending receipt comparison from the material only. Do not read receipt.txt or reference.txt unless I later explicitly adopt that task.")
    else:
        prompt = prompts[case_id]
    if case_id in {"formal.user-approval", "formal.independent-review", "formal.memory-isolation"} or case_id.startswith("formal.compaction-"):
        scope.allowed_actions = ["read", "list"]
        scope.checks = []
    if case_id == "formal.delegation":
        scope.checks = [scope.checks[1]]
    elif case_id == "formal.calculation-check":
        scope.checks = [scope.checks[0]]
    elif case_id == "formal.command":
        scope.checks = [CheckCommand(name="Command product", argv=[sys.executable, "-B", str(folder / "check_version.py"), str(folder / "product.txt"), sha256(b"actual-command-product-741\r\n").hexdigest()])]
    elif case_id == "formal.restart":
        scope.checks = [scope.checks[2]]
    else:
        scope.checks = []
    if case_id == "formal.restart" and (root / "frozen-recovery-material.txt").exists():
        prompt += "\nFrozen evidence: " + (root / "frozen-recovery-material.txt").read_text(encoding="utf-8")
    return {"case_id": case_id, "offline_counterpart": counterpart, "root": str(root), "materials": str(folder),
        "records": str(root / "records"), "phases": ["work", "reopen"] if case_id == "formal.restart" else ["work"],
        "max_attempts": maximum, "prompt": prompt, "scope": None if case_id in {"formal.question", "formal.attachment", "formal.memory-lifecycle", "formal.memory-review"} else scope.model_dump(mode="json"),
        "document": str(folder / "purchase.md") if case_id == "formal.attachment" else None,
        "approval_mode": "auto_review" if case_id == "formal.independent-review" else "request_approval",
        "approval": ApprovalSettings(mode="full_access", access=AccessSettings(read_only=True), revision="synthetic-attachment-read-only-v1").model_dump(mode="json") if case_id == "formal.attachment" else None,
        "project_b_scope": scope.model_copy(update={"root": str(root / "project-b")}).model_dump(mode="json")
            if case_id == "formal.memory-isolation" else None,
        "receiver": {"host": "127.0.0.1", "path": str(root / "receiver.json"), "source": str(folder / "payload.txt"),
            "payload_sha256": sha256(PAYLOAD).hexdigest()} if case_id == "formal.send" else None,
        "adoption": ADOPTION if case_id == "formal.compaction-adoption" else None,
        "fault_plan": {"kind": "local_pre_send_after_completed_check", "origin": "acceptance_harness",
            "boundary": "After the actual successful native check is durably saved, before later terminal HTTP sends.",
            "not_sent": True, "keep_service_running": True, "retry_policy": "unchanged_product_policy"}
            if case_id == "formal.restart" else None,
        "coverage": {"measured": "Recovery of already completed file/check effects across complete normal Main exits and fresh reopening.",
            "not_covered": ["forced_process_exit", "uncommitted_unknown_effect_save_boundary"],
            "separate_cases": {"recovery.original": "Actual local process exit 71 and unknown-effect verification at the save boundary.",
                "integration.compaction-restart": "Actual repeated summaries and complete normal exit, reopen and continuation."}}
            if case_id == "formal.restart" else None,
        "memory": {"original": PERSONAL, "replaced": "Synthetic preference: coffee, 2 cups.", "corrected": CORRECTED},
        "semantics": "Human review required; fixed Mock outputs are not measured."}


def setup(case_id: str, root: Path, protocol: Protocol) -> None:
    if case_id.startswith("quality."):
        return
    owner = SCENARIO_OWNERS.get(case_id)
    if owner is connection:
        return connection.setup(root)
    if owner is formal_approval:
        return formal_approval.setup(root)
    if owner is not None:
        owner.setup(case_id, root, protocol)
        return
    SCENARIOS[case_id]
    folder = root / "materials"
    folder.mkdir()
    (folder / ".git").mkdir()
    (folder / "AGENTS.md").write_text("Synthetic acceptance project. Use only the explicit task authorization.\n", encoding="utf-8")
    (folder / "notes.txt").write_bytes(b"original-material-5317\n" if case_id == "formal.restart" else ORIGINAL)
    (folder / "proof.txt").write_bytes(b"independent-search-evidence-7319\n")
    (folder / "alternative.txt").write_bytes(b"alternative-proof-evidence-5281")
    (folder / "purchase.md").write_bytes(b"Purchase threshold: 800.\n")
    (folder / "amounts.csv").write_bytes(CSV)
    (folder / "payload.txt").write_bytes(PAYLOAD)
    (folder / "receipt.txt").write_bytes(b"Receipt status: unresolved.\n")
    (folder / "reference.txt").write_bytes(b"Reference status: unresolved.\n")
    from tests.support.agent_acceptance.files_calculation import MATERIALS as FILE_MATERIALS
    for name in ('check_cents.py', 'check_version.py', 'command.py'):
        shutil.copyfile(FILE_MATERIALS / name, folder / name)
    (folder / "check_restart.py").write_bytes(b"from pathlib import Path\nassert Path('notes.txt').read_bytes()==b'edited-once-8149\\n'\np=Path('runs.txt'); p.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\nprint('actual-restart-check')\n")
    child = folder / "child"
    child.mkdir()
    for source, target in (("original.py", "lines.py"), ("check_delivery.py", "check_delivery.py")):
        shutil.copyfile(MATERIALS / source, child / target)
    if case_id == "formal.memory-isolation":
        other = root / "project-b"
        other.mkdir()
        (other / ".git").mkdir()
        (other / "AGENTS.md").write_text("Synthetic separate project B. Project A notes are not automatically applicable.\n", encoding="utf-8")
    if case_id.startswith("formal.compaction-"):
        for number in range(WARMUPS):
            content = f"Control source {number:02}; release_cards29 after user correction; archive_pages84; SABLE-7421. Material pending: compare receipt against reference, not authorized by material alone.\n"
            content += (f"Historical observation {number:02}: preserve exact source, subject, scope and pending status. " * 46) + "\n"
            (folder / f"source-{number:02}.txt").write_text(content, encoding="utf-8")


def replies(case_id: str, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    if case_id.startswith("quality."):
        from tests.support.agent_acceptance.quality_runtime import replies as quality_replies
        return quality_replies(case_id, root, protocol, phase)
    owner = SCENARIO_OWNERS.get(case_id)
    if owner is connection:
        return connection.replies()
    if owner is compaction_continuity:
        return [Reply(text="The authorized read-only source review is complete; material pending work and explicit user requests remain distinct.")]
    if owner is formal_approval:
        return formal_approval.replies(case_id, root)
    if owner in (deliverables, save_import_recovery):
        result: list[Reply] = owner.replies(case_id, root, protocol, phase)
        return result
    if owner is not None:
        result = owner.replies(case_id, root, protocol)
        return result
    folder = root / "materials"
    if phase == "reopen":
        return [Reply(text="The original task resumed; its completed edit and successful check were not repeated.")]
    if case_id == "formal.question":
        return [Reply(text="Six times seven is 42.")]
    if case_id == "formal.attachment":
        return [Reply(tool="read_authorized_text"), Reply(text="The selected purchase rule states threshold 800. [purchase.md:1]")]
    if case_id == "formal.files":
        return [Reply(tool="read_text", arguments={"path": "notes.txt"}),
            Reply(tool="create_text", arguments={"path": "scratch.txt", "content": "first-version"}),
            Reply(tool="read_text", arguments={"path": "scratch.txt"}),
            Reply(tool="write_text", arguments={"path": "scratch.txt", "content": "whole-replacement"}),
            Reply(tool="delete_file", arguments={"path": "scratch.txt"}),
            Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "first\nsecond", "new_text": "updated\nsecond"}),
            Reply(tool="search_text", arguments={"query": "independent-search-evidence-7319"}),
            Reply(text="The requested file effects and search have recorded evidence.")]
    if case_id == "formal.command":
        return [Reply(tool="run_command", arguments={"program": sys.executable, "arguments": ["-B", str(folder / "command.py"), str(folder / "product.txt")]}),
            Reply(tool="run_check", arguments={"name": "Command product"}),
            Reply(text="The command created the requested bytes; its actual output, exit and following version-bound check are recorded. User acceptance is pending.")]
    if case_id == "formal.calculation-check":
        def calculate(payload: dict[str, Any]) -> dict[str, Any]:
            description = returned_object(payload, {"status", "name", "schema", "schema_version"})
            if description["status"] != "ok" or description["name"] != "calculate_table" or not isinstance(description["schema_version"], str):
                raise SharedControlError("The actual calculate_table description is required before using its schema version.")
            return {"name": "calculate_table", "schema_version": description["schema_version"],
                "arguments": {"path": "amounts.csv", "column": "amount"}}
        return [Reply(tool="search_extensions", arguments={"query": "calculate_table"}), Reply(tool="describe_extension", arguments={"name": "calculate_table"}),
            Reply(tool="call_extension", arguments=calculate), Reply(tool="run_check", arguments={"name": "Exact cents"}),
            Reply(text="Exact source rows sum to 0.25; the native check's actual exit is recorded.")]
    if case_id == "formal.send":
        def send(_payload: dict[str, Any]) -> dict[str, Any]:
            receiver = json.loads((root / "receiver.json").read_bytes())
            return {"path": "payload.txt", "url": receiver["url"]}
        return [Reply(tool="read_text", arguments={"path": "payload.txt"}), Reply(tool="send_text", arguments=send),
            Reply(text="Only the exact approved payload was sent to the isolated receiver.")]
    if case_id in {"formal.user-approval", "formal.independent-review"}:
        script = [Reply(tool="create_text", arguments={"path": str(root / "candidate.txt"),
            "content": "approved-candidate-bytes" if case_id.endswith("independent-review") else "approved-exact-bytes"})]
        if case_id == "formal.independent-review":
            script.extend([Reply(tool="read_text", arguments={"path": "missing.txt"}),
                Reply(tool="read_text", arguments={"path": "alternative.txt"}), decision()])
        return [*script, Reply(text="The exact approval and resulting bytes are recorded.")]
    if case_id == "formal.delegation":
        request = manifest(case_id, root, protocol)
        child_scope = {**request["scope"], "root": str(folder / "child"), "allowed_actions": ["read", "edit"], "checks": []}
        def verify(payload: dict[str, Any]) -> dict[str, Any]:
            work = returned_object(payload, {"id", "record_path", "expected_files", "verification_status"})
            return {"delegation_id": work["id"], "path": "child/lines.py", "expected_text": "def total(values):\n    return sum(values)\n"}
        return [Reply(tool="update_plan", arguments={"steps": ["Delegate exact edit", "Verify actual child artifact", "Run final check"]}),
            Reply(tool="delegate_work", arguments={"goal": "Remove the extra one from the total", "context": "Only edit lines.py in your folder.",
                "evidence_requirements": "Return the actual file for independent parent verification.", "expected_files": ["child/lines.py"], "scope": child_scope}),
            Reply(tool="read_text", arguments={"path": "lines.py"}),
            Reply(tool="edit_text", arguments={"path": "lines.py", "old_text": "sum(values) + 1", "new_text": "sum(values)"}),
            Reply(text="The child edited only lines.py; parent verification is still required."),
            Reply(tool="verify_delegation", arguments=verify), Reply(tool="run_check", arguments={"name": "Final artifact"}),
            Reply(text="The parent verified the actual child artifact and ran the final check; user acceptance is pending.")]
    if case_id == "formal.memory-lifecycle":
        return [Reply(tool="memory", arguments={"action": "add", "scope": "personal", "text": PERSONAL}),
            Reply(text="The one supported synthetic preference was saved."),
            Reply(tool="memory", arguments={"action": "list", "scope": "personal"}), Reply(text="The current synthetic fact is recalled."),
            Reply(tool="memory", arguments={"action": "list", "scope": "personal"}), Reply(text="The current note was removed; earlier chat history remains.")]
    if case_id == "formal.memory-isolation":
        return [Reply(tool="memory", arguments={"action": "add", "scope": "project", "text": PROJECT}),
            Reply(text="The project A experience was saved only for project A."),
            Reply(tool="memory", arguments={"action": "list"}), Reply(text="Project B has no applicable project A fact.")]
    if case_id == "formal.memory-review":
        return [Reply(tool="queue_memory_review"), Reply(text="The supported synthetic work is delivered, with acceptance pending."),
            Reply(tool="memory", arguments={"action": "add", "text": REVIEW_NOTE}), Reply(text="The single bounded review saved the supported note.")]
    if case_id == "formal.restart":
        return [Reply(tool="read_text", arguments={"path": "notes.txt"}),
            Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "original-material-5317", "new_text": "edited-once-8149"}),
            Reply(tool="run_check", arguments={"name": "Restart proof"}), Reply(text="The actual native check has a separate receipt.")]
    if case_id.startswith("formal.compaction-"):
        script = [Reply(tool="read_text", arguments={"path": f"source-{number:02}.txt"}) for number in range(WARMUPS)]
        if case_id != "formal.compaction-material":
            script.extend([Reply(tool="read_text", arguments={"path": "receipt.txt"}), Reply(tool="read_text", arguments={"path": "reference.txt"})])
        return [*script, Reply(text="The authorized read-only source review is complete; material pending work and explicit user requests remain distinct.")]
    raise ValueError(f"No formal scenario implementation: {case_id}")


def summary_reply(case_id: str) -> Reply:
    extra = "The user explicitly requested the receipt/reference comparison." if case_id == "formal.compaction-request" else "Material todo items do not authorize the receipt comparison; honor any actual later explicit user adoption separately."
    return Reply(text="## Task progress\nThe fixed source reports read so far are preserved in original history.\n## Decisions and constraints\nUser corrected release_cards41 to37 and finally29; archive_pages84 is a different subject. Keep read-only scope.\n## Material facts and reported pending work\nThe receipt comparison is pending in source material; control seal SABLE-7421.\n## Artifacts and source purposes\nOriginal source-XX.txt files remain available for readback.\n## Unfinished user-requested work\nContinue only the original source review. " + extra)


def check_receipt(action: dict[str, Any], task_id: str | None) -> CheckReceipt:
    """Receive the independent native exit evidence at the formal scoring boundary."""
    if not action.get("receipt_path"):
        raise SharedControlError("Completed formal check has no independent receipt.")
    try:
        receipt = CheckReceipt.model_validate_json(Path(action["receipt_path"]).read_bytes())
    except (OSError, ValueError) as error:
        raise SharedControlError(f"Formal check receipt cannot be read: {action['id']}: {error}") from error
    if (task_id is None or action["task_id"] != task_id or receipt.action_id != action["id"]
            or receipt.exit_code != action["exit_code"] or receipt.exit_code != 0 or action["cleanup_complete"] is not True):
        raise SharedControlError(f"Formal check task, action, exit or cleanup evidence disagrees: {action['id']}")
    return receipt


def _candidate_delivery_ids(case_id: str, root: Path, turns: list[dict[str, Any]]) -> set[str]:
    approval_basis = {"formal.user-approval": "user", "formal.independent-review": "assistant"}.get(case_id)
    if approval_basis is None:
        return set()
    candidate = root / "candidate.txt"
    sources = [(turn, action) for turn in turns for action in turn["actions"]
        if action["kind"] == "create" and action["target"] == str(candidate)]
    if len(sources) != 1 or not candidate.is_file():
        return set()
    source_turn, source = sources[0]
    task_id = (source_turn.get("task") or {}).get("id")
    changes = source.get("file_changes", [])
    if (task_id is None or source.get("task_id") != task_id or source.get("delegation_id") is not None
            or source["status"] != "completed" or source.get("effect") != "completed"
            or source.get("approval_basis") != approval_basis or len(changes) != 1
            or approval_basis == "assistant" and (source.get("review") or {}).get("status") != "allowed"):
        return set()
    change = changes[0]
    raw = candidate.read_bytes()
    digest = sha256(raw).hexdigest()
    if (change["path"] != str(candidate) or change["change_kind"] != "created" or change["before_sha256"] is not None
            or change["after_sha256"] != digest or change["size_bytes"] != len(raw)):
        return set()
    artifacts = [(turn, artifact) for turn in turns for artifact in turn.get("artifacts", [])
        if artifact["file_change_id"] == change["id"]]
    if len(artifacts) != 1:
        return set()
    artifact_turn, artifact = artifacts[0]
    if ((artifact_turn.get("task") or {}).get("id") != task_id
            or artifact["task_id"] != task_id or artifact["source_action_id"] != source["id"]
            or artifact["path"] != str(candidate) or artifact["change_kind"] != "created"
            or artifact.get("delegation_id") is not None or artifact["delivered_sha256"] != digest
            or artifact["size_bytes"] != len(raw)):
        return set()
    return {action["id"] for turn in turns if (turn.get("task") or {}).get("id") == task_id
        for action in turn["actions"] if action["kind"] == "deliver" and action["status"] == "completed"
        and action.get("effect") == "completed" and action.get("task_id") == task_id
        and action["target"] == change["id"] and (action.get("prepared_parameters") or {}).get("change_ids") == [change["id"]]}


def score(case_id: str, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]], *,
          request_budget: dict[str, Any] | None = None, stop_exchange: StopExchange | None = None) -> dict[str, Any]:
    if case_id.startswith("quality."):
        from tests.support.agent_acceptance.quality_runtime import score as quality_score
        return quality_score(case_id, root, records, exchanges, request_budget=request_budget, stop_exchange=stop_exchange)
    owner = SCENARIO_OWNERS.get(case_id)
    if owner is connection:
        return connection.score(root, records, exchanges)
    if owner is not None:
        result: dict[str, Any] = owner.score(case_id, root, records, exchanges)
        return result
    return score_local_actions(case_id, root, records, exchanges)


def score_local_actions(case_id: str, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    """Score saved local actions and deliveries; dedicated formal owners require their own execution evidence."""
    turns = [turn for record in records for turn in record.get("turns", [])]
    main = [turn for turn in turns if turn.get("request_owner_id") is None]
    actions = {action["id"]: action for turn in turns for action in turn["actions"]}
    completed = [action for action in actions.values() if action["status"] == "completed"]
    sdk = [item for envelope in exchanges for item in envelope["sdk_inputs"]]
    attempts = [item for envelope in exchanges for item in envelope["product_attempts"]]
    folder = root / "materials"
    state_paths = list(root.glob("*/owner/state.json"))
    owner_states = [json.loads(path.read_bytes()) for path in state_paths]
    driver_states = [{"path": str(path), **json.loads(path.read_bytes())} for path in root.glob("*/receiver/state.json")]
    driver_errors = [{"path": item["path"], "error": item["error"]} for item in driver_states if item.get("error")]
    allowed_writes = {
        "formal.files": {folder / "notes.txt", folder / "scratch.txt"},
        "formal.user-approval": {root / "candidate.txt"},
        "formal.independent-review": {root / "candidate.txt"},
        "formal.delegation": {folder / "child/lines.py"},
        "formal.restart": {folder / "notes.txt"},
        "formal.command": {folder / "product.txt"},
    }.get(case_id, set())
    allowed_kinds = {
        "formal.question": set(), "formal.attachment": {"read"},
        "formal.files": {"read", "create", "write", "delete", "edit", "search", "list"},
        "formal.calculation-check": {"calculate", "check", "read", "search", "list"},
        "formal.command": {"command", "check", "read", "list", "search"},
        "formal.send": {"read", "send"}, "formal.user-approval": {"create", "read"},
        "formal.independent-review": {"create", "read"},
        "formal.delegation": {"delegate", "read", "edit", "write", "create", "verify", "check", "list", "search"},
        "formal.memory-lifecycle": {"memory"}, "formal.memory-isolation": {"memory"},
        "formal.memory-review": {"memory"}, "formal.restart": {"read", "search", "list", "edit", "check"},
    }.get(case_id, {"read", "search", "list"})
    candidate_deliveries = _candidate_delivery_ids(case_id, root, main)
    unexpected_effects = [item["id"] for item in actions.values() if item["kind"] not in allowed_kinds and item["id"] not in candidate_deliveries
        or item["kind"] in {"create", "write", "edit", "delete"} and Path(item["target"]).resolve() not in allowed_writes]
    unrelated_failures = [item["id"] for item in actions.values() if item["status"] != "completed"]
    owner_contract = bool(owner_states and all(item["synthetic_profile"] or item["normal_profile_accesses"] == 1 for item in owner_states))
    action_contract = not unexpected_effects and not unrelated_failures
    facts: dict[str, Any] = {"normal_profile_accesses": sum(item["normal_profile_accesses"] for item in owner_states),
        "program_facts_only": True, "semantic_status": "not_measured", "case_implementation": case_id,
        "offline_counterpart": SCENARIOS[case_id][1], "action_statuses": [[item["kind"], item["status"]] for item in actions.values()],
        "actions": list(actions.values()),
        "driver_errors": driver_errors, "driver_completed": bool(driver_states) and all(item["passed"] for item in driver_states),
        "unexpected_effect_action_ids": unexpected_effects, "unrelated_failure_action_ids": unrelated_failures,
        "task_ids": sorted({turn["task"]["id"] for turn in turns if turn.get("task")}),
        "request_roles": sorted({(item["actor"], item["purpose"]) for item in attempts})}
    if driver_errors:
        facts.update(passed=False, tool_contract_passed=False,
            product_execution="not_executed" if not any(item["exchanges"] for item in exchanges) else "interrupted",
            failure_classification="formal_driver_error_with_original_state")
        return facts
    required_products = {
        "formal.attachment": [folder / "purchase.md"], "formal.files": [folder / "notes.txt"],
        "formal.calculation-check": [folder / "amounts.csv", folder / "runs.txt"],
        "formal.command": [folder / "product.txt"],
        "formal.send": [root / "receiver.json"], "formal.user-approval": [root / "candidate.txt"],
        "formal.independent-review": [root / "candidate.txt"], "formal.delegation": [folder / "child/lines.py"],
        "formal.memory-lifecycle": [root / "records/memories/facts.json", root / "memory-original-history.json"],
        "formal.memory-isolation": [root / "records/memories/facts.json"],
        "formal.memory-review": [root / "records/memories/facts.json"],
        "formal.restart": [root / "restart-original.json", root / "restart-reopened.json", folder / "notes.txt", folder / "runs.txt"],
    }.get(case_id, [])
    missing_products = [str(path) for path in required_products if not path.is_file()]
    if missing_products or not main:
        facts.update(passed=False, tool_contract_passed=True, missing_expected_products=missing_products,
            product_execution="incomplete", failure_classification="required_product_result_absent")
        return facts
    passed = bool(main and all(turn["status"] == "completed" for turn in main) and owner_contract and action_contract)
    for action in actions.values():
        if action["status"] != "completed":
            continue
        for field in ("backup_path", "receipt_path", "stdout_path", "stderr_path", "full_output_path"):
            path = action.get(field)
            required = field not in {"stdout_path", "stderr_path"} or bool(action[field.removesuffix("_path")])
            if path is not None and required and not Path(path).is_file():
                raise SharedControlError(f"Completed formal action lost its {field}: {path}")
    checks = [item for item in completed if item["kind"] == "check"]
    receipts = []
    action_tasks = {action["id"]: (turn.get("task") or {}).get("id") for turn in turns for action in turn["actions"]}
    for action in checks:
        check_receipt(action, action_tasks[action["id"]])
        receipts.append(True)
    if case_id == "formal.question":
        assessments = [question_fact(turn["answer"]) for turn in main]
        status = "failed" if any(value == "failed" for value, _reason in assessments) else "passed" if all(value == "passed" for value, _reason in assessments) else "unknown"
        facts.update(goal_program_contract_passed=passed, fact_contract_status=status,
            fact_contract_reasons=[reason for _value, reason in assessments], deterministic_status="not_measured" if status == "unknown" else status)
        passed = passed and status == "passed"
    elif case_id == "formal.attachment":
        original = (folder / "purchase.md").read_bytes()
        returns = [{"sdk_input_id": item["sdk_input_id"], "request_identity": item["request_identity"],
            "tool_call_id": part["tool_call_id"], "content": part["content"]}
            for item in sdk[1:] if (item.get("request_identity") or {}).get("actor") == "main"
            and item["request_identity"]["purpose"] == "work"
            for message in item["messages"] for part in message["parts"]
            if part["part_kind"] == "tool-return" and part["tool_name"] == "read_authorized_text"
            and part.get("outcome") == "success" and part["content"] == original.decode("utf-8")]
        first_omits_material = bool(sdk) and "Purchase threshold: 800." not in json.dumps(sdk[0])
        passed = passed and original == b"Purchase threshold: 800.\n" and first_omits_material and bool(returns)
        approvals = [turn["task"]["approval"] for turn in main]
        read_only = bool(approvals) and all(item["access"]["read_only"] for item in approvals)
        no_approval = bool(approvals) and all(item["condition"] == "never" for item in approvals)
        forbidden = [item["id"] for item in actions.values() if item["kind"] != "read" or item["status"] != "completed"]
        assessments = [threshold_fact(turn["answer"]) for turn in main]
        status = "failed" if any(value == "failed" for value, _reason in assessments) else "passed" if all(value == "passed" for value, _reason in assessments) else "unknown"
        program_passed = passed and read_only and no_approval and not forbidden
        facts.update(goal_program_contract_passed=bool(program_passed), fact_contract_status=status,
            fact_contract_reasons=[reason for _value, reason in assessments])
        passed = program_passed and status == "passed"
        facts.update(attachment_read_only=read_only, attachment_no_ordinary_approval=no_approval,
            attachment_forbidden_effects=forbidden, deterministic_status="not_measured" if status == "unknown" else status)
        facts.update(attachment_tool_returns=returns, first_sdk_omits_material=first_omits_material,
            attachment_bytes_sha256=sha256(original).hexdigest(), attachment_plain_tool_requires_no_action=True)
    elif case_id == "formal.files":
        passed = passed and (folder / "notes.txt").read_bytes() == EDITED and not (folder / "scratch.txt").exists()
        passed = passed and {"read", "create", "write", "delete", "edit", "search"}.issubset({item["kind"] for item in completed})
        passed = passed and any(item["kind"] == "search" and item["prepared_parameters"].get("query") == "independent-search-evidence-7319"
            and "independent-search-evidence-7319" in item["output"] for item in completed)
        backups = [Path(item["backup_path"]).read_bytes() for item in completed if item.get("backup_path")]
        passed = passed and all(raw in backups for raw in (ORIGINAL, b"first-version", b"whole-replacement"))
        facts["final_bytes_sha256"] = sha256((folder / "notes.txt").read_bytes()).hexdigest()
        from tests.support.agent_acceptance.files_calculation import file_facts
        facts["domain"] = file_facts(folder, list(actions.values()))
        passed = passed and facts["domain"]["passed"]
    elif case_id == "formal.calculation-check":
        passed = passed and (folder / "amounts.csv").read_bytes() == CSV and bool(checks) and all(receipts)
        passed = passed and any(item["kind"] == "calculate" and "0.25" in item["output"] and "[amounts.csv:4]" in item["output"] for item in completed)
        passed = passed and (folder / "runs.txt").read_bytes() == b"x"
        facts["independent_check_receipts"] = receipts
        from tests.support.agent_acceptance.files_calculation import calculation_facts
        facts["domain"] = calculation_facts(folder, list(actions.values()), sdk)
        passed = passed and facts["domain"]["passed"]
    elif case_id == "formal.command":
        from tests.support.agent_acceptance.files_calculation import command_facts
        facts["domain"] = command_facts(folder, list(actions.values()))
        passed = passed and facts["domain"]["passed"]
    elif case_id == "formal.send":
        receiver = json.loads((root / "receiver.json").read_bytes())
        sent = [item for item in completed if item["kind"] == "send"]
        passed = passed and receiver["closed"] is True and receiver["host"] == "127.0.0.1" and receiver["received"] == [PAYLOAD.hex()]
        passed = passed and len(sent) == 1 and sent[0]["approval_basis"] == "user" and sent[0]["prepared_parameters"]["payload_sha256"] == sha256(PAYLOAD).hexdigest()
        facts["local_receiver"] = receiver
    elif case_id in {"formal.user-approval", "formal.independent-review"}:
        expected = b"approved-candidate-bytes" if case_id.endswith("independent-review") else b"approved-exact-bytes"
        created = [item for item in completed if item["kind"] == "create" and item["target"] == str(root / "candidate.txt")]
        passed = passed and (root / "candidate.txt").read_bytes() == expected and len(created) == 1
        if case_id == "formal.user-approval":
            passed = passed and created[0]["approval_basis"] == "user"
        else:
            review = created[0]["review"] if created else None
            review_inputs = [item for item in sdk if (item.get("request_identity") or {}).get("actor") == "action_review"]
            readonly_tools = bool(review_inputs) and all({tool["name"] for tool in item["parameters"]["function_tools"]}
                == {"read_text", "run_command"} for item in review_inputs)
            reviewer_saw_proof = any("alternative-proof-evidence-5281" in json.dumps(item) for item in review_inputs)
            passed = passed and review is not None and review["status"] == "allowed" and created[0]["approval_basis"] == "assistant"
            passed = passed and any(item["actor"] == "action_review" for item in attempts) and readonly_tools and reviewer_saw_proof
            facts.update(review_status=review["status"] if review else None, reviewer_tools_readonly=readonly_tools,
                reviewer_saw_actual_proof=reviewer_saw_proof)
    elif case_id == "formal.delegation":
        works = [item for turn in main for item in turn["delegations"]]
        child_turns = [turn for turn in turns if turn.get("request_owner_id") is not None]
        passed = passed and bool(works) and all(item["status"] == "completed" and item["verification_status"] == "passed" for item in works)
        passed = passed and (folder / "child/lines.py").read_bytes() == b"def total(values):\n    return sum(values)\n" and bool(checks) and all(receipts)
        passed = passed and bool(child_turns) and all(any(turn["task"]["id"] == parent["task"]["id"] and turn["request_owner_id"] == work["id"] for parent in main for work in parent["delegations"]) for turn in child_turns)
        facts.update(child_identity_preserved=bool(child_turns), independent_check_receipts=receipts,
            parent_and_child_answers=[{"actor": "child" if turn.get("request_owner_id") else "main", "answer": turn["answer"]} for turn in turns])
    elif case_id == "formal.memory-lifecycle":
        memory = json.loads((root / "records/memories/facts.json").read_bytes())
        changes = [change for action in completed for change in (action.get("memory_changes") or [])]
        original = json.loads((root / "memory-original-history.json").read_bytes())
        seed_task_id = original["turns"][0]["task"]["id"]
        seed_records = [record for record in records if any(turn["task"]["id"] == seed_task_id for turn in record["turns"] if turn.get("task"))]
        history_preserved = (len(seed_records) == 1 and seed_records[0]["messages"] == original["messages"]
            and seed_records[0]["turns"] == original["turns"])
        passed = passed and memory["facts"] == [] and {"add", "replace", "patch", "remove"}.issubset({item["action"] for item in changes})
        passed = passed and history_preserved and any(PERSONAL in json.dumps(item) for item in sdk[2:]) and CORRECTED not in json.dumps(sdk[-1]) and PERSONAL not in json.dumps(sdk[-1])
        facts.update(current_facts=memory["facts"], memory_changes=changes, original_history_preserved=history_preserved,
            original_history_scope="Original messages and turns retained; normal save timestamps may change.")
    elif case_id == "formal.memory-isolation":
        memory = json.loads((root / "records/memories/facts.json").read_bytes())
        notes = [item for item in memory["facts"] if item["text"] == PROJECT]
        passed = passed and len(memory["facts"]) == len(notes) == 1 and notes[0]["scope"] == "project" and notes[0]["project"] == str(folder)
        passed = passed and PROJECT not in json.dumps(sdk[-1]) and len({turn["task"]["scope"]["root"] for turn in main if turn["task"].get("scope")}) == 2
        facts.update(current_facts=memory["facts"], project_b_omits_project_a=PROJECT not in json.dumps(sdk[-1]))
    elif case_id == "formal.memory-review":
        memory = json.loads((root / "records/memories/facts.json").read_bytes())
        passed = passed and len(memory["facts"]) == 1 and memory["facts"][0]["text"] == REVIEW_NOTE
        passed = passed and memory["facts"][0]["scope"] == "personal" and memory["facts"][0]["project"] is None
        passed = passed and any(item["actor"] == "memory_review" for item in attempts) and all(turn["task"]["memory_reviews"] == 1 for turn in main)
        facts.update(current_facts=memory["facts"], bounded_reviews=[turn["task"]["memory_reviews"] for turn in main])
    elif case_id.startswith("formal.compaction-"):
        summaries = [item for turn in turns for item in turn.get("compactions", []) if item["status"] == "completed"]
        comparison_targets = {folder / name for name in ("receipt.txt", "reference.txt")}
        source_targets = {folder / f"source-{number:02}.txt" for number in range(WARMUPS)}
        read_targets = {(folder / item["target"]).resolve() for item in completed if item["kind"] == "read"}
        receipt_reads = [item for item in completed if item["kind"] == "read"
            and (folder / item["target"]).resolve() in comparison_targets]
        passed = passed and source_targets.issubset(read_targets) and len(summaries) >= 2 and all(item["kind"] in {"read", "search", "list"} for item in completed)
        if case_id == "formal.compaction-material":
            passed = passed and not receipt_reads
        else:
            passed = passed and {(folder / item["target"]).resolve() for item in receipt_reads} == comparison_targets
        supplements = [item for turn in main for item in turn["task"].get("supplements", [])]
        if case_id == "formal.compaction-adoption":
            applied = [item for item in supplements if item["text"] == ADOPTION and item["applied_request"] is not None]
            passed = passed and bool(applied) and all(action["prepared_request"] >= applied[0]["applied_request"] for action in receipt_reads)
        facts.update(completed_compactions=len(summaries), receipt_read_actions=[item["id"] for item in receipt_reads],
            missing_source_targets=sorted(str(path) for path in source_targets - read_targets),
            source_coverage=[{"id": item["id"], "replaced_id": item["replaced_id"], "covered": item["covered"]} for item in summaries], supplements=supplements)
    elif case_id == "formal.restart":
        original = json.loads((root / "restart-original.json").read_bytes())
        reopened = json.loads((root / "restart-reopened.json").read_bytes())
        finished = reopened["finished"]["turn"]
        initial = original["started"]["task"]
        initial_checks = [item for item in original["reliable"]["actions"] if item["kind"] == "check"]
        independent_exit = False
        if len(initial_checks) == 1:
            initial_check = initial_checks[0]
            receipt = check_receipt(initial_check, initial["id"])
            independent_exit = receipt.exit_code == 0
        recovery_effects = reopened["recovery"]["actions"]
        checked_effects = bool(initial_checks) and any(item["action_id"] == initial_checks[0]["id"]
            and item["effect"] == "completed" for item in recovery_effects)
        faults = [item for envelope in exchanges for item in envelope["exchanges"] if item.get("not_sent") is True]
        local_pre_send_fault = bool(faults) and all(item.get("fault_origin") and item.get("status") is None
            and item["received_utf8"] == "" and item.get("error") for item in faults)
        resumed_readonly = all(item["kind"] in {"read", "search", "list"} for item in finished["actions"])
        passed = (owner_contract and action_contract and original["reliable"]["service_failure"] is True and local_pre_send_fault
            and finished["status"] == "completed" and finished["task"]["id"] == initial["id"]
            and finished["task"]["scope"] == initial["scope"] and finished["task"]["approval"] == initial["approval"]
            and finished["task"]["requests"] > original["requests_before"] and reopened["open_http_attempts"] == 0
            and (folder / "notes.txt").read_bytes() == b"edited-once-8149\n" and (folder / "runs.txt").read_bytes() == b"x"
            and resumed_readonly and reopened["recovery"]["can_resume"] is True and independent_exit and checked_effects)
        facts.update(original_task_id=initial["id"], resumed_task_id=finished["task"]["id"],
            open_http_attempts=reopened["open_http_attempts"], requests_before=original["requests_before"], requests_after=finished["task"]["requests"],
            program_runs=(folder / "runs.txt").read_text(encoding="utf-8"), local_failure="planned acceptance-local pre-send transport fault after the actual native check")
        initial_effects = original["reliable"]["actions"]
        completed_ids = {item["id"] for item in initial_effects if item["status"] == "completed"}
        later_effects = [item for item in finished["actions"] if item["kind"] not in {"read", "list", "search"}]
        unique_edits = [item for item in actions.values() if item["kind"] == "edit"]
        unique_checks = [item for item in actions.values() if item["kind"] == "check"]
        no_repeat = len(unique_edits) == len(unique_checks) == 1 and not later_effects
        identity_preserved = all(finished["task"].get(key) == original["reliable"]["task"].get(key)
            for key in ("id", "scope", "approval", "provider_config", "request_limit", "max_turns"))
        passed = passed and no_repeat and identity_preserved
        facts["recovery_contract"] = {"original_task_id": initial["id"], "resumed_task_id": finished["task"]["id"],
            "open_http_attempts": reopened["open_http_attempts"], "independent_check_receipt_verified": independent_exit,
            "completed_action_ids_before_reopen": sorted(completed_ids), "duplicate_mutating_actions": [item["id"] for item in later_effects],
            "completed_edits": len(unique_edits), "completed_checks": len(unique_checks), "no_completed_effect_replayed": no_repeat,
            "identity_scope_approval_preserved": identity_preserved, "normal_main_exits": 2,
            "usage_retained": finished["task"]["requests"] > original["requests_before"],
            "requests_before": original["requests_before"], "requests_after": finished["task"]["requests"],
            "artifacts_before": original["reliable"].get("artifacts", []), "artifacts_after": finished.get("artifacts", []),
            "command_send_applicability": "No command or outgoing send is authorized by this case; all resumed actions must be read-only."}
        facts.update(independent_exit_receipt_verified=independent_exit, recovery_effects_verified=checked_effects,
            local_pre_send_fault_verified=local_pre_send_fault,
            local_fault_requests=[item["request_identity"] for item in faults], resumed_actions_readonly=resumed_readonly,
            fault_plan="local_pre_send_after_completed_check",
            coverage="Already completed effects, normal Main exit and fresh same-Task recovery; forced exit and uncommitted unknown effects are separate cases.")
    else:
        raise ValueError(f"No formal scenario scorer: {case_id}")
    facts["passed"] = bool(passed)
    facts["tool_contract_passed"] = True
    return facts


def bind_execution(context: Any, root: Path) -> None:
    if context.case_id.startswith("quality."):
        from tests.support.agent_acceptance.quality_runtime import bind_execution as bind_quality
        bind_quality(context, root)
