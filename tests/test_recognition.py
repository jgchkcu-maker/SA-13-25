import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import types

from schedule_bot.models import Extraction
from schedule_bot.recognition import Recognizer


@pytest.mark.parametrize("finish_reason", ["STOP", "MAX_TOKENS"])
async def test_real_sdk_request_contract(tmp_path, finish_reason):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": '{"status":"not_found","rows":[]}'}],
                        },
                        "finishReason": finish_reason,
                    }
                ]
            },
        )

    images = [tmp_path / f"{i}.jpg" for i in range(3)]
    for path in images:
        path.write_bytes(b"test image bytes")
    publication = SimpleNamespace(
        images=images, caption="table", published_at="2026-10-20T00:00:00Z"
    )
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.models = ("gemini-3.5-flash-lite",)
    recognizer.exhausted = set()
    recognizer.client = genai.Client(
        api_key="not-a-real-key",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)), api_version="v1beta"
        ),
    )
    try:
        if finish_reason == "STOP":
            assert await recognizer.extract(publication) == Extraction(status="not_found", rows=[])
        else:
            with pytest.raises(RuntimeError, match="неполный"):
                await recognizer.extract(publication)
    finally:
        recognizer.close()
    assert len(requests) == 1
    assert requests[0].url.path.endswith("gemini-3.5-flash-lite:generateContent")
    payload = json.loads(requests[0].content)
    assert sum("inlineData" in p for p in payload["contents"][0]["parts"]) == 3
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" in payload["generationConfig"]
    assert "temperature" not in payload["generationConfig"]


async def test_model_pool_falls_back_on_quota_and_remembers(tmp_path):
    called = []

    def handler(request):
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        called.append(model)
        if model == "strong":
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}},
            )
        if model == "middle":
            return httpx.Response(
                404, json={"error": {"code": 404, "message": "no model", "status": "NOT_FOUND"}}
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": '{"status":"not_found","rows":[]}'}],
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    image = tmp_path / "1.jpg"
    image.write_bytes(b"img")
    publication = SimpleNamespace(images=[image], caption="", published_at="2026-10-20")
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.models = ("strong", "middle", "lite")
    recognizer.exhausted = set()
    recognizer.client = genai.Client(
        api_key="not-a-real-key",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)), api_version="v1beta"
        ),
    )
    try:
        await recognizer.extract(publication)
        await recognizer.extract(publication)
        recognizer.exhausted.add("lite")
        with pytest.raises(RuntimeError, match="исчерпаны"):
            await recognizer.extract(publication)
    finally:
        recognizer.close()
    # Exhausted models are not retried within the same run.
    assert called == ["strong", "middle", "lite", "lite"]


def test_model_pool_config(monkeypatch):
    from schedule_bot.config import models

    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    assert models() == ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_MODELS", " a, b ,a,, ")
    assert models() == ("a", "b")
    monkeypatch.setenv("GEMINI_MODELS", " , ")
    with pytest.raises(ValueError):
        models()
