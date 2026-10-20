"""Read public Telegram channel HTML, without an account, cookies or session."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .models import GROUP_MATCH_VERSION, digest
from .telegram import Publication

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 25 * 1024 * 1024


@dataclass
class WebPost:
    id: int
    ids: list[int]
    date: datetime
    caption: str
    photos: list[str]
    unsupported: bool


def channel_username(source: str) -> str:
    value = source.strip().rstrip("/")
    value = re.sub(r"^https?://t\.me/(?:s/)?", "", value).lstrip("@")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", value):
        raise ValueError("TELEGRAM_SOURCE: нужен @username публичного канала")
    return value.lower()


def media_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(
            host == domain or host.endswith("." + domain)
            for domain in ("telesco.pe", "telegram.org", "t.me", "telegram-cdn.org")
        )
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise RuntimeError("Неожиданный адрес изображения Telegram")
    return url


def parse_page(html: str, username: str) -> tuple[list[WebPost], int | None]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        match = re.fullmatch(re.escape(username) + r"/(\d+)", node["data-post"], re.I)
        if not match:
            raise RuntimeError("Telegram вернул публикацию другого канала")
        post_id = int(match[1])
        clock = node.select_one(".tgme_widget_message_date time[datetime]")
        if clock is None:
            raise RuntimeError("Не удалось прочитать дату публикации Telegram")
        date = datetime.fromisoformat(clock["datetime"].replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise RuntimeError("Дата публикации без часового пояса")
        photos, ids = [], {post_id}
        for photo in node.select("a.tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            image = re.search(r"background-image:\s*url\(['\"]?([^'\")]+)", style)
            if not image:
                raise RuntimeError("Telegram изменил разметку фотографии")
            photos.append(media_url(image[1]))
            href = photo.get("href", "")
            sibling = re.search(r"/" + re.escape(username) + r"/(\d+)(?:[/?#]|$)", href, re.I)
            if sibling:
                ids.add(int(sibling[1]))
        text = node.select_one(".tgme_widget_message_text")
        unsupported = bool(
            node.select(
                ".message_media_not_supported, .tgme_widget_message_document_wrap, "
                ".tgme_widget_message_video_player, .tgme_widget_message_error"
            )
        )
        posts.append(
            WebPost(
                post_id,
                sorted(ids),
                date,
                text.get_text("\n", strip=True) if text else "",
                photos,
                unsupported,
            )
        )
    older = soup.select_one("a.tme_messages_more[data-before]")
    before = int(older["data-before"]) if older else None
    if not posts and not soup.select_one(".tgme_channel_info"):
        raise RuntimeError("Нет публичной ленты Telegram: доступ ограничен или разметка изменилась")
    return posts, before


class Source:
    def __init__(self, config, client: httpx.AsyncClient | None = None):
        self.config = config
        self.username = channel_username(config.source)
        self.client = client
        self.owns_client = client is None

    @property
    def channel_id(self):
        return "public:@" + self.username

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=60,
                follow_redirects=False,
                headers={"User-Agent": "AKScheduleBot/2.0 (public channel reader)"},
            )
        return self

    async def __aexit__(self, *args):
        if self.owns_client and self.client:
            await self.client.aclose()

    async def fetch(self, url: str, *, image=False):
        for attempt in range(4):
            try:
                async with self.client.stream("GET", url) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 3:
                            await asyncio.sleep(2**attempt)
                            continue
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Публичный источник Telegram: HTTP {response.status_code}"
                        )
                    limit = MAX_IMAGE_BYTES if image else 8 * 1024 * 1024
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > limit:
                            raise RuntimeError("Превышен лимит размера публикации Telegram")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if image and not (
                        content.startswith(b"\xff\xd8\xff")
                        or content.startswith(b"\x89PNG\r\n\x1a\n")
                        or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
                    ):
                        raise RuntimeError("Вместо фотографии Telegram вернул другой файл")
                    return content
            except httpx.HTTPError:
                if attempt == 3:
                    raise RuntimeError("Публичный источник Telegram недоступен") from None
                await asyncio.sleep(2**attempt)
        raise RuntimeError("Публичный источник Telegram: исчерпаны попытки")

    async def collect(self, state: dict, directory: Path) -> tuple[list[Publication], int]:
        # Rescan the complete requested window on every run: web HTML has no reliable
        # edit timestamp. Never use expiring CDN URLs as content revision identifiers.
        posts, before, visited = {}, None, set()
        while True:
            url = f"https://t.me/s/{self.username}"
            if before:
                url += f"?before={before}"
            page, next_before = parse_page((await self.fetch(url)).decode("utf-8"), self.username)
            for post in page:
                if post.date >= self.config.start_at:
                    previous = posts.get(post.id)
                    if previous and (
                        previous.ids != post.ids
                        or previous.caption != post.caption
                        or previous.date != post.date
                        or len(previous.photos) != len(post.photos)
                    ):
                        # A live edit during pagination is retried next run, not mixed.
                        raise RuntimeError(
                            "Публикация изменилась во время загрузки; повторите проверку"
                        )
                    posts[post.id] = post
            if not page or min(p.date for p in page) < self.config.start_at or not next_before:
                break
            if next_before in visited or (before and next_before >= before):
                raise RuntimeError("Telegram не продвинул страницу истории; сбор остановлен")
            visited.add(next_before)
            before = next_before
        publications, deferred = [], []
        now = datetime.now(UTC)
        cursor = max([state["cursor"], *[max(p.ids) for p in posts.values()]])
        count_images = 0
        for post in sorted(posts.values(), key=lambda p: p.id):
            if (now - post.date).total_seconds() < self.config.settle_seconds:
                deferred.extend(post.ids)
                continue
            if post.unsupported:
                raise RuntimeError(
                    f"Публикация {post.id}: вложение недоступно публично; курсор не продвинут"
                )
            key = f"post:{post.id}"
            # Retain existing notification slots when upgrading from the account reader.
            for old_key, old_post in state["posts"].items():
                if set(post.ids).intersection(old_post["ids"]):
                    key = old_key
                    break
            old = state["posts"].get(key)
            images, hashes = [], []
            for index, url in enumerate(post.photos):
                content = await self.fetch(url, image=True)
                path = directory / f"{post.id}-{index}.jpg"
                if content.startswith(b"\x89PNG"):
                    path = path.with_suffix(".png")
                elif content.startswith(b"RIFF"):
                    path = path.with_suffix(".webp")
                path.write_bytes(content)
                images.append(path)
                hashes.append(hashlib.sha256(content).hexdigest())
            count_images += len(images)
            # Recheck earlier OCR decisions once when group matching rules change.
            signature = digest(
                [post.ids, post.date.isoformat(), post.caption, hashes, GROUP_MATCH_VERSION]
            )
            retry = old and old.get("retry_after") and now.timestamp() >= old["retry_after"]
            if old and old.get("revision") == signature and not retry:
                continue
            publications.append(
                Publication(
                    key,
                    post.ids,
                    signature,
                    signature,
                    f"https://t.me/{self.username}/{post.id}",
                    post.caption,
                    post.date.isoformat(),
                    images,
                )
            )
        if deferred:
            cursor = min(cursor, min(deferred) - 1)
        log.info(
            "Публичная лента с %s: публикаций %d, фотографий %d, новых/изменённых %d",
            self.config.start_at.date(),
            len(posts),
            count_images,
            len(publications),
        )
        return publications, max(state["cursor"], cursor)
