import asyncio
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import httpx


class DeliveryRejected(RuntimeError):
    """Telegram explicitly refused a request; no message was delivered."""


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


def photo_file(path: Path):
    return path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"


class BotAPI:
    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str, thread_id=None):
        self.client, self.token, self.chat_id, self.thread_id = client, token, chat_id, thread_id

    async def call(self, method: str, payload: dict, files: dict | None = None):
        # Multipart requests take flat string fields; file contents are bytes, so retries reuse them.
        body = {"data": payload, "files": files} if files else {"json": payload}
        for attempt in range(4):
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/{method}", **body
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
            if method.startswith("edit") and "message is not modified" in description.lower():
                return {"message_id": int(payload["message_id"])}
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

    async def send_album(self, paths: list[Path], caption: str) -> list[int]:
        """Photos with the notification as caption of the first one; returns all message IDs."""
        ids = []
        # A media group holds 2-10 items; a lone photo in a chunk goes through sendPhoto.
        for start in range(0, len(paths), 10):
            chunk = paths[start : start + 10]
            files = {f"photo{i}": photo_file(path) for i, path in enumerate(chunk)}
            # Only the captioned first chunk notifies; a continuation chunk is silent.
            payload = {"chat_id": self.chat_id, "disable_notification": str(start > 0).lower()}
            if self.thread_id:
                payload["message_thread_id"] = str(self.thread_id)
            text = caption if start == 0 else None
            if len(chunk) == 1:
                if text:
                    payload.update(caption=text, parse_mode="HTML")
                result = await self.call(
                    "sendPhoto", {**payload, "photo": "attach://photo0"}, files
                )
                ids.append(result["message_id"])
                continue
            media = [{"type": "photo", "media": f"attach://photo{i}"} for i in range(len(chunk))]
            if text:
                media[0].update(caption=text, parse_mode="HTML")
            result = await self.call(
                "sendMediaGroup", {**payload, "media": json.dumps(media)}, files
            )
            ids.extend(message["message_id"] for message in result)
        return ids

    async def edit_caption(self, message_id: int, caption: str) -> int:
        result = await self.call(
            "editMessageCaption",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
        )
        return result["message_id"] if isinstance(result, dict) else message_id

    async def edit_photo(self, message_id: int, path: Path, caption: str | None) -> int:
        """Replace one photo of a sent album in place; caption is set on the first one only."""
        media = {"type": "photo", "media": "attach://photo0"}
        if caption is not None:
            media.update(caption=caption, parse_mode="HTML")
        result = await self.call(
            "editMessageMedia",
            {"chat_id": self.chat_id, "message_id": str(message_id), "media": json.dumps(media)},
            {"photo0": photo_file(path)},
        )
        return result["message_id"] if isinstance(result, dict) else message_id

    def payload(self, text):
        return {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": False,
        }
