import argparse
from unittest.mock import AsyncMock

import httpx
import pytest

from schedule_bot import cli
from schedule_bot.state import GitHubStore, LocalStore


async def test_local_first_run_initializes_but_never_resets(tmp_path):
    store = LocalStore(str(tmp_path / "state.json"))
    await store.ensure_initialized()
    state = await store.load()
    state["cursor"] = 123
    await store.save(state)
    await store.ensure_initialized()
    assert (await store.load())["cursor"] == 123
    store.path.write_text("broken", encoding="utf-8")
    await store.ensure_initialized()
    with pytest.raises(ValueError):
        await store.load()


@pytest.mark.parametrize(
    "status,created,error",
    [(404, True, False), (200, False, False), (403, False, True), (500, False, True)],
)
async def test_remote_only_initializes_missing_branch(status, created, error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status))
    ) as http:
        store = GitHubStore(http, "owner/repo", "token", "bot-state", "key")
        store.initialize = AsyncMock()
        if error:
            with pytest.raises(RuntimeError):
                await store.ensure_initialized()
        else:
            await store.ensure_initialized()
        assert store.initialize.await_count == int(created)


async def test_chat_id_replies_only_to_explicit_commands_and_hides_ids(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    calls = []

    async def call(self, method, payload):
        calls.append((method, payload))
        if method == "getMe":
            return {"username": "MyBot"}
        if method == "getUpdates":
            if "offset" in payload:
                return []
            return [
                {"update_id": i, "message": {"text": text, "chat": {"id": chat, "type": kind}}}
                for i, text, chat, kind in [
                    (1, "/chatid@MyBot", -100001, "supergroup"),
                    (2, "/chatid@OtherBot", -100002, "supergroup"),
                    (3, "hello", -100003, "group"),
                    (4, "/chatid", 1234, "private"),
                ]
            ]
        if method == "sendMessage":
            return {"message_id": 10}
        raise AssertionError(method)

    monkeypatch.setattr(cli.BotAPI, "call", call)
    await cli.execute(argparse.Namespace(command="chat-id"))
    sends = [payload for method, payload in calls if method == "sendMessage"]
    assert len(sends) == 1
    assert sends[0]["chat_id"] == "-100001"
    assert "-100001" in sends[0]["text"]
    assert "-100001" not in capsys.readouterr().out
    assert calls[-1][1]["offset"] == 5
