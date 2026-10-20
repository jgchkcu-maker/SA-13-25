from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from schedule_bot.state import empty_state
from schedule_bot.telegram import Source, fingerprint


def msg(i, *, album=None, days=0, text="", edit=False):
    return NS(
        id=i,
        grouped_id=album,
        date=datetime.now(UTC) - timedelta(days=days, minutes=10),
        edit_date=datetime.now(UTC) - timedelta(minutes=5) if edit else None,
        message=text,
        photo=NS(id=i * 10),
        document=None,
    )


class Client:
    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.downloaded = []

    async def iter_messages(self, channel, min_id=0):
        for m in sorted(self.messages.values(), key=lambda m: -m.id):
            if m.id > min_id:
                yield m

    async def get_messages(self, channel, ids):
        return [self.messages.get(i) for i in ids]

    async def download_media(self, message, file):
        self.downloaded.append(message.id)
        path = Path(file + ".jpg")
        path.write_bytes(f"image{message.id}".encode())
        return str(path)


def source(messages):
    instance = Source.__new__(Source)
    instance.config = NS(lookback_days=3, settle_seconds=120)
    instance.channel = NS(id=123, username="test")
    instance.client = Client(messages)
    return instance


async def test_complete_album_when_cursor_splits_it(tmp_path):
    s = source([msg(10, album=42), msg(11, album=42), msg(12, album=42)])
    state = empty_state() | {"initialized": True, "cursor": 11}
    publications, cursor = await s.collect(state, tmp_path)
    assert len(publications) == 1
    assert publications[0].ids == [10, 11, 12]
    assert len(publications[0].images) == 3
    assert cursor == 12


async def test_all_new_posts_not_just_last_page(tmp_path):
    s = source([msg(i) for i in range(1, 241)])
    pubs, cursor = await s.collect(empty_state() | {"initialized": True}, tmp_path)
    assert len(pubs) == 240
    assert cursor == 240


async def test_revisits_old_post_that_was_not_found(tmp_path):
    original = msg(1, days=300)
    s = source([msg(1, days=300, text="Corrected", edit=True)])
    state = empty_state() | {"initialized": True, "cursor": 500}
    state["posts"]["post:1"] = {
        "ids": [1],
        "fingerprint": fingerprint([original]),
        "status": "not_found",
    }
    pubs, cursor = await s.collect(state, tmp_path)
    assert len(pubs) == 1
    assert cursor == 500


async def test_unchanged_skips_download_and_ocr(tmp_path):
    original = msg(1)
    s = source([original])
    state = empty_state() | {"initialized": True, "cursor": 1}
    state["posts"]["post:1"] = {"ids": [1], "fingerprint": fingerprint([original])}
    pubs, _ = await s.collect(state, tmp_path)
    assert pubs == []
    assert s.client.downloaded == []


async def test_recent_album_waits_without_cursor_skip(tmp_path):
    recent = msg(2, album=42)
    recent.date = datetime.now(UTC)
    s = source([msg(1), recent])
    pubs, cursor = await s.collect(empty_state(), tmp_path)
    assert [p.ids for p in pubs] == [[1]]
    assert cursor == 1


async def test_empty_initial_window_does_not_backfill_entire_channel(tmp_path):
    s = source([msg(100, days=40), msg(99, days=50)])
    pubs, cursor = await s.collect(empty_state(), tmp_path)
    assert pubs == []
    assert cursor == 100


async def test_album_neighbors_do_not_import_untracked_partial_albums(tmp_path):
    original = msg(10, album=1, days=20)
    s = source(
        [original, msg(11, album=1, days=20), *[msg(i, album=2, days=20) for i in range(20, 30)]]
    )
    state = empty_state() | {"initialized": True, "cursor": 100}
    state["posts"]["album:1"] = {"ids": [10], "fingerprint": fingerprint([original])}
    pubs, cursor = await s.collect(state, tmp_path)
    assert [p.ids for p in pubs] == [[10, 11]]
    assert cursor == 100


async def test_service_messages_only_advance_cursor(tmp_path):
    service = msg(5)
    service.action = "pinned message"
    s = source([service])
    pubs, cursor = await s.collect(empty_state() | {"initialized": True}, tmp_path)
    assert pubs == []
    assert cursor == 5
