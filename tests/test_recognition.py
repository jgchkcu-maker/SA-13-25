import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import types
from test_models_formatting import make_row

from schedule_bot.models import Extraction, accepted_rows
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


@pytest.mark.parametrize("review_case", ["corrected", "unclear", "wrong_lesson", "wrong_group"])
async def test_teacher_is_independently_rechecked_on_original_image(tmp_path, review_case):
    first = make_row(
        group_text="СА-13",
        teacher="Горохов ИВ",
        room="208",
        evidence="СА-13 2 Математика Горохов ИВ 208",
    )
    corrected = first.model_copy(
        update={
            "teacher": "Михайлова СВ",
            "evidence": "СА-13 2 Математика Михайлова СВ 208",
        }
    )
    if review_case == "unclear":
        corrected = corrected.model_copy(update={"teacher": None})
    elif review_case == "wrong_lesson":
        corrected = corrected.model_copy(update={"lesson": "3"})
    elif review_case == "wrong_group":
        corrected = corrected.model_copy(update={"group_text": "СА-15"})
    results = [
        Extraction(status="found", rows=[first]),
        Extraction(status="found", rows=[corrected]),
    ]
    requests = []

    def handler(request):
        result = results[len(requests)]
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": result.model_dump_json()}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    path = tmp_path / "table.jpg"
    path.write_bytes(b"test image bytes")
    publication = SimpleNamespace(images=[path], caption="table", published_at="2026-10-20")
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.model = "gemini-3.5-flash-lite"
    recognizer.client = genai.Client(
        api_key="not-a-real-key",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            api_version="v1beta",
        ),
    )
    try:
        result = await recognizer.extract(publication)
    finally:
        recognizer.close()
    assert len(requests) == 2
    assert "Горохов" not in json.dumps(requests[1], ensure_ascii=False)
    assert "Михайлова" not in json.dumps(requests[1], ensure_ascii=False)
    for request in requests:
        thinking = request["generationConfig"]["thinkingConfig"]
        assert thinking.get("thinkingLevel", thinking.get("thinking_level")).upper() == "HIGH"
        assert any("inlineData" in part for part in request["contents"][0]["parts"])
    rows = accepted_rows(result, 1)
    if review_case in ("wrong_lesson", "wrong_group"):
        assert result.status == "unreadable"
        assert rows == []
    else:
        assert rows[0]["teacher"] == ("Михайлова СВ" if review_case == "corrected" else None)
