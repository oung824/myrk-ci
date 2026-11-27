"""Recorded responses remain reviewable without calling a model or HTTP client."""

from base64 import b64encode
import asyncio
import gzip
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
import httpx2

from myrk.conversation import ConversationRecord, Task, Turn
from myrk.request_observation import RequestObservation, WireRequestSummary
from myrk.task_control import RequestAttempt
from tests.support.agent_acceptance.compaction_review import CRITERIA, HumanReview, apply_reviews, write_review
from tests.support.agent_acceptance.core import CaseContext


def captured_report(tmp_path, raw, *, content_type="application/json", encoding="gzip", error=None, status=200):
    request = '{"messages":[]}'
    identity = {"number": 1, "actor": "main", "purpose": "compaction", "delegation_id": None}
    sdk = {"sdk_input_id": "captured-sdk", "request_identity": identity, "messages": []}
    exchange = {"attempt": 1, "status": status, "sdk_input_id": "captured-sdk", "request_identity": identity,
                "request_utf8": request, "received_utf8": raw.decode("utf-8", errors="replace"),
                "received_base64": b64encode(raw).decode("ascii"),
                "response_metadata": {"byte_layer": "transport", "source": "observed_headers",
                                      "content_encoding": encoding, "content_type": content_type}}
    if error:
        exchange["error"] = error
    wire = WireRequestSummary(body_sha256=sha256(request.encode()).hexdigest(), body_bytes=len(request),
        system_sha256="0" * 64, tools_sha256="0" * 64, messages_sha256="0" * 64,
        message_count=0, tool_count=0, message_hashes=[])
    attempt = RequestAttempt(number=1, model="captured", provider="local", purpose="compaction", status="completed",
        observation=RequestObservation(provider="local", endpoint="https://captured.invalid", protocol="deepseek",
                                       model="captured", finish_reason="length", wire=wire))
    record = ConversationRecord(version=10, messages=[], turns=[Turn(prompt="Summarize existing material.", answer="",
        status="failed", task=Task(id="captured-task", goal="Summarize existing material.", scope=None), request_attempts=[attempt])])
    (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": [sdk], "exchanges": [exchange]}), encoding="utf-8")
    context = CaseContext(root=tmp_path, protocol="deepseek", run_id="local-capture", case_id="compaction.capture")
    session = SimpleNamespace(exchanges=[exchange], sdk_inputs=[sdk])
    return context, record, session, exchange


def test_gzip_response_is_readable_without_changing_original_fields_or_truncated_status(tmp_path):
    text = '{"choices":[{"message":{"content":"原材料"},"finish_reason":"length"}]}'
    raw = gzip.compress(text.encode("utf-8"), mtime=0)
    context, record, session, exchange = captured_report(tmp_path, raw)
    original = (tmp_path / "exchange.json").read_bytes()
    path = write_review(context, record, session, provenance="current_real")
    report = json.loads(path.read_bytes())
    sample = report["samples"][0]
    assert sample["response_utf8"] == exchange["received_utf8"]
    assert sample["response_sha256"] == sha256(exchange["received_utf8"].encode()).hexdigest()
    assert sample["response_evidence"]["text"] == text
    assert sample["response_evidence"]["raw_sha256"] == sha256(raw).hexdigest()
    assert sample["stage_result"] == "truncated"
    assert report["semantic_status"] == sample["semantic_status"] == "not_measured"
    assert set(sample["criteria"].values()) == {"unknown"}
    assert record.turns[0].status == "failed"
    assert (tmp_path / "exchange.json").read_bytes() == original


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_sse_comments_crlf_and_multiline_data_are_reviewed_as_events(tmp_path, encoding):
    text = ': comment\r\n\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"原材料"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
    raw = gzip.compress(text.encode("utf-8"), mtime=0) if encoding == "gzip" else text.encode("utf-8")
    context, record, session, _ = captured_report(tmp_path, raw, encoding=encoding, content_type="text/event-stream")
    report = json.loads(write_review(context, record, session).read_bytes())
    assert report["samples"][0]["response_evidence"]["events"] == [{"choices": [{"delta": {"content": "原材料"}}]}]
    assert report["samples"][0]["response_evidence"]["text"] == text


def test_decoded_text_cannot_be_changed_under_an_unchanged_legacy_response_hash(tmp_path):
    context, record, session, _ = captured_report(tmp_path, b'{"choices":[]}', encoding="identity")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    sample = report["samples"][0]
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"],
        response_evidence_sha256=sample["response_evidence_sha256"],
        reviewer="Local contract reviewer", criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["semantic_status"] == "not_measured"
    sample["response_evidence"]["text"] = "Changed after review."
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="decoded response binding"):
        apply_reviews(path, [review])


