"""Exercise the desktop commands with the pinned Google SDK and offline HTTP."""

import asyncio
import json
import sys
from pathlib import Path

import httpx2
from pydantic_ai import models

from myrk.desktop_service import serve
from myrk.provider import deepseek_model


def respond(request: httpx2.Request) -> httpx2.Response:
    if request.headers["x-goog-api-key"] == "catalog-rejected":
        return httpx2.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "controlled catalog rejection"}})
    if request.method == "GET":
        if "pageToken" not in request.url.params:
            return httpx2.Response(200, json={"models": [{"name": "models/gemini-first", "displayName": "首个型号", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "next"})
        return httpx2.Response(200, json={"models": [{"name": "models/gemini-2.5-flash", "displayName": "后页型号", "supportedGenerationMethods": ["generateContent"]}]})
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "原生文字流"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}}
    return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})


async def run() -> None:
    models.ALLOW_MODEL_REQUESTS = True
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        await serve(Path(sys.argv[1]), deepseek_model, http_client=http)


if __name__ == "__main__":
    asyncio.run(run())
