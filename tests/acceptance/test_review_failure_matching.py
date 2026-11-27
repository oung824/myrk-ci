"""The observed review save failure remains a narrow, unresolved product failure."""

import pytest

from myrk.actions import Action
from myrk.conversation import Turn
from myrk.review import ReviewRecord
from myrk.task_control import RequestAttempt
from tests.support.agent_acceptance.actions import known_save_failure


def observed_review_record(tmp_path):
    path = tmp_path / "conversation.json"
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials/alternative.txt").write_bytes(b"alternative-proof-evidence-5281")
    error = f"2026-11-13T13:13:17.618101+00:00：进展未能保存；最后可靠保存位置：{path}；时间：2026-11-13T13:13:17.538928+00:00。"
    review = ReviewRecord(status="allowed", attempts=2, unmeasured_requests=1, request_attempts=[
        RequestAttempt(number=2, own_number=2, model="deepseek-flash", provider="https://api.deepseek.com", actor="action_review",
            status="failed", http_status=503, error="ModelUnavailableError（审核服务报告的错误种类）"),
        RequestAttempt(number=3, own_number=3, model="deepseek-flash", provider="https://api.deepseek.com", actor="action_review", status="completed")])
    target = tmp_path / "candidate.txt"
    action = Action(task_id="synthetic-review-task", kind="create", target=str(target), status="cancelled", effect="unknown",
        prepared_request=1, approval_basis="assistant", review=review,
        prepared_parameters={"path": str(target), "content": "approved-candidate-bytes", "create_parents": [], "creation_parent": str(tmp_path)})
    turn = Turn(prompt="Create the exact candidate after independent review.", answer="", status="limited", error=error, requests=3,
        actions=[action], request_attempts=[RequestAttempt(number=1, own_number=1, model="deepseek-flash", provider="https://api.deepseek.com", status="completed")])
    return path, error, turn


def test_exact_review_retry_save_failure_is_a_product_failure_with_unknown_cause(tmp_path):
    path, error, turn = observed_review_record(tmp_path)
    defect = known_save_failure("review.service-retry", turn, path=path, save_failures=(error,), protocol="deepseek")
    assert defect is not None
    assert defect["id"] == "conversation-progress-save-failure"
    assert defect["cause"] == "unknown"


@pytest.mark.parametrize("changed", ["actor", "number", "requests", "503", "action-state", "effect", "review-state", "path",
                                     "proof", "candidate", "disappeared", "other-error", "unrecorded", "protocol"])
def test_changed_review_signature_and_disappeared_symptom_do_not_match(tmp_path, changed):
    path, error, turn = observed_review_record(tmp_path)
    review = turn.actions[0].review
    assert review is not None
    if changed == "actor":
        review.request_attempts[0].actor = "main"
    elif changed == "number":
        review.request_attempts[0].number = 4
    elif changed == "requests":
        turn.requests = 4
    elif changed == "503":
        review.request_attempts[0].http_status = 500
    elif changed == "action-state":
        turn.actions[0].status = "failed"
    elif changed == "effect":
        turn.actions[0].effect = "not_started"
    elif changed == "review-state":
        review.status = "failed"
    elif changed == "path":
        path = tmp_path / "another-record.json"
    elif changed == "proof":
        (tmp_path / "materials/alternative.txt").write_bytes(b"different-proof")
    elif changed == "candidate":
        (tmp_path / "candidate.txt").write_bytes(b"approved-candidate-bytes")
    elif changed == "disappeared":
        turn.status, turn.error, turn.answer = "completed", None, "A new observation must be reviewed; it does not repair the historical failure."
    elif changed == "other-error":
        turn.error = "A different local failure."
    assert known_save_failure("review.service-retry", turn, path=path,
        save_failures=() if changed == "unrecorded" else (error,), protocol="openai-chat" if changed == "protocol" else "deepseek") is None