@pytest.mark.parametrize("change", ["view_hash", "raw_hash", "metadata", "missing_view", "missing_hash", "version", "downgrade"])
def test_v2_review_rejects_binding_changes_and_source_based_downgrade(tmp_path, change):
    context, record, session, _ = captured_report(tmp_path, b'{"choices":[]}', encoding="identity")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    sample = report["samples"][0]
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"],
        response_evidence_sha256=sample["response_evidence_sha256"], reviewer="Local contract reviewer",
        criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["samples"][0]["reviewer"] == "Local contract reviewer"
    if change == "view_hash":
        sample["response_evidence_sha256"] = "0" * 64
    elif change == "raw_hash":
        sample["response_evidence"]["raw_sha256"] = "0" * 64
    elif change == "metadata":
        sample["response_evidence"]["metadata"]["content_encoding"] = "gzip"
    elif change == "missing_view":
        del sample["response_evidence"]
    elif change == "missing_hash":
        del sample["response_evidence_sha256"]
    elif change == "version":
        report["response_binding_version"] = 2.0
    else:
        del report["response_binding_version"]
        del sample["response_evidence"]
        del sample["response_evidence_sha256"]
        review = review.model_copy(update={"response_evidence_sha256": None})
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        apply_reviews(path, [review])


def test_legacy_plain_report_and_old_review_keep_their_original_contract(tmp_path):
    context, record, session, _ = captured_report(tmp_path, b'{"choices":[]}', encoding="identity")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    original = json.loads((tmp_path / "exchange.json").read_bytes())
    del original["exchanges"][0]["response_metadata"]
    (tmp_path / "exchange.json").write_text(json.dumps(original), encoding="utf-8")
    report["exchange_sha256"] = sha256((tmp_path / "exchange.json").read_bytes()).hexdigest()
    del report["response_binding_version"]
    sample = report["samples"][0]
    del sample["response_evidence"]
    del sample["response_evidence_sha256"]
    path.write_text(json.dumps(report), encoding="utf-8")
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"], reviewer="Legacy reviewer",
        criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["samples"][0]["reviewer"] == "Legacy reviewer"


