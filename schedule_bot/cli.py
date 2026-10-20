import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import Config, required
from .engine import Engine
from .public_source import Source
from .recognition import Recognizer
from .state import make_store
from .telegram import BotAPI


async def execute(args):
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
        if args.command == "chat-id":
            bot = BotAPI(http, required("TELEGRAM_BOT_TOKEN"), "")
            username = (await bot.call("getMe", {}))["username"].lower()
            updates = await bot.call(
                "getUpdates",
                {
                    "timeout": 0,
                    "limit": 100,
                    "allowed_updates": ["message"],
                },
            )
            answered = 0
            for update in updates:
                message = update.get("message", {})
                command = message.get("text", "").split()
                chat = message.get("chat", {})
                if chat.get("type") not in ("group", "supergroup") or not command:
                    continue
                if command[0].lower() not in ("/chatid", f"/chatid@{username}"):
                    continue
                target = BotAPI(http, bot.token, str(chat["id"]), message.get("message_thread_id"))
                await target.send(f"<b>ID этого чата</b>\n<code>{chat['id']}</code>")
                answered += 1
            if updates:
                await bot.call(
                    "getUpdates",
                    {
                        "offset": max(u["update_id"] for u in updates) + 1,
                        "timeout": 0,
                    },
                )
            print(f"Обработано команд /chatid: {answered}. ID отправлен в соответствующий чат.")
            return
        store = make_store(http)
        if args.command == "init-state":
            await store.initialize()
            print("Состояние создано. Теперь можно запускать run.")
            return
        if args.command == "run":
            await store.ensure_initialized()
        state = await store.load()
        if args.command == "resolve":
            pending = state["pending"]
            if not pending or pending["kind"] != "send":
                raise ValueError("Нет неоднозначной отправки для подтверждения")
            engine = Engine(state, store, None, None)
            if args.message_id:
                if args.message_id < 1:
                    raise ValueError("ID должен быть положительным")
                engine.complete_pending(args.message_id)
            elif args.confirm_not_delivered:
                state["pending"] = None
            else:
                raise ValueError("Укажите --message-id ID или --confirm-not-delivered")
            await store.save(state)
            print("Состояние отправки уточнено. Следующий run продолжит обработку.")
            return
        config = Config.load()
        async with Source(config) as source:
            scope = {
                "source": source.channel_id,
                "chat": config.chat_id,
                "thread": config.thread_id,
                "group": "СА-13-25",
            }
            if state["scope"] and state["scope"] != scope:
                previous = state["scope"]
                same_destination = all(
                    previous.get(k) == scope[k] for k in ("chat", "thread", "group")
                )
                empty = not state["initialized"] and not state["posts"] and not state["pending"]
                same_channel = bool(state["posts"]) and all(
                    p.get("url", "").startswith(f"https://t.me/{source.username}/")
                    for p in state["posts"].values()
                )
                if not (same_destination and (empty or same_channel)):
                    raise RuntimeError(
                        "Источник/чат/тема изменены. Используйте отдельное состояние"
                    )
            state["scope"] = scope
            await store.save(state)
            bot = BotAPI(http, config.bot_token, config.chat_id, config.thread_id)
            recognizer = Recognizer(config.gemini_key, config.model)
            try:
                engine = Engine(state, store, bot, recognizer)
                # Resolve interrupted edits before downloading or paying for OCR.
                await engine.recover()
                with tempfile.TemporaryDirectory(prefix="ak-schedule-") as directory:
                    publications, cursor = await source.collect(state, Path(directory))
                    await engine.run(publications, cursor)
            finally:
                recognizer.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Изменения расписания СА-13-25")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Одна проверка канала")
    sub.add_parser("chat-id", help="Ответить на /chatid в группе; нужен только токен бота")
    sub.add_parser("init-state", help="Однократное создание состояния, без отправки в Telegram")
    resolve = sub.add_parser("resolve", help="Уточнить результат прерванной отправки")
    group = resolve.add_mutually_exclusive_group(required=True)
    group.add_argument("--message-id", type=int)
    group.add_argument("--confirm-not-delivered", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Libraries may include secret-bearing URLs / extracted student data in diagnostics.
    for name in ("httpx", "httpcore", "google_genai", "google.genai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(execute(args))
    except (ValueError, RuntimeError) as exc:
        # ValidationError contains raw model output; expose its type only.
        from pydantic import ValidationError

        print(
            "Ошибка: неверный JSON распознавания"
            if isinstance(exc, ValidationError)
            else f"Ошибка: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Ошибка: {type(exc).__name__}. Запуск остановлен; секреты скрыты.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
