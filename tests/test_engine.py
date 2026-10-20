import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_models_formatting import make_row

from schedule_bot.engine import DeliveryUncertain, Engine
from schedule_bot.models import Extraction
from schedule_bot.state import empty_state
from schedule_bot.telegram import DeliveryRejected


class Store:
    def __init__(self):
        self.saved = empty_state()

    async def save(self, state):
        self.saved = copy.deepcopy(state)


class Bot:
    def __init__(self):
        self.sent, self.edited, self.albums, self.captions, self.photos = [], [], [], [], []
        self.crash_after_send = False

    async def send_album(self, paths, caption):
        self.albums.append((list(paths), caption))
        return [100 * len(self.albums) + i for i in range(len(paths))]

    async def edit_caption(self, message_id, caption):
        self.captions.append((message_id, caption))
        return message_id

    async def edit_photo(self, message_id, path, caption):
        self.photos.append((message_id, path, caption))
        return message_id

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


def publication(key="post:1", revision="v1", images=("test.jpg",)):
    return SimpleNamespace(
        key=key,
        ids=[int(key.split(":")[1])],
        fingerprint=revision,
        metadata_fingerprint=revision,
        images=[Path(name) for name in images],
        url="https://t.me/test/1",
    )


@pytest.fixture(autouse=True)
def image_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("test.jpg").write_bytes(b"photo-v1")
    Path("second.jpg").write_bytes(b"photo-2")


@pytest.fixture
def setup():
    store, bot, ocr = Store(), Bot(), OCR()
    return Engine(empty_state(), store, bot, ocr), store, bot, ocr


async def test_changes_sent_as_caption_of_source_photos(setup):
    engine, store, bot, _ = setup
    await engine.run([publication(images=("test.jpg", "second.jpg"))], 1)
    paths, caption = bot.albums[0]
    assert paths == [Path("test.jpg"), Path("second.jpg")]
    for text in ("Изменения в расписании", "2 пара", "Математика", "Иванова А. Б.", "204"):
        assert text in caption
    assert bot.sent == []
    post = store.saved["posts"]["post:1"]
    assert post["album"]["ids"] == [100, 101]
    assert post["pages"] == [{"id": 100, "text": caption, "caption": True}]


async def test_repost_deduplicated(setup):
    engine, store, bot, _ = setup
    await engine.run([publication()], 1)
    await engine.run([publication("post:2")], 2)
    assert len(bot.albums) == 1
    assert store.saved["cursor"] == 2


async def test_text_edit_updates_caption_in_place(setup):
    engine, _, bot, ocr = setup
    await engine.run([publication()], 1)
    ocr.result = Extraction(status="found", rows=[make_row(room="305")])
    await engine.run([publication(revision="v2")], 1)
    assert len(bot.albums) == 1
    assert bot.photos == []
    assert bot.captions[0][0] == 100
    assert "305" in bot.captions[0][1] and "Уточнение" in bot.captions[0][1]


async def test_changed_photos_replaced_in_same_album(setup):
    engine, _, bot, _ = setup
    await engine.run([publication()], 1)
    Path("test.jpg").write_bytes(b"photo-v2")
    await engine.run([publication(revision="v2")], 1)
    assert len(bot.albums) == 1
    assert bot.photos[0][0] == 100 and "Уточнение" in bot.photos[0][2]
    assert bot.captions == []


async def test_photo_count_change_sends_new_album_and_marks_old(setup):
    engine, store, bot, _ = setup
    await engine.run([publication()], 1)
    await engine.run([publication(revision="v2", images=("test.jpg", "second.jpg"))], 1)
    assert bot.captions[0][0] == 100 and "устарело" in bot.captions[0][1]
    assert len(bot.albums) == 2 and "Уточнение" in bot.albums[1][1]
    assert store.saved["posts"]["post:1"]["album"]["ids"] == [200, 201]


