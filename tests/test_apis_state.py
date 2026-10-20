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
    key = hashlib.sha256(b"session-secret").digest()
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key)
    raw_state = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    encrypted = cipher.encrypt(nonce, raw_state, GitHubStore.AAD)
    envelope = {"format": GitHubStore.FORMAT, "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(encrypted).decode()}

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
        store = GitHubStore(http, "owner/repo", "secret", "bot-state", "session-secret")
        assert await store.load() == state
        await store.save(state)
        assert store.sha == "new-sha"
        assert len(calls) == 1  # No no-op commits on the 20-minute schedule.
    assert calls[0].url.params["ref"] == "bot-state"


async def test_public_github_state_payload_is_encrypted_and_bound_to_session():
    saved = {}

    def handler(request):
        if request.method == "GET":
            envelope = {"format": GitHubStore.FORMAT,
                        "nonce": base64.b64encode(b"n" * 12).decode(),
                        "ciphertext": base64.b64encode(b"c" * 40).decode()}
            return httpx.Response(200, json={"sha": "old-sha", "encoding": "base64",
                                               "content": base64.b64encode(json.dumps(envelope).encode()).decode()})
        saved.update(json.loads(request.content))
        return httpx.Response(200, json={"content": {"sha": "new-sha"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        store = GitHubStore(http, "owner/repo", "token", "bot-state", "session-secret")
        await store.save(empty_state() | {"scope": {"group": "СА-13-25", "chat": "-100"}})
        other = GitHubStore(http, "owner/repo", "token", "bot-state", "different-session")
        with pytest.raises(ValueError, match="TELEGRAM_SESSION"):
            await other.load()
    raw = base64.b64decode(saved["content"])
    assert b"СА-13-25" not in raw
    assert json.loads(raw)["format"] == GitHubStore.FORMAT
