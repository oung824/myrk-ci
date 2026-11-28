"""Action outcome evidence and known save failure matching."""

from pathlib import Path
import re
from typing import Any

from myrk.conversation import Conversation, Task, Turn
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol
from tests.support.agent_acceptance.replay import AcceptanceSession


def known_save_failure(case_id: str, turn: Turn, *, path: Path, save_failures: tuple[str, ...],
                       protocol: Protocol | None = None) -> dict[str, Any] | None:
    """Match independently observed public shapes; never infer a shared cause or repair."""
    if turn.status != "limited" or turn.answer or turn.error not in save_failures:
        return None
    timestamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00"
    pattern = timestamp + "：进展未能保存；最后可靠保存位置：" + re.escape(str(path)) + "；时间：" + timestamp + "。"
    if turn.error is None or re.fullmatch(pattern, turn.error) is None:
        return None
    if case_id == "review.service-retry":
        if protocol != "deepseek" or turn.requests != 3 or turn.request_owner_id is not None or len(turn.actions) != 1:
            return None
        action = turn.actions[0]
        review = action.review
        target, proof = path.parent / "candidate.txt", path.parent / "materials/alternative.txt"
        if (action.kind != "create" or action.status != "cancelled" or action.effect != "unknown" or action.target != str(target)
            or action.delegation_id is not None or action.prepared_request != 1 or action.approval_basis != "assistant"
            or action.prepared_parameters != {"path": str(target), "content": "approved-candidate-bytes", "create_parents": [], "creation_parent": str(path.parent)}
            or review is None or review.status != "allowed" or review.attempts != 2 or review.unmeasured_requests != 1 or review.request_owner_id is not None
            or len(turn.request_attempts) != 1 or len(review.request_attempts) != 2):
            return None
        attempts = [*turn.request_attempts, *review.request_attempts]
        observed = [(item.number, item.own_number, item.actor, item.purpose, item.status, item.http_status, item.delegation_id) for item in attempts]
        if (observed != [(1, 1, "main", "work", "completed", None, None),
                        (2, 2, "action_review", "work", "failed", 503, None),
                        (3, 3, "action_review", "work", "completed", None, None)]
            or not attempts[1].error or attempts[0].error is not None or attempts[2].error is not None
            or target.exists() or not proof.is_file() or proof.read_bytes() != b"alternative-proof-evidence-5281"):
            return None
        return {"id": "conversation-progress-save-failure", "issue": ".scratch/acceptance-save-failure-diagnosis-20261113",
                "matched_public_symptom": True, "cause": "unknown"}
    shapes = {"actions.check": (0, []), "actions.files": (3, [("read", "completed"), ("create", "completed"), ("create", "cancelled")])}
    expected = shapes.get(case_id)
    if (expected is None or turn.requests != expected[0] or [(action.kind, action.status) for action in turn.actions] != expected[1]
        or [attempt.number for attempt in turn.request_attempts] != list(range(1, expected[0] + 1))
        or any(attempt.status != "completed" or attempt.actor != "main" or attempt.purpose != "work" for attempt in turn.request_attempts)):
        return None
    return {"id": "conversation-progress-save-failure", "issue": ".scratch/acceptance-save-failure-diagnosis-20261113",
            "matched_public_symptom": True, "cause": "unknown"}


def record_outcome(context: CaseContext, session: AcceptanceSession, conversation: Conversation,
                   task: Task, turn: Turn, passed: bool, *, facts: dict[str, Any] | None = None,
                   evidence: dict[str, Path] | None = None, children: list[Conversation] | None = None) -> CaseOutcome:
    """Keep original product attempts, including independently owned reviews."""
    saved_turns = [saved for account in [conversation, *(children or [])] for saved in account.turns
                   if saved.task and saved.task.id == task.id]
    attempts = {attempt.number: attempt for saved in saved_turns
                for attempt in [*saved.request_attempts, *(attempt for action in saved.actions if action.review
                                                          for attempt in action.review.request_attempts)]}
    exchange = session.save(context.root / "exchange.json",
        attempts=[attempt.model_dump(mode="json") for attempt in sorted(attempts.values(), key=lambda item: item.number)],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    originals = {}
    missing_originals = []
    actions = {action.id: action for saved in saved_turns for action in saved.actions}
    for action in actions.values():
        produced_streams = {"stdout": bool(action.stdout), "stderr": bool(action.stderr)}
        for name, value in (("backup", action.backup_path), ("receipt", action.receipt_path),
                            ("stdout", action.stdout_path), ("stderr", action.stderr_path), ("full_output", action.full_output_path)):
            if value is not None:
                if Path(value).is_file():
                    originals[f"{action.id}.{name}"] = Path(value)
                else:
                    missing_originals.append({"action_id": action.id, "field": name, "path": value,
                        "required": produced_streams.get(name, True) and (action.status == "completed"
                            or action.status == "failed" and action.exit_code is not None and not action.timed_out)})
    known_defect = known_save_failure(context.case_id, turn, path=conversation.path, save_failures=conversation.save_failures, protocol=context.protocol)
    passed = passed and not any(item["required"] for item in missing_originals) and known_defect is None
    return CaseOutcome("passed" if passed else "failed", {
        "turn_status": turn.status, "error": turn.error, "answer": turn.answer,
        "product_attempts": task.total_requests, "main_product_attempts": task.requests, "mock_http_attempts": len(session.exchanges),
        "statuses": [action.status for action in turn.actions],
        "actions": [action.model_dump(mode="json") for action in turn.actions],
        "missing_action_evidence": missing_originals,
        "known_defect": known_defect,
        "historical_defect_status": "unresolved" if context.case_id in {"actions.check", "actions.files"}
            or context.case_id == "review.service-retry" and context.protocol == "deepseek" else None,
        **(facts or {}),
    }, {"conversation": conversation.path, "exchange": exchange,
        **{f"child.{index}.conversation": child.path for index, child in enumerate(children or [])},
        **originals, **(evidence or {})})
