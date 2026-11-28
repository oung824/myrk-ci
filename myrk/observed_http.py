from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
import json
import re
from types import TracebackType
from typing import Self

import httpx2

from myrk.request_observation import _Capture, _active_capture


_SSE_LINE_END = re.compile(rb"[\r\n]")


class _ResponseBody:
    def __init__(self, capture: _Capture, *, sse: bool) -> None:
        self.capture, self.sse = capture, sse
        self.buffer = bytearray()
        self.data_lines: list[bytes] = []
        self.after_cr = False

    def receive_json(self, data: bytes | bytearray) -> None:
        try:
            value = json.loads(data)
        except ValueError:
            return
        self.capture.receive(value)

    def receive_sse_line(self, line: bytes) -> None:
        if not line and self.data_lines:
            data = b"\n".join(self.data_lines)
            self.data_lines.clear()
            if data.strip() != b"[DONE]":
                self.receive_json(data)
        elif line.startswith(b"data:"):
            self.data_lines.append(line[5:].removeprefix(b" "))

    def feed(self, chunk: bytes) -> None:
        if not self.sse:
            self.buffer.extend(chunk)
            return
        start = 0
        for match in _SSE_LINE_END.finditer(chunk):
            end, separator = match.start(), match.group()
            if not (self.after_cr and start == end and separator == b"\n"):
                self.buffer.extend(chunk[start:end])
                self.receive_sse_line(bytes(self.buffer))
                self.buffer.clear()
            self.after_cr = separator == b"\r"
            start = end + 1
        if start < len(chunk):
            self.after_cr = False
            self.buffer.extend(chunk[start:])

    def finish(self) -> None:
        if self.sse:
            if self.buffer:
                self.receive_sse_line(bytes(self.buffer))
            self.receive_sse_line(b"")
        else:
            self.receive_json(self.buffer)


class _ResponseStream(httpx2.AsyncByteStream):
    def __init__(self, response: httpx2.Response, stream: httpx2.AsyncByteStream) -> None:
        self._response = response
        self._stream = stream

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._stream.__aiter__()

    async def aclose(self) -> None:
        await self._response.aclose()


class _ObservedResponse(httpx2.Response):
    def __init__(self, response: httpx2.Response, stream: httpx2.AsyncByteStream, body: _ResponseBody) -> None:
        if response.is_stream_consumed:
            super().__init__(
                response.status_code, content=response.content, extensions=response.extensions,
                default_encoding=response.default_encoding,
            )
            self.headers = httpx2.Headers(response.headers)
            self.stream = stream
        else:
            super().__init__(
                response.status_code, headers=response.headers, stream=_ResponseStream(response, stream),
                extensions=response.extensions, default_encoding=response.default_encoding,
            )
        self._observation_body: _ResponseBody | None = body

    async def aread(self) -> bytes:
        content = await super().aread()
        body = self._observation_body
        if body is not None:
            self._observation_body = None
            body.feed(content)
            body.finish()
        return content

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncGenerator[bytes, None]:
        body, self._observation_body = self._observation_body, None
        async with aclosing(super().aiter_bytes(chunk_size)) as decoded:
            async for chunk in decoded:
                if body is not None:
                    body.feed(chunk)
                yield chunk
        if body is not None:
            body.finish()


class ObservedHTTPTransport(httpx2.AsyncBaseTransport):
    """Opt in to request-scoped native observation when constructing an HTTPX client."""

    def __init__(self, transport: httpx2.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        capture = _active_capture.get()
        if capture is None or capture.observation.protocol == "unknown":
            return await self._transport.handle_async_request(request)
        capture.observation.native_http = True
        capture.observation.wire = None
        try:
            capture.record_http_request(request.content)
        except httpx2.RequestNotRead:
            pass
        response = await self._transport.handle_async_request(request)
        if type(response) is not httpx2.Response or not isinstance(response.stream, httpx2.AsyncByteStream):
            return response
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        body = _ResponseBody(capture, sse=media_type == "text/event-stream")
        return _ObservedResponse(response, response.stream, body)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> Self:
        await self._transport.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None = None, exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self._transport.__aexit__(exc_type, exc_value, traceback)