def test_old_gzip_capture_requires_explicit_interpretation_bound_to_original_and_raw_bytes(tmp_path):
    raw = gzip.compress(b'{"choices":[]}', mtime=0)
    context, record, session, exchange = captured_report(tmp_path, raw)
    del exchange["response_metadata"]
    original = {"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}
    (tmp_path / "exchange.json").write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        write_review(context, record, session, provenance="current_real")
    interpretation = {"exchange_sha256": sha256((tmp_path / "exchange.json").read_bytes()).hexdigest(),
        "raw_sha256": sha256(raw).hexdigest(), "encoding": "gzip", "content_type": "application/json",
        "basis": "Posthoc local captured-byte diagnostic; no historical header was recorded."}
    path = write_review(context, record, session, provenance="current_real",
                        legacy_interpretations={"captured-sdk": interpretation})
    sample = json.loads(path.read_bytes())["samples"][0]
    assert sample["response_evidence"]["metadata"]["source"] == "posthoc_explicit"
    assert sample["response_evidence"]["text"] == '{"choices":[]}'
    wrong = {**interpretation, "raw_sha256": "0" * 64}
    with pytest.raises(ValueError, match="interpretation"):
        write_review(context, record, session, provenance="current_real", legacy_interpretations={"captured-sdk": wrong})


def test_transport_failure_keeps_priority_over_unknown_encoding_and_malformed_body(tmp_path):
    context, record, session, _ = captured_report(tmp_path, b"not-json", encoding="unknown", status=503, error="ReadError")
    sample = json.loads(write_review(context, record, session).read_bytes())["samples"][0]
    assert sample["stage_result"] == "transport_failure"
    assert sample["response_evidence"]["status"] == "transport_failure"
    assert sample["response_evidence"]["events"] == []


def test_capture_metadata_is_limited_to_two_response_headers():
    from tests.support.agent_acceptance.recorded_response import capture_response_metadata

    response = httpx2.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json",
        "authorization": "must-not-be-recorded", "set-cookie": "must-not-be-recorded", "x-extra": "must-not-be-recorded"})
    assert capture_response_metadata(response) == {"byte_layer": "transport", "source": "observed_headers",
        "content_encoding": "gzip", "content_type": "application/json"}


def test_formal_recording_preserves_raw_bytes_and_utf8_across_chunks():
    from tests.support.agent_acceptance.recorded_response import RecordedResponseStream

    raw = '{"choices":[{"message":{"content":"原材料"}}]}'.encode("utf-8")
    split = raw.index("原".encode("utf-8")) + 1

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield raw[:split]
            yield raw[split:]

    async def read():
        capture = {}
        response = httpx2.Response(200, stream=RecordedResponseStream(Chunks(), capture, lambda: None))
        try:
            received = await response.aread()
        finally:
            await response.aclose()
        return capture, received

    capture, received = asyncio.run(read())
    assert received == raw
    assert capture["received_utf8"] == raw.decode("utf-8")
    assert capture["received_base64"] == b64encode(raw).decode("ascii")
    assert "response_body_state" not in capture


@pytest.mark.parametrize("streaming", [False, True])
def test_fixed_fixture_capture_has_explicit_metadata_and_original_bytes(streaming):
    from tests.support.agent_acceptance.replay import OfflineSession, Reply

    async def read():
        session = OfflineSession("deepseek", [Reply(text="Fixed material.")])
        response = session.respond(httpx2.Request("POST", "https://fixture.invalid", json={"stream": streaming}))
        try:
            raw = await response.aread()
        finally:
            await response.aclose()
        return session.exchanges[0], raw

    exchange, raw = asyncio.run(read())
    assert exchange["response_metadata"] == {"byte_layer": "transport", "source": "fixed_fixture", "content_encoding": None,
        "content_type": "text/event-stream" if streaming else "application/json"}
    assert exchange["received_base64"] == b64encode(raw).decode("ascii")
    assert "response_body_state" not in exchange