async def test_legacy_text_notification_replaced_by_album(setup):
    engine, store, bot, _ = setup
    store.saved["posts"]["post:1"] = {"ids": [1], "pages": [{"id": 7, "text": "old"}]}
    engine.state = copy.deepcopy(store.saved)
    await engine.run([publication(revision="v2")], 1)
    assert bot.edited[0][0] == 7 and "устарело" in bot.edited[0][1]
    assert len(bot.albums) == 1


@pytest.mark.parametrize("status", ["not_found", "unreadable"])
async def test_no_group_or_unreadable_initial_silent(setup, status):
    engine, _, bot, ocr = setup
    ocr.result = Extraction(status=status, rows=[])
    await engine.run([publication()], 1)
    assert bot.albums == [] and bot.sent == []


async def test_unreadable_edit_invalidates_previous(setup):
    engine, _, bot, ocr = setup
    await engine.run([publication()], 1)
    ocr.result = Extraction(status="unreadable", rows=[])
    await engine.run([publication(revision="v2")], 1)
    assert "не удаётся подтвердить" in bot.captions[0][1]
    assert len(bot.albums) == 1


async def test_long_changes_continue_in_text_message(setup):
    engine, _, bot, ocr = setup
    rows = [
        make_row(lesson=str(i % 12 + 1), change_date=f"2026-10-{i // 12 + 21}") for i in range(30)
    ]
    ocr.result = Extraction(status="found", rows=rows)
    await engine.run([publication()], 1)
    assert len(bot.albums[0][1]) <= 1000
    assert bot.sent


async def test_lost_album_response_requires_resolve(setup):
    engine, store, bot, _ = setup

    async def crash(*_):
        raise RuntimeError("Simulated lost HTTP response")

    bot.send_album = crash
    with pytest.raises(RuntimeError):
        await engine.run([publication(images=("test.jpg", "second.jpg"))], 1)
    restored = store.saved
    assert restored["pending"]["kind"] == "album"
    assert restored["cursor"] == 0
    fresh = Bot()
    restarted = Engine(restored, store, fresh, OCR())
    with pytest.raises(DeliveryUncertain):
        await restarted.recover()
    # Operator found the delivered album and gave the ID of its first photo.
    restarted.complete_pending(55)
    await restarted.save()
    assert restarted.state["posts"]["post:1"]["album"]["ids"] == [55, 56]
    await restarted.run([publication(images=("test.jpg", "second.jpg"))], 1)
    assert fresh.albums == []
    assert store.saved["pending"] is None


async def test_caption_edit_intent_recovered_idempotently(setup):
    engine, store, bot, _ = setup
    await engine.run([publication()], 1)
    engine.state["pending"] = {
        "kind": "edit",
        "post": "post:1",
        "page": 0,
        "message_id": 100,
        "text": "Updated",
        "caption": True,
    }
    await engine.recover()
    assert bot.captions == [(100, "Updated")]
    assert store.saved["pending"] is None


async def test_ocr_failure_does_not_advance_cursor(setup):
    engine, store, _, ocr = setup

    async def fail(_):
        raise RuntimeError("OCR unavailable")

    ocr.extract = fail
    with pytest.raises(RuntimeError):
        await engine.run([publication()], 10)
    assert store.saved["cursor"] == 0


async def test_rejected_album_falls_back_to_text(setup):
    engine, store, bot, _ = setup

    async def rejected(*_):
        raise DeliveryRejected("400")

    bot.send_album = rejected
    await engine.run([publication()], 1)
    assert len(bot.sent) == 1 and "Математика" in bot.sent[0]
    assert store.saved["pending"] is None
    assert store.saved["cursor"] == 1


async def test_explicit_telegram_rejection_can_retry_without_manual_recovery(setup):
    engine, store, bot, _ = setup

    async def rejected(*_):
        raise DeliveryRejected("403")

    bot.send_album = rejected
    bot.send = rejected
    with pytest.raises(DeliveryRejected):
        await engine.run([publication()], 1)
    assert store.saved["pending"] is None
    assert store.saved["cursor"] == 0
