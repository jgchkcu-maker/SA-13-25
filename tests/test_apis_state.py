import base64
import hashlib
import json
import secrets

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from schedule_bot.state import GitHubStore, LocalStore, empty_state
from schedule_bot.telegram import BotAPI


async def test_bot_timeout_is_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("lost response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bot = BotAPI(http, "secret-token", "-100123")
        with pytest.raises(RuntimeError, match="Неоднозначный") as error:
            await bot.send("text")
    assert "secret-token" not in str(error.value)
    assert count == 1


async def test_bot_html_topic_and_idempotent_edit():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if request.url.path.endswith("editMessageText"):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bot = BotAPI(http, "token", "-100123", 55)
        assert await bot.send("<b>Текст</b>") == 7
        assert await bot.edit(7, "<b>Текст</b>") == 7
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["message_thread_id"] == 55
    assert calls[0]["link_preview_options"]["is_disabled"] is True


async def test_bot_sends_captioned_album_in_chunks(tmp_path):
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request.content))
        if method == "sendMediaGroup":
            return httpx.Response(
                200, json={"ok": True, "result": [{"message_id": i} for i in range(10)]}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    paths = []
    for i in range(11):
        path = tmp_path / f"{i}.jpg"
        path.write_bytes(b"jpeg")
        paths.append(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bot = BotAPI(http, "token", "-100123", 55)
        ids = await bot.send_album(paths, "<b>Изменения</b>")
    assert ids == [*range(10), 99]
    assert [method for method, _ in calls] == ["sendMediaGroup", "sendPhoto"]
    first = calls[0][1].decode()
    assert "attach://photo9" in first and "message_thread_id" in first
    assert "caption" in first and "reply_parameters" not in first
    second = calls[1][1].decode()
    assert "caption" not in second and "true" in second  # silent continuation


async def test_bot_edits_album_photo_and_caption(tmp_path):
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request.content))
        if method == "editMessageCaption":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    path = tmp_path / "1.jpg"
    path.write_bytes(b"jpeg")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bot = BotAPI(http, "token", "-100123")
        assert await bot.edit_photo(5, path, "<b>Новое</b>") == 5
        assert await bot.edit_caption(5, "<b>Новое</b>") == 5
    assert calls[0][0] == "editMessageMedia" and "attach://photo0" in calls[0][1].decode()


async def test_local_missing_corrupt_state_never_silently_resets(tmp_path):
    store = LocalStore(str(tmp_path / "state.json"))
    with pytest.raises(RuntimeError):
        await store.load()
    await store.initialize()
    with pytest.raises(RuntimeError):
        await store.initialize()
    assert await store.load() == empty_state()
    store.path.write_text('{"schema": 999}', encoding="utf-8")
    with pytest.raises(ValueError):
        await store.load()


async def test_github_state_uses_sha_and_separate_branch():
    calls = []
    state = empty_state()
    key = hashlib.sha256(b"state-encryption-key").digest()
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key)
    raw_state = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    encrypted = cipher.encrypt(nonce, raw_state, GitHubStore.AAD)
    envelope = {
        "format": GitHubStore.FORMAT,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(encrypted).decode(),
    }

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sha": "old-sha",
                    "encoding": "base64",
                    "content": base64.b64encode(json.dumps(envelope).encode()).decode(),
                },
            )
        payload = json.loads(request.content)
        assert payload["sha"] == "old-sha"
        assert payload["branch"] == "bot-state"
        return httpx.Response(200, json={"content": {"sha": "new-sha"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        store = GitHubStore(http, "owner/repo", "secret", "bot-state", "state-encryption-key")
        assert await store.load() == state
        await store.save(state)
        assert store.sha == "old-sha"
        assert len(calls) == 1  # No no-op commits on the 20-minute schedule.
        state["cursor"] = 42
        await store.save(state)
        assert store.sha == "new-sha"
        assert len(calls) == 2
        saved = json.loads(base64.b64decode(json.loads(calls[-1].content)["content"]))
        recovered = cipher.decrypt(
            base64.b64decode(saved["nonce"]), base64.b64decode(saved["ciphertext"]), GitHubStore.AAD
        )
        assert json.loads(recovered) == state
    assert calls[0].url.params["ref"] == "bot-state"


async def test_public_github_state_payload_is_encrypted_and_bound_to_key():
    saved = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"sha": "old-sha", "encoding": "base64", "content": saved["content"]}
            )
        saved.update(json.loads(request.content))
        return httpx.Response(200, json={"content": {"sha": "new-sha"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        store = GitHubStore(http, "owner/repo", "token", "bot-state", "state-encryption-key")
        state = empty_state() | {"scope": {"group": "СА-13-25", "chat": "-100"}}
        await store.save(state)
        restored = GitHubStore(http, "owner/repo", "token", "bot-state", "state-encryption-key")
        assert await restored.load() == state
        other = GitHubStore(http, "owner/repo", "token", "bot-state", "wrong-encryption-key")
        with pytest.raises(ValueError, match="STATE_ENCRYPTION_KEY"):
            await other.load()
    raw = base64.b64decode(saved["content"])
    assert "СА-13-25".encode() not in raw
    assert json.loads(raw)["format"] == GitHubStore.FORMAT


async def test_missing_state_branch_is_created_from_default_branch():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/git/ref/heads/bot-state"):
            return httpx.Response(404, json={})
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        return httpx.Response(201, json={"content": {"sha": "new"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        store = GitHubStore(http, "owner/repo", "token", "bot-state", "state-encryption-key")
        await store.ensure_initialized()
    assert ("GET", "/repos/owner/repo") in calls
    assert ("POST", "/repos/owner/repo/git/refs") in calls
    assert calls[-1] == ("PUT", "/repos/owner/repo/contents/state.json")