def test_capture_cannot_self_declare_posthoc_encoding_without_an_independent_interpretation(tmp_path):
    context, record, session, exchange = captured_report(tmp_path, b'{"choices":[]}', encoding="identity")
    exchange["response_metadata"].update(source="posthoc_explicit", interpretation={"exchange_sha256": "wrong", "raw_sha256": "wrong"})
    (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata|interpretation"):
        write_review(context, record, session)


@pytest.mark.parametrize("raw,encoding,content_type", [
    (b'{}', False, "application/json"), (b'{}', "unknown", "application/json"),
    (b'not-gzip', "gzip", "application/json"), (b'{"text":"\xff"}', "identity", "application/json"),
    (b'not-json', "identity", "application/json"), (b'{}', "identity", "text/event-stream"),
    (b'{}', "identity", "text/html"),
])
def test_unknown_or_malformed_successful_response_is_an_explicit_error(tmp_path, raw, encoding, content_type):
    context, record, session, _ = captured_report(tmp_path, raw, encoding=encoding, content_type=content_type)
    with pytest.raises(ValueError):
        write_review(context, record, session)


def test_legacy_error_report_still_accepts_its_original_lossy_text_contract(tmp_path):
    context, record, session, exchange = captured_report(tmp_path, b'\xff', status=503, error="ReadError")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    original = json.loads((tmp_path / "exchange.json").read_bytes())
    del original["exchanges"][0]["response_metadata"]
    (tmp_path / "exchange.json").write_text(json.dumps(original), encoding="utf-8")
    report["exchange_sha256"] = sha256((tmp_path / "exchange.json").read_bytes()).hexdigest()
    del report["response_binding_version"]
    sample = report["samples"][0]
    del sample["response_evidence"]
    del sample["response_evidence_sha256"]
    path.write_text(json.dumps(report), encoding="utf-8")
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"], reviewer="Legacy failure reviewer",
        criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["samples"][0]["stage_result"] == "transport_failure"


def test_new_failure_review_binds_raw_bytes_even_when_legacy_text_stays_the_same(tmp_path):
    from tests.support.agent_acceptance.compaction_review import fingerprint
    from tests.support.agent_acceptance.recorded_response import response_evidence

    context, record, session, _ = captured_report(tmp_path, b'\xff', status=503, error="ReadError")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    sample = report["samples"][0]
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"],
        response_evidence_sha256=sample["response_evidence_sha256"], reviewer="Failure contract reviewer",
        criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["samples"][0]["response_evidence"]["raw_sha256"] == sha256(b'\xff').hexdigest()
    original = json.loads((tmp_path / "exchange.json").read_bytes())
    changed = original["exchanges"][0]
    changed["received_base64"] = b64encode(b'\xfe').decode("ascii")
    assert b'\xfe'.decode("utf-8", errors="replace") == changed["received_utf8"]
    (tmp_path / "exchange.json").write_text(json.dumps(original), encoding="utf-8")
    report["exchange_sha256"] = sha256((tmp_path / "exchange.json").read_bytes()).hexdigest()
    sample["response_evidence"] = response_evidence(changed)
    sample["response_evidence_sha256"] = fingerprint(sample["response_evidence"])
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="decoded response binding"):
        apply_reviews(path, [review])


def test_old_fixture_text_uses_an_explicit_bound_generation_contract_without_faking_raw_bytes(tmp_path):
    context, record, session, exchange = captured_report(tmp_path, b'{"choices":[]}', encoding="identity")
    del exchange["received_base64"]
    del exchange["response_metadata"]
    (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}), encoding="utf-8")
    interpretation = {"exchange_sha256": sha256((tmp_path / "exchange.json").read_bytes()).hexdigest(),
        "text_sha256": sha256(exchange["received_utf8"].encode()).hexdigest(), "encoding": "identity",
        "content_type": "application/json", "contract": "offline-session-identity-v1",
        "basis": "Explicit legacy OfflineSession identity generation contract; transport bytes were not stored."}
    path = write_review(context, record, session, legacy_interpretations={"captured-sdk": interpretation})
    sample = json.loads(path.read_bytes())["samples"][0]
    assert sample["response_evidence"]["text"] == '{"choices":[]}'
    assert sample["response_evidence"]["raw_sha256"] is None
    assert sample["response_evidence"]["raw_source"] == "explicit_fixed_fixture_text_contract"
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"],
        wire_sha256=sample["wire_sha256"], response_sha256=sample["response_sha256"],
        response_evidence_sha256=sample["response_evidence_sha256"], reviewer="Legacy fixture contract reviewer",
        criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["semantic_status"] == "not_measured"
    with pytest.raises(ValueError, match="interpretation"):
        write_review(context, record, session, legacy_interpretations={"captured-sdk": {**interpretation, "text_sha256": "0" * 64}})


