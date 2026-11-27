"""Decode captured response bytes for reports without changing their originals."""

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from collections.abc import AsyncIterator, Callable
import asyncio
from hashlib import sha256
import json
from typing import Any

import httpx2


def capture_response_metadata(response: httpx2.Response, *, source: str = "observed_headers") -> dict[str, Any]:
    """Only public content metadata belongs in a response capture."""
    return {"byte_layer": "transport", "source": source,
            "content_encoding": response.headers.get("content-encoding"), "content_type": response.headers.get("content-type")}


class RecordedResponseStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, record: dict[str, Any], persist: Callable[[], None],
                 complete: Callable[[], None] | None = None,
                 failed: Callable[[str], None] | None = None) -> None:
        self.stream, self.record, self.persist = stream, record, persist
        self.complete, self.failed = complete, failed
        self.received = b""
        self.terminal = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.stream:
                self.received += chunk
                self.record["received_base64"] = b64encode(self.received).decode("ascii")
                self.record["received_utf8"] = self.received.decode("utf-8", errors="replace")
                self.persist()
                yield chunk
            if not self.terminal:
                self.terminal = True
                if self.complete is not None:
                    self.complete()
        except asyncio.CancelledError:
            self.record["response_body_state"] = "cancelled_during_body"
            if not self.terminal:
                self.terminal = True
                if self.failed is not None:
                    self.failed("cancelled during response body")
            self.persist()
            raise
        except httpx2.HTTPError as error:
            self.record["error"] = f"{type(error).__name__}: {error}"
            if not self.terminal:
                self.terminal = True
                if self.failed is not None:
                    self.failed(self.record["error"])
            self.persist()
            raise

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream.aclose()
        finally:
            if not self.terminal:
                self.terminal = True
                self.record["response_body_state"] = "closed_before_eof"
                if self.failed is not None:
                    self.failed("response closed before EOF")
                self.persist()


