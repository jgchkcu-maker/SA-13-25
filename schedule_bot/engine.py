import hashlib
import logging

from .formatting import format_cards
from .models import accepted_rows, digest, row_key
from .telegram import DeliveryRejected

log = logging.getLogger(__name__)


class DeliveryUncertain(RuntimeError):
    pass


class Engine:
    def __init__(self, state: dict, store, bot, recognizer):
        self.state, self.store, self.bot, self.recognizer = state, store, bot, recognizer

    async def save(self):
        await self.store.save(self.state)

    def complete_pending(self, message_id: int | list[int]):
        pending = self.state["pending"]
        post = self.state["posts"][pending["post"]]
        if pending["kind"] == "photos":
            ids = message_id if isinstance(message_id, list) else [message_id]
            post["photos"] = {"hash": pending["hash"], "ids": ids}
            self.state["pending"] = None
            return
        page = {"id": message_id, "text": pending["text"]}
        if pending["page"] < len(post["pages"]):
            post["pages"][pending["page"]] = page
        else:
            post["pages"].append(page)
        self.state["pending"] = None

    async def recover(self):
        pending = self.state["pending"]
        if not pending:
            return
        if pending["kind"] in ("send", "photos"):
            what = "фото публикации" if pending["kind"] == "photos" else "сообщения"
            raise DeliveryUncertain(
                f"Незавершённая отправка {what}. Проверьте группу и выполните resolve; "
                "автоповтор отключён, чтобы исключить дубль."
            )
        message_id = await self.bot.edit(pending["message_id"], pending["text"])
        self.complete_pending(message_id)
        await self.save()

    async def sync_pages(self, key: str, texts: list[str]):
        post = self.state["posts"][key]
        # Retain and overwrite old page slots, so a shorter revised album leaves no stale pages.
        for index in range(max(len(texts), len(post["pages"]))):
            text = (
                texts[index]
                if index < len(texts)
                else (
                    "<b>Уточнение расписания · СА-13-25</b>\n\n"
                    "Эта часть сообщения больше не актуальна. "
                    "Актуальные данные — в первой части публикации бота."
                )
            )
            old = post["pages"][index] if index < len(post["pages"]) else None
            if old and old["text"] == text:
                continue
            self.state["pending"] = {
                "kind": "edit" if old else "send",
                "post": key,
                "page": index,
                "message_id": old["id"] if old else None,
                "text": text,
            }
            # Intent must be durably committed BEFORE the Telegram side effect.
            await self.save()
            try:
                message_id = (
                    await self.bot.edit(old["id"], text) if old else await self.bot.send(text)
                )
            except DeliveryRejected:
                # An explicit refusal is safe to retry after fixing permissions/configuration.
                self.state["pending"] = None
                await self.save()
                raise
            self.complete_pending(message_id)
            await self.save()

    async def sync_photos(self, publication):
        post = self.state["posts"][publication.key]
        if not post["pages"] or not publication.images:
            return
        photo_hash = digest(
            [hashlib.sha256(path.read_bytes()).hexdigest() for path in publication.images]
        )
        sent = post.get("photos")
        if sent and sent["hash"] == photo_hash:
            return
        caption = "<b>Обновлённые фото публикации</b>" if sent else "<b>Фото публикации</b>"
        self.state["pending"] = {"kind": "photos", "post": publication.key, "hash": photo_hash}
        await self.save()
        try:
            ids = await self.bot.send_photos(publication.images, caption, post["pages"][0]["id"])
        except DeliveryRejected:
            # Text is the notification itself; photos are a supplement and must not block it.
            log.warning(
                "Telegram отклонил фото публикации %s; оставлен только текст", publication.key
            )
            ids = []
        self.complete_pending(ids)
        await self.save()

    async def process(self, publication):
        old = self.state["posts"].get(publication.key)
        revised = bool(old and old["pages"])
        if publication.images:
            result = await self.recognizer.extract(publication)
            rows = accepted_rows(result, len(publication.images))
            status = result.status
            if status == "found" and not rows:
                status = "unreadable"
        else:
            # Source is photographic; do not invent schedule rows from text-only posts.
            rows, status = [], "not_found"
        if old is None:
            old = {"ids": publication.ids, "pages": [], "fingerprint": ""}
            self.state["posts"][publication.key] = old
        # Keep fingerprint uncommitted until all Telegram edits/sends finish, so interrupted
        # multi-page work is resumed. No cursor advancement on a failed publication.
        old.update(ids=publication.ids, url=publication.url)
        if rows:
            seen = set(self.state["seen"])
            visible_rows = rows if revised else [r for r in rows if row_key(r) not in seen]
            if visible_rows:
                await self.sync_pages(
                    publication.key, format_cards(visible_rows, publication.url, revised=revised)
                )
            self.state["seen"] = sorted(seen | {row_key(r) for r in rows})
        elif revised:
            await self.sync_pages(
                publication.key, format_cards([], publication.url, revised=True, uncertain=True)
            )
        await self.sync_photos(publication)
        old.update(
            fingerprint=publication.metadata_fingerprint,
            revision=publication.fingerprint,
            status=status,
        )
        await self.save()
        log.info("Публикация %s: %s, принято строк: %d", publication.key, status, len(rows))

    async def run(self, publications, cursor):
        await self.recover()
        for publication in publications:
            await self.process(publication)
        self.state.update(cursor=cursor, initialized=True)
        await self.save()
