from datetime import UTC, datetime
from types import SimpleNamespace as NS

import httpx
import pytest

from schedule_bot.config import Config
from schedule_bot.public_source import Source, channel_username, parse_page
from schedule_bot.state import empty_state


def html(post_id=10, date="2026-10-18T09:00:00+00:00", caption="text", photos=2, before=None):
    images = "".join(
        f'<a class="tgme_widget_message_photo_wrap" '
        f'href="https://t.me/test_channel/{post_id + i}?single" '
        f"style=\"background-image:url('https://cdn4.telesco.pe/file/{post_id}-{i}.jpg')\"></a>"
        for i in range(photos)
    )
    more = f'<a class="tme_messages_more" data-before="{before}"></a>' if before else ""
    return (
        f'<div class="tgme_widget_message" data-post="test_channel/{post_id}">'
        f'<div class="tgme_widget_message_text">{caption}</div>{images}'
        f'<a class="tgme_widget_message_date"><time datetime="{date}"></time></a>'
        f"</div>{more}"
    )


def config():
    return NS(
        source="@test_channel", start_at=datetime(2026, 10, 18, tzinfo=UTC), settle_seconds=120
    )


def test_full_album_and_caption():
    posts, before = parse_page(html(photos=4, caption="<b>caption</b>", before=10), "test_channel")
    assert before == 10
    assert posts[0].ids == [10, 11, 12, 13]
    assert len(posts[0].photos) == 4
    assert posts[0].caption == "caption"


@pytest.mark.parametrize(
    "source", ["@test_channel", "https://t.me/s/test_channel", "https://t.me/test_channel/"]
)
def test_channel_formats(source):
    assert channel_username(source) == "test_channel"


@pytest.mark.parametrize("source", ["-100123", "https://evil.test/channel", "+private"])
def test_private_or_invalid_source_rejected(source):
    with pytest.raises(ValueError):
        channel_username(source)


def test_changed_html_and_external_images_fail_closed():
    with pytest.raises(RuntimeError):
        parse_page("<html>Login required</html>", "test_channel")
    with pytest.raises(RuntimeError):
        parse_page(html().replace("https://cdn4.telesco.pe", "http://localhost"), "test_channel")
    with pytest.raises(RuntimeError):
        parse_page(html().replace("test_channel/10", "other_channel/10"), "test_channel")


async def test_pagination_downloads_every_photo_and_excludes_old_posts(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.host.endswith("telesco.pe"):
            return httpx.Response(200, content=b"\xff\xd8\xffimage" + req.url.path.encode())
        if req.url.params.get("before") == "20":
            return httpx.Response(200, text=html(10) + html(5, date="2026-10-17T23:59:59Z"))
        return httpx.Response(200, text=html(20, date="2026-10-19T12:00:00Z", before=20))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            result, cursor = await source.collect(empty_state(), tmp_path)
    assert [p.ids for p in result] == [[10, 11], [20, 21]]
    assert cursor == 21
    assert len([c for c in calls if "telesco.pe" in c]) == 4
    assert all(p.images[0].read_bytes().startswith(b"\xff\xd8\xff") for p in result)


async def test_content_edits_and_signed_url_rotation(tmp_path):
    version = {"image": b"same", "url": "first", "caption": "same"}

    def handler(req):
        if req.url.host.endswith("telesco.pe"):
            return httpx.Response(200, content=b"\xff\xd8\xff" + version["image"])
        return httpx.Response(
            200, text=html(caption=version["caption"]).replace("/file/", f"/file/{version['url']}/")
        )

    state = empty_state()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            first, _ = await source.collect(state, tmp_path)
            p = first[0]
            state["posts"][p.key] = {"ids": p.ids, "revision": p.fingerprint}
            state["cursor"] = 11
            state["initialized"] = True
            version["url"] = "rotated"
            assert (await source.collect(state, tmp_path))[0] == []
            version["image"] = b"edited photo"
            changed, _ = await source.collect(state, tmp_path)
            assert changed[0].fingerprint != p.fingerprint
            version["image"] = b"same"
            version["caption"] = "edited caption"
            assert (await source.collect(state, tmp_path))[0]


async def test_unchanged_post_skipped_and_old_album_key_preserved(tmp_path):
    def handler(req):
        if req.url.host.endswith("telesco.pe"):
            return httpx.Response(200, content=b"\xff\xd8\xffimage")
        return httpx.Response(200, text=html())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            state = empty_state()
            first, _ = await source.collect(state, tmp_path)
            state["posts"]["album:old"] = {
                "ids": [10, 11],
                "revision": first[0].fingerprint,
                "retry_after": 1,
            }
            # Legacy retry markers no longer trigger another paid recognition.
            assert (await source.collect(state, tmp_path))[0] == []
            state["posts"]["album:old"]["revision"] = "edited"
            edited, _ = await source.collect(state, tmp_path)
            assert edited[0].key == "album:old"


async def test_unsupported_and_fake_photos_do_not_advance_state(tmp_path):
    mode = {"value": "unsupported"}

    def handler(req):
        if req.url.host.endswith("telesco.pe"):
            return httpx.Response(200, content=b"<html>not an image</html>")
        page = html()
        if mode["value"] == "unsupported":
            page = page.replace(
                '<a class="tgme_widget_message_date"',
                '<div class="message_media_not_supported"></div>'
                '<a class="tgme_widget_message_date"',
            )
        return httpx.Response(200, text=page)

    state = empty_state()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            with pytest.raises(RuntimeError, match="10"):
                await source.collect(state, tmp_path)
            mode["value"] = "fake"
            with pytest.raises(RuntimeError, match="Telegram"):
                await source.collect(state, tmp_path)
    assert state["cursor"] == 0


async def test_new_album_settles_without_downloading(tmp_path):
    def handler(req):
        return httpx.Response(200, text=html(date=datetime.now(UTC).isoformat()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            posts, cursor = await source.collect(empty_state(), tmp_path)
    assert posts == []
    assert cursor == 9


async def test_pagination_loop_is_error(tmp_path):
    def handler(req):
        return httpx.Response(200, text=html(before=10))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Source(config(), client) as source:
            with pytest.raises(RuntimeError, match="Telegram"):
                await source.collect(empty_state(), tmp_path)


def test_config_does_not_require_account_credentials(monkeypatch):
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(name, raising=False)
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("START_DATE", "2026-10-18")
    loaded = Config.load()
    assert loaded.start_at.isoformat() == "2026-10-18T00:00:00+07:00"
    assert loaded.start_at.astimezone(UTC) == datetime(2026, 10, 17, 17, tzinfo=UTC)