def response_evidence(exchange: dict[str, Any], *, exchange_sha256: str | None = None,
                      legacy_interpretation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a separate, byte-bound response view; never send an HTTP request."""
    metadata = exchange.get("response_metadata")
    encoded = exchange.get("received_base64")
    evidence: dict[str, Any] = {"version": 2, "status": "decoded", "metadata": metadata,
        "raw_sha256": None, "decoded_sha256": None, "text": None, "events": [],
        "capture_base64_sha256": sha256(encoded.encode()).hexdigest() if isinstance(encoded, str) else None}
    body_state = exchange.get("response_body_state")
    if body_state is not None:
        evidence["response_body_state"] = body_state
    if exchange.get("not_sent") or exchange.get("error") or exchange.get("status") != 200:
        evidence["status"] = "not_sent" if exchange.get("not_sent") else "transport_failure"
        if isinstance(encoded, str):
            try:
                evidence["raw_sha256"] = sha256(b64decode(encoded, validate=True)).hexdigest()
            except Base64Error as error:
                evidence["capture_error"] = str(error)
        return evidence
    if metadata is not None and (not isinstance(metadata, dict) or metadata.get("source") not in {"observed_headers", "fixed_fixture"}):
        raise ValueError("Posthoc response interpretation must be supplied independently of captured metadata.")
    if metadata is None:
        if legacy_interpretation is None:
            raise ValueError("Recorded response lacks explicit encoding metadata.")
        if (not {"exchange_sha256", "encoding", "content_type", "basis"}.issubset(legacy_interpretation)
                or legacy_interpretation.get("exchange_sha256") != exchange_sha256
                or not exchange_sha256 or not legacy_interpretation.get("basis")):
            raise ValueError("Legacy response interpretation lacks its original exchange binding.")
        metadata = {"byte_layer": "transport", "source": "legacy_fixed_fixture_contract" if
                    legacy_interpretation.get("contract") == "offline-session-identity-v1" else "posthoc_explicit",
                    "content_encoding": legacy_interpretation["encoding"],
                    "content_type": legacy_interpretation["content_type"], "interpretation": legacy_interpretation}
    elif legacy_interpretation is not None:
        raise ValueError("Legacy interpretation cannot override observed response metadata.")
    fields = {"byte_layer", "source", "content_encoding", "content_type"}
    if metadata.get("source") in {"posthoc_explicit", "legacy_fixed_fixture_contract"}:
        fields.add("interpretation")
    if set(metadata) != fields:
        raise ValueError("Recorded response metadata contains unknown or missing fields.")
    if metadata.get("byte_layer") != "transport" or metadata.get("source") not in {"observed_headers", "fixed_fixture", "posthoc_explicit", "legacy_fixed_fixture_contract"}:
        raise ValueError("Recorded response metadata has an unknown byte layer or source.")
    if any(metadata[name] is not None and not isinstance(metadata[name], str) for name in ("content_encoding", "content_type")):
        raise ValueError("Recorded response content metadata must be text or an explicitly absent header.")
    if body_state is not None:
        if body_state not in {"cancelled_before_delivery", "cancelled_during_body", "closed_before_eof"} or metadata["source"] not in {"observed_headers", "fixed_fixture"}:
            raise ValueError("Unknown or unsupported recorded response body state.")
        if body_state == "cancelled_before_delivery" and metadata["source"] != "fixed_fixture":
            raise ValueError("A response held before delivery must have an explicit fixed fixture source.")
        if isinstance(encoded, str):
            evidence["raw_sha256"] = sha256(b64decode(encoded, validate=True)).hexdigest()
            evidence["raw_source"] = "captured_base64"
        elif encoded is None and exchange["received_utf8"] == "":
            evidence["raw_source"] = "no_body_bytes_received"
        else:
            raise ValueError("Cancelled response text lacks its original captured bytes.")
        evidence["status"] = "incomplete"
        return evidence
    encoding = (metadata["content_encoding"] or "identity").strip().lower()
    if encoding not in {"identity", "gzip"}:
        raise ValueError(f"Unsupported recorded content encoding: {encoding!r}")
    content_type = (metadata["content_type"] or "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "text/event-stream"}:
        raise ValueError(f"Unsupported recorded response format: {content_type!r}")
    if not isinstance(encoded, str):
        if metadata["source"] != "legacy_fixed_fixture_contract" or encoding != "identity":
            raise ValueError("Recorded response has no captured transport bytes.")
        raw = exchange["received_utf8"].encode("utf-8")
        if legacy_interpretation is None or legacy_interpretation.get("text_sha256") != sha256(raw).hexdigest():
            raise ValueError("Legacy fixture interpretation differs from its original text.")
        evidence["raw_source"] = "explicit_fixed_fixture_text_contract"
    else:
        raw = b64decode(encoded, validate=True)
        evidence["raw_sha256"] = sha256(raw).hexdigest()
        evidence["raw_source"] = "captured_base64"
    if legacy_interpretation is not None and legacy_interpretation.get("raw_sha256") != evidence["raw_sha256"]:
        raise ValueError("Legacy response interpretation differs from its original response bytes.")
    response = httpx2.Response(exchange["status"], request=httpx2.Request("POST", "https://captured.invalid"),
        headers={"content-encoding": encoding, "content-type": metadata["content_type"]}, stream=httpx2.ByteStream(raw))
    try:
        decoded = response.read()
        text = decoded.decode("utf-8")
        response.encoding = "utf-8"
        events = ([event.json() for event in httpx2.EventSource(response) if event.data and event.data.strip() != "[DONE]"]
                  if content_type == "text/event-stream" else [json.loads(text)])
        if any(not isinstance(event, dict) for event in events):
            raise ValueError("Recorded model response events must contain JSON objects.")
        if content_type == "text/event-stream" and not events:
            raise ValueError("Recorded SSE response has no JSON data events.")
    except (httpx2.DecodingError, httpx2.SSEError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Recorded response decoding failed: {type(error).__name__}: {error}") from error
    finally:
        response.close()
    evidence.update(metadata=metadata, decoded_sha256=sha256(decoded).hexdigest(), text=text, events=events)
    return evidence
