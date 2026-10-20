import hashlib
import logging

from .formatting import STALE_PART, format_cards, outdated_notice
from .models import accepted_rows, digest, row_key
from .telegram import DeliveryRejected

log = logging.getLogger(__name__)


class DeliveryUncertain(RuntimeError):
    pass


class Engine:
    """Delivers each publication as one album: the source photos with the changes as caption.

    State per post: `pages` are the bot's messages (page 0 is the album caption when `album`
    is set, the rest are text continuations); `album` holds the photo message IDs and hash.
    """

    def __init__(self, state: dict, store, bot, recognizer):
        self.state, self.store, self.bot, self.recognizer = state, store, bot, recognizer

    async def save(self):
        await self.store.save(self.state)

    def complete_pending(self, message_id: int | list[int]):
        pending = self.state["pending"]
        post = self.state["posts"][pending["post"]]
        if pending["kind"] == "album":
            # Manual recovery gives only the first ID; album messages are numbered in a row.
            ids = (
                message_id
                if isinstance(message_id, list)
                else [message_id + i for i in range(pending["count"])]
            )
            post["album"] = {"ids": ids, "hash": pending["hash"], "count": pending["count"]}
            post["pages"] = [{"id": ids[0], "text": pending["text"], "caption": True}]
        else:
            page = {
                "id": message_id,
                "text": pending["text"],
                "caption": pending.get("caption", False),
            }
            if pending["page"] < len(post["pages"]):
                post["pages"][pending["page"]] = page
            else:
                post["pages"].append(page)
        self.state["pending"] = None

    async def recover(self):
        pending = self.state["pending"]
        if not pending:
            return
        if pending["kind"] in ("send", "album"):
            what = "альбома" if pending["kind"] == "album" else "сообщения"
            raise DeliveryUncertain(
                f"Незавершённая отправка {what}. Проверьте группу и выполните resolve; "
                "автоповтор отключён, чтобы исключить дубль."
            )
        if pending["kind"] in ("media", "photos"):
            # Photo replacement is redone when the publication is processed again
            # (its revision is committed only after delivery); "photos" is a legacy reply album.
            self.state["pending"] = None
            await self.save()
            return
        edit = self.bot.edit_caption if pending.get("caption") else self.bot.edit
        message_id = await edit(pending["message_id"], pending["text"])
        self.complete_pending(message_id)
        await self.save()

    async def write_page(self, key: str, index: int, text: str):
        post = self.state["posts"][key]
        old = post["pages"][index] if index < len(post["pages"]) else None
        if old and old["text"] == text:
            return
        caption = bool(old and old.get("caption"))
        self.state["pending"] = {
            "kind": "edit" if old else "send",
            "post": key,
            "page": index,
            "message_id": old["id"] if old else None,
            "text": text,
            "caption": caption,
        }
        # Intent must be durably committed BEFORE the Telegram side effect.
        await self.save()
        try:
            if not old:
                message_id = await self.bot.send(text)
            elif caption:
                message_id = await self.bot.edit_caption(old["id"], text)
            else:
                message_id = await self.bot.edit(old["id"], text)
        except DeliveryRejected:
            # An explicit refusal is safe to retry after fixing permissions/configuration.
            self.state["pending"] = None
            await self.save()
            raise
        self.complete_pending(message_id)
        await self.save()

    async def sync_pages(self, key: str, texts: list[str]):
        post = self.state["posts"][key]
        # Retain and overwrite old page slots, so a shorter revision leaves no stale pages.
        for index in range(max(len(texts), len(post["pages"]))):
            await self.write_page(key, index, texts[index] if index < len(texts) else STALE_PART)

    async def retire(self, publication):
        """Mark the previous notification outdated before a new album replaces it."""
        post = self.state["posts"][publication.key]
        for index in range(len(post["pages"])):
            text = outdated_notice(publication.url) if index == 0 else STALE_PART
            try:
                await self.write_page(publication.key, index, text)
            except DeliveryRejected:
                log.warning("Не удалось пометить устаревшее сообщение %s", publication.key)
        post.update(pages=[], retired=True)
        post.pop("album", None)
        post.pop("photos", None)
        await self.save()

    async def send_album(self, publication, caption: str, photo_hash: str):
        count = len(publication.images)
        self.state["pending"] = {
            "kind": "album",
            "post": publication.key,
            "hash": photo_hash,
            "count": count,
            "text": caption,
        }
        await self.save()
        try:
            ids = await self.bot.send_album(publication.images, caption)
        except DeliveryRejected:
            # E.g. media are forbidden in the group: the changes still go out as text.
            log.warning("Telegram отклонил альбом %s; отправляется текст", publication.key)
            self.state["pending"] = None
            await self.save()
            return
        self.complete_pending(ids)
        await self.save()

    async def replace_photos(self, publication, caption: str, photo_hash: str) -> bool:
        post = self.state["posts"][publication.key]
        album = post["album"]
        self.state["pending"] = {"kind": "media", "post": publication.key}
        await self.save()
        try:
            # Same photo count is checked by the caller, so the lengths match.
            pairs = zip(album["ids"], publication.images, strict=True)
            for index, (message_id, path) in enumerate(pairs):
                await self.bot.edit_photo(message_id, path, caption if index == 0 else None)
        except DeliveryRejected:
            self.state["pending"] = None
            await self.save()
            return False
        album["hash"] = photo_hash
        post["pages"][0]["text"] = caption
        self.state["pending"] = None
        await self.save()
        return True

    async def deliver(self, publication, texts: list[str]):
        key = publication.key
        post = self.state["posts"][key]
        album = post.get("album")
        if not publication.images:
            if album:
                await self.retire(publication)
            await self.sync_pages(key, texts)
            return
        photo_hash = digest(
            [hashlib.sha256(path.read_bytes()).hexdigest() for path in publication.images]
        )
        # Telegram cannot add or remove photos in a sent album: a different photo count
        # (or a text-only notification from an older version) gets a new album.
        if post["pages"] and not (album and album["count"] == len(publication.images)):
            await self.retire(publication)
        elif album and album["hash"] != photo_hash:
            if not await self.replace_photos(publication, texts[0], photo_hash):
                await self.retire(publication)
        if not post["pages"]:
            await self.send_album(publication, texts[0], photo_hash)
        await self.sync_pages(key, texts)

    async def process(self, publication):
        old = self.state["posts"].get(publication.key)
        revised = bool(old and (old["pages"] or old.get("retired")))
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
                await self.deliver(
                    publication, format_cards(visible_rows, publication.url, revised=revised)
                )
            self.state["seen"] = sorted(seen | {row_key(r) for r in rows})
        elif revised:
            await self.deliver(
                publication, format_cards([], publication.url, revised=True, uncertain=True)
            )
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
