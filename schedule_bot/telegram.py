import asyncio
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
