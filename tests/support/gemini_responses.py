import json

import httpx2

from myrk.provider import ProviderConfig


def google_config(model: str = "gemini-2.5-flash") -> ProviderConfig:
    return ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model=model, protocol="google", context_window_override=128000)


def google_stream(parts: list[dict], finish: str | None = "STOP", *, usage: dict | None = None) -> httpx2.Response:
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish is not None:
        candidate["finishReason"] = finish
    chunk = {"candidates": [candidate]}
    if usage is not None:
        chunk["usageMetadata"] = usage
    return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})
