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
    recognizer.model = "gemini-3.5-flash-lite"
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
