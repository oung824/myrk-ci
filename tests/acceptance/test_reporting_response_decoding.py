"""General semantic reports use the same byte-bound response view."""

from base64 import b64encode
import gzip
from hashlib import sha256
import json

import pytest

from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome
from tests.support.agent_acceptance.reporting import write_semantic_review


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_general_report_keeps_original_response_and_exposes_decoded_body(tmp_path, encoding):
    request = '{"messages":[]}'
    text = '{"choices":[{"message":{"content":"原材料"},"finish_reason":"length"}]}'
    raw = gzip.compress(text.encode(), mtime=0) if encoding == "gzip" else text.encode()
    identity = {"number": 1, "actor": "main", "purpose": "compaction", "delegation_id": None}
    sdk = {"sdk_input_id": "report-sdk", "request_identity": identity, "messages": []}
    exchange = {"status": 200, "sdk_input_id": "report-sdk", "request_identity": identity,
        "request_utf8": request, "received_utf8": raw.decode("utf-8", errors="replace"),
        "received_base64": b64encode(raw).decode("ascii"), "response_metadata": {
            "byte_layer": "transport", "source": "fixed_fixture", "content_encoding": encoding,
            "content_type": "application/json"}}
    original = tmp_path / "exchange.json"
    original.write_text(json.dumps({"protocol": "deepseek", "sdk_inputs": [sdk], "exchanges": [exchange],
        "product_attempts": [{**identity, "observation": {"wire": {"body_sha256": sha256(request.encode()).hexdigest()}}}]}), encoding="utf-8")
    original_bytes = original.read_bytes()
    context = CaseContext(tmp_path, "deepseek", "local-report", "compaction.capture")
    outcome = CaseOutcome("failed", evidence={"exchange": original})

    async def unused_case(_context):
        raise AssertionError("Report generation must not execute a case.")

    case = AcceptanceCase("compaction.capture", "capture", ("deepseek",), unused_case, group="compaction")
    write_semantic_review(context, outcome, case)
    report = json.loads(outcome.evidence["semantic_review"].read_bytes())
    sample = report["samples"][0]
    assert sample["received_utf8"] == exchange["received_utf8"]
    assert sample["response_sha256"] == sha256(exchange["received_utf8"].encode()).hexdigest()
    assert sample["response_evidence"]["text"] == text
    assert sample["response_evidence"]["raw_sha256"] == sha256(raw).hexdigest()
    assert sample["response_evidence_sha256"] == sha256(json.dumps(sample["response_evidence"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert sample["kind"] == "summary"
    assert report["semantic_status"] == sample["semantic_status"] == "not_measured"
    assert set(sample["criteria_results"].values()) == {"unknown"}
    assert outcome.product_status == "failed"
    assert original.read_bytes() == original_bytes
