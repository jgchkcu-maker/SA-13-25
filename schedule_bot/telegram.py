import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl.types import Channel

from .models import digest


class DeliveryRejected(RuntimeError):
    """Telegram explicitly refused a request; no message was delivered."""


def has_image(message) -> bool:
    return bool(
        message.photo
        or (message.document and (message.document.mime_type or "").startswith("image/"))
    )


def fingerprint(messages) -> str:
    return digest(
        [
            {
                "id": m.id,
                "text": m.message or "",
                "edit": m.edit_date.isoformat() if m.edit_date else None,
                "photo": str(m.photo.id) if m.photo else None,
                "document": str(m.document.id) if m.document else None,
            }
            for m in messages
        ]
    )


@dataclass
class Publication:
    key: str
    ids: list[int]
    fingerprint: str
    metadata_fingerprint: str
    url: str
    caption: str
    published_at: str
    images: list[Path]


class Source:
    def __init__(self, config):
        self.config = config
        self.client = TelegramClient(
            StringSession(config.session),
            config.api_id,
            config.api_hash,
            flood_sleep_threshold=60,
            request_retries=3,
        )
        self.channel = None

    async def __aenter__(self):
        await self.client.connect()
        try:
            if not await self.client.is_user_authorized():
                raise RuntimeError("Telegram-сессия отозвана. Создайте TELEGRAM_SESSION заново")
            source = self.config.source.strip().rstrip("/")
            source = source.replace("https://t.me/", "").replace("http://t.me/", "")
            if source.startswith("+") or source.startswith("joinchat/"):
                raise ValueError("Используйте ID приватного канала из команды dialogs")
            if source.lstrip("-").isdigit():
                # StringSession does not retain entity access hashes. Populate in-memory cache.
                await self.client.get_dialogs(limit=None)
                source = int(source)
            self.channel = await self.client.get_entity(source)
            if not isinstance(self.channel, Channel):
                raise ValueError("Источник должен быть Telegram-каналом")
        except BaseException:
            await self.client.disconnect()
            raise
        return self

    async def __aexit__(self, *args):
        await self.client.disconnect()

    @property
    def channel_id(self):
        return str(utils.get_peer_id(self.channel))

    def url(self, message_id: int):
        if self.channel.username:
            return f"https://t.me/{self.channel.username}/{message_id}"
        return f"https://t.me/c/{self.channel.id}/{message_id}"

    async def collect(self, state: dict, directory: Path) -> tuple[list[Publication], int]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.config.lookback_days)
        messages = {}
        bootstrap_cursor = 0
        history_cursor = state["cursor"]
        # Fetch every new post, not just the latest N messages. Cursor advances only after success.
        async for message in self.client.iter_messages(
            self.channel, min_id=state["cursor"] if state["initialized"] else 0
        ):
            history_cursor = max(history_cursor, message.id)
            if not state["initialized"] and not bootstrap_cursor:
                bootstrap_cursor = message.id
            if not state["initialized"] and message.date < cutoff:
                break
            if getattr(message, "action", None) is not None:
                continue
            messages[message.id] = message
        # Revisit ALL tracked IDs, including old publications where the target was absent.
        known = sorted({i for p in state["posts"].values() for i in p["ids"]})
        for start in range(0, len(known), 100):
            for message in await self.client.get_messages(
                self.channel, ids=known[start : start + 100]
            ):
                if message and getattr(message, "date", None):
                    messages[message.id] = message
        # Restore siblings if an initial scan/cursor happened to cut an album boundary.
        sibling_ids = set()
        album_ids = {m.grouped_id for m in messages.values() if m.grouped_id}
        for message in messages.values():
            if message.grouped_id:
                sibling_ids.update(range(max(1, message.id - 10), message.id + 11))
        missing = sorted(sibling_ids - messages.keys())
        for start in range(0, len(missing), 100):
            for message in await self.client.get_messages(
                self.channel, ids=missing[start : start + 100]
            ):
                if message and getattr(message, "date", None) and message.grouped_id in album_ids:
                    messages[message.id] = message
        groups = {}
        for message in sorted(messages.values(), key=lambda m: m.id):
            key = f"album:{message.grouped_id}" if message.grouped_id else f"post:{message.id}"
            groups.setdefault(key, []).append(message)
        publications, deferred = [], []
        cursor = max([state["cursor"], bootstrap_cursor, history_cursor, *messages.keys()])
        for key, members in groups.items():
            latest = max(m.edit_date or m.date for m in members)
            if (now - latest).total_seconds() < self.config.settle_seconds:
                deferred.extend(m.id for m in members)
                continue
            ids = [m.id for m in members]
            old = state["posts"].get(key)
            signature = fingerprint(members)
            # No downloads/OCR for unchanged posts, except deferred recognition retries.
            retry = old and old.get("retry_after") and now.timestamp() >= old["retry_after"]
            if old and old["fingerprint"] == signature and not retry:
                continue
            images = []
            for m in members:
                if has_image(m):
                    path = await self.client.download_media(m, file=str(directory / str(m.id)))
                    if not path:
                        raise RuntimeError(f"Не удалось скачать изображение публикации {m.id}")
                    images.append(Path(path))
            # Include bytes in revision hash: even a caption-only edit reuses content reliably.
            signature = digest(
                [signature, *[hashlib.sha256(p.read_bytes()).hexdigest() for p in images]]
            )
            publications.append(
                Publication(
                    key,
                    ids,
                    signature,
                    fingerprint(members),
                    self.url(ids[0]),
                    "\n".join(m.message for m in members if m.message),
                    members[0].date.isoformat(),
                    images,
                )
            )
        if deferred:
            cursor = min(cursor, min(deferred) - 1)
        return publications, max(state["cursor"], cursor)


class BotAPI:
    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str, thread_id=None):
        self.client, self.token, self.chat_id, self.thread_id = client, token, chat_id, thread_id

    async def call(self, method: str, payload: dict):
        for attempt in range(4):
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/{method}", json=payload
                )
                data = response.json()
            except (httpx.HTTPError, ValueError):
                # A send timeout can occur AFTER Telegram accepted the message. Never retry it.
                raise RuntimeError(
                    "Неоднозначный ответ Telegram; проверьте pending в состоянии"
                ) from None
            if data.get("ok"):
                return data["result"]
            description = data.get("description", "")
            if method == "editMessageText" and "message is not modified" in description.lower():
                return {"message_id": payload["message_id"]}
            if data.get("error_code") == 429 and attempt < 3:
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                if wait <= 60:
                    await asyncio.sleep(wait + 1)
                    continue
            error_type = (
                DeliveryRejected
                if data.get("error_code") in (400, 401, 403, 404, 413, 429)
                else RuntimeError
            )
            raise error_type(
                f"Telegram Bot API: ошибка {data.get('error_code', response.status_code)}"
            )
        raise RuntimeError("Telegram Bot API: исчерпаны попытки")

    async def send(self, text: str) -> int:
        payload = self.payload(text)
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
        return (await self.call("sendMessage", payload))["message_id"]

    async def edit(self, message_id: int, text: str) -> int:
        return (
            await self.call("editMessageText", {**self.payload(text), "message_id": message_id})
        )["message_id"]

    def payload(self, text):
        return {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": False,
        }
