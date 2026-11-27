"""Known-defect classification keeps the observed public symptom narrow."""

from pathlib import Path

import pytest

from myrk.conversation import Turn
from tests.support.agent_acceptance.actions import known_save_failure


def test_exact_saved_progress_failure_is_unresolved_without_claiming_a_cause():
    path = Path("E:/synthetic/conversation.json")
    error = f"2026-11-13T11:15:11.224440+00:00：进展未能保存；最后可靠保存位置：{path}；时间：2026-11-13T11:15:10.957384+00:00。"
    turn = Turn(prompt="Run the exact check.", answer="", status="limited", error=error, requests=0)
    defect = known_save_failure("actions.check", turn, path=path, save_failures=(error,))
    assert defect == {"id": "conversation-progress-save-failure", "issue": ".scratch/acceptance-save-failure-diagnosis-20261113",
                      "matched_public_symptom": True, "cause": "unknown"}


@pytest.mark.parametrize("status,error,requests,recorded", [
    ("limited", "Assistant turn limit reached.", 0, True),
    ("limited", "Missing required receipt.", 0, True),
    ("failed", "Arbitrary transport failure.", 0, True),
    ("completed", None, 0, False),
    ("limited", "exact", 1, True),
    ("limited", "exact", 0, False),
])
def test_other_failures_and_disappearing_symptom_are_not_classified_as_the_known_defect(status, error, requests, recorded):
    path = Path("E:/synthetic/conversation.json")
    exact = f"2026-11-13T11:15:11.224440+00:00：进展未能保存；最后可靠保存位置：{path}；时间：2026-11-13T11:15:10.957384+00:00。"
    message = exact if error == "exact" else error
    turn = Turn(prompt="Run the exact check.", answer="", status=status, error=message, requests=requests)
    assert known_save_failure("actions.check", turn, path=path, save_failures=(message,) if recorded else ()) is None