def test_cancelled_response_before_body_is_incomplete_and_still_bound_to_its_capture(tmp_path):
    context, record, session, exchange = captured_report(tmp_path, b"", encoding="identity", content_type="text/event-stream")
    del exchange["received_base64"]
    exchange["response_metadata"]["source"] = "fixed_fixture"
    exchange["response_body_state"] = "cancelled_before_delivery"
    (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}), encoding="utf-8")
    path = write_review(context, record, session)
    report = json.loads(path.read_bytes())
    sample = report["samples"][0]
    assert sample["stage_result"] == "incomplete"
    assert sample["response_evidence"]["status"] == "incomplete"
    assert sample["response_evidence"]["response_body_state"] == "cancelled_before_delivery"
    assert sample["response_evidence"]["raw_sha256"] is None
    assert sample["response_evidence"]["text"] is None
    assert sample["semantic_status"] == "not_measured"
    review = HumanReview(sample_id=sample["sample_id"], sdk_sha256=sample["sdk_sha256"], wire_sha256=sample["wire_sha256"],
        response_sha256=sample["response_sha256"], response_evidence_sha256=sample["response_evidence_sha256"],
        reviewer="Cancelled capture contract reviewer", criteria=dict.fromkeys(CRITERIA, "unknown"), notes={})
    assert apply_reviews(path, [review])["semantic_status"] == "not_measured"
    sample["response_evidence"]["response_body_state"] = "complete"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="binding"):
        apply_reviews(path, [review])


@pytest.mark.parametrize("prefix", [b"", b"\xe5"])
def test_recorded_stream_marks_cancellation_and_preserves_any_partial_bytes(prefix):
    from tests.support.agent_acceptance.recorded_response import RecordedResponseStream

    async def cancel_before_body():
        entered = asyncio.Event()
        held = asyncio.Event()

        class Held(httpx2.AsyncByteStream):
            async def __aiter__(self):
                if prefix:
                    yield prefix
                entered.set()
                await held.wait()
                yield b"unreached"

        capture = {"received_utf8": ""}
        response = httpx2.Response(200, stream=RecordedResponseStream(Held(), capture, lambda: None))
        task = asyncio.create_task(response.aread())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await response.aclose()
        return capture

    capture = asyncio.run(cancel_before_body())
    assert capture["response_body_state"] == "cancelled_during_body"
    assert capture["received_utf8"] == prefix.decode("utf-8", errors="replace")
    if prefix:
        assert capture["received_base64"] == b64encode(prefix).decode("ascii")
    else:
        assert "received_base64" not in capture


def test_cancelled_partial_body_keeps_raw_fingerprint_without_decoding_it(tmp_path):
    raw = b"\x1f\x8b"
    context, record, session, exchange = captured_report(tmp_path, raw, encoding="gzip")
    exchange["response_body_state"] = "cancelled_during_body"
    (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}), encoding="utf-8")
    sample = json.loads(write_review(context, record, session).read_bytes())["samples"][0]
    assert sample["stage_result"] == "incomplete"
    assert sample["response_evidence"]["raw_sha256"] == sha256(raw).hexdigest()
    assert sample["response_evidence"]["text"] is None
    assert sample["response_evidence"]["events"] == []


@pytest.mark.parametrize("missing_raw", [False, True])
def test_empty_200_without_explicit_cancellation_is_not_accepted_as_incomplete(tmp_path, missing_raw):
    context, record, session, exchange = captured_report(tmp_path, b"", encoding="identity")
    if missing_raw:
        del exchange["received_base64"]
        (tmp_path / "exchange.json").write_text(json.dumps({"sdk_inputs": session.sdk_inputs, "exchanges": session.exchanges}), encoding="utf-8")
    with pytest.raises(ValueError):
        write_review(context, record, session)
