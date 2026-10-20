import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_models_formatting import make_row

from schedule_bot.engine import DeliveryUncertain, Engine
from schedule_bot.models import Extraction
from schedule_bot.state import empty_state


class Store:
    def __init__(self):
        self.saved = empty_state()

    async def save(self, state):
        self.saved = copy.deepcopy(state)


class Bot:
    def __init__(self):
        self.sent, self.edited = [], []
        self.crash_after_send = False

    async def send(self, text):
        self.sent.append(text)
        if self.crash_after_send:
            raise RuntimeError("Simulated lost HTTP response")
        return len(self.sent)

    async def edit(self, message_id, text):
        self.edited.append((message_id, text))
        return message_id


class OCR:
    def __init__(self, result=None):
        self.result = result or Extraction(status="found", rows=[make_row()])

    async def extract(self, publication):
        return self.result


def publication(key="post:1", revision="v1"):
    return SimpleNamespace(
        key=key,
        ids=[int(key.split(":")[1])],
        fingerprint=revision,
        metadata_fingerprint=revision,
        images=[Path("test.jpg")],
        url="https://t.me/test/1",
    )


@pytest.fixture
def setup():
    store, bot, ocr = Store(), Bot(), OCR()
    return Engine(empty_state(), store, bot, ocr), store, bot, ocr


async def test_repost_deduplicated(setup):
    engine, store, bot, _ = setup
    await engine.run([publication()], 1)
    await engine.run([publication("post:2")], 2)
    assert len(bot.sent) == 1
    assert store.saved["cursor"] == 2


async def test_edit_updates_existing_message(setup):
    engine, _, bot, ocr = setup
    await engine.run([publication()], 1)
    ocr.result = Extraction(status="found", rows=[make_row(room="305")])
    await engine.run([publication(revision="v2")], 1)
    assert len(bot.sent) == 1
    assert bot.edited[0][0] == 1
    assert "305" in bot.edited[0][1]


async def test_teacher_correction_edits_sent_card_without_duplicate(setup):
    engine, _, bot, ocr = setup
    ocr.result = Extraction(
        status="found",
        rows=[
            make_row(
                teacher="Горохов ИВ",
                evidence="СА-13-25 2 Математика Горохов ИВ 204",
            )
        ],
    )
    await engine.run([publication()], 1)
    ocr.result = Extraction(
        status="found",
        rows=[
            make_row(
                teacher="Михайлова СВ",
                evidence="СА-13-25 2 Математика Михайлова СВ 204",
            )
        ],
    )
    await engine.run([publication(revision="verified-v3")], 1)
    await engine.run([publication(revision="verified-v3")], 1)
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert bot.edited[0][0] == 1
    assert "Михайлова СВ" in bot.edited[0][1]
    assert "Горохов ИВ" not in bot.edited[0][1]


@pytest.mark.parametrize("status", ["not_found", "unreadable"])
async def test_no_group_or_unreadable_initial_silent(setup, status):
    engine, _, bot, ocr = setup
    ocr.result = Extraction(status=status, rows=[])
    await engine.run([publication()], 1)
    assert bot.sent == []


async def test_unreadable_edit_invalidates_previous(setup):
    engine, _, bot, ocr = setup
    await engine.run([publication()], 1)
    ocr.result = Extraction(status="unreadable", rows=[])
    await engine.run([publication(revision="v2")], 1)
    assert "не удаётся подтвердить" in bot.edited[0][1]
    assert len(bot.sent) == 1


async def test_crash_never_blindly_resends(setup):
    engine, store, bot, _ = setup
    bot.crash_after_send = True
    with pytest.raises(RuntimeError):
        await engine.run([publication()], 1)
    restored = store.saved
    assert restored["pending"]["kind"] == "send"
    assert restored["cursor"] == 0
    restarted = Engine(restored, store, bot, OCR())
    with pytest.raises(DeliveryUncertain):
        await restarted.recover()
    assert len(bot.sent) == 1
    # Operator found the already-delivered message; recover without another send.
    restarted.complete_pending(1)
    await restarted.save()
    await restarted.run([publication()], 1)
    assert len(bot.sent) == 1
    assert store.saved["pending"] is None


async def test_edit_intent_recovered_idempotently(setup):
    engine, store, bot, _ = setup
    await engine.run([publication()], 1)
    engine.state["pending"] = {
        "kind": "edit",
        "post": "post:1",
        "page": 0,
        "message_id": 1,
        "text": "Updated",
    }
    await engine.recover()
    assert bot.edited == [(1, "Updated")]
    assert store.saved["pending"] is None


async def test_ocr_failure_does_not_advance_cursor(setup):
    engine, store, _, ocr = setup

    async def fail(_):
        raise RuntimeError("OCR unavailable")

    ocr.extract = fail
    with pytest.raises(RuntimeError):
        await engine.run([publication()], 10)
    assert store.saved["cursor"] == 0


async def test_explicit_telegram_rejection_can_retry_without_manual_recovery(setup):
    from schedule_bot.telegram import DeliveryRejected

    engine, store, bot, _ = setup

    async def rejected(_):
        raise DeliveryRejected("403")

    bot.send = rejected
    with pytest.raises(DeliveryRejected):
        await engine.run([publication()], 1)
    assert store.saved["pending"] is None
    assert store.saved["cursor"] == 0
