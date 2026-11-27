"""Public native qualification follows the independent required catalogue."""

from itertools import product
import json
from pathlib import Path

import pytest

from tests.acceptance import test_entry, test_formal_scenarios, test_input_and_failures, test_question_review


PROTOCOL_GROUPS = {"three_protocols": ("deepseek", "openai-chat", "google"),
    "deepseek_only": ("deepseek",), "deepseek_openai": ("deepseek", "openai-chat"),
    "openai_only": ("openai-chat",)}


def declared_combinations(function, default_case=None):
    dimensions = []
    for mark in getattr(function, "pytestmark", []):
        if mark.name != "parametrize":
            continue
        names = mark.args[0]
        names = names.split(",") if isinstance(names, str) else names
        dimensions.append([dict(zip(names, row if len(names) > 1 else (row,), strict=True))
                           for row in mark.args[1]])
    return {(values.get("case_id", default_case), values.get("protocol", "deepseek"))
            for rows in product(*dimensions) for values in [{key: value for row in rows for key, value in row.items()}]}


@pytest.mark.parametrize(("prefix", "count"), [("formal.", 168), ("ordinary.", 21)])
def test_public_native_parameters_cover_the_required_protocols(prefix, count):
    required = json.loads((Path(__file__).parent / "materials/catalogue/required.json").read_bytes())
    expected = {(case, protocol) for group, cases in required.items() for case in cases
                if case.startswith(prefix) for protocol in PROTOCOL_GROUPS[group]}
    if prefix == "formal.":
        actual = declared_combinations(test_formal_scenarios.test_selected_formal_scenario_traverses_real_main_service_and_sdk)
        plans = [json.loads((Path(__file__).parent / "plans" / name).read_bytes())
                 for name in ("deepseek-key-behavior.json", "gemini-key-compatibility.json")]
        assert sum(len(plan["cases"]) * len(plan["protocols"]) for plan in plans) == 43
    else:
        actual = set().union(*(declared_combinations(function, case) for function, case in (
            (test_entry.test_selected_question_records_real_sdk_exchange, "ordinary.question"),
            (test_entry.test_attachment_is_read_by_tool_and_reaches_next_sdk_input, "ordinary.attachment"),
            (test_question_review.test_encoded_long_answer_keeps_original_bytes_and_complete_response, "ordinary.encoded-long"),
            (test_input_and_failures.test_ordinary_failure_and_input_contracts, None),
            (test_input_and_failures.test_current_input_keeps_tool_contract_prefix_and_measures_entire_request, "ordinary.input-layout"))))
    assert len(expected) == count
    assert actual == expected, {"missing": sorted(expected - actual), "extra": sorted(actual - expected)}
